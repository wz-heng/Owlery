"""The detached `deploy_switch` switcher and its write-ahead journal
(docs/plans/local-deploy.md §7.3 + §8, step 3 of §17).

This is the one piece of the deploy pipeline that must survive the death of the
process recording the op: the running server hands off to a tiny, detached
`owlery deploy-switch` child, then shuts itself down. The switcher waits for the
old server to exit, atomically flips `current`, brings up the new slot, health-
checks it, and — if the new version never becomes healthy — rolls everything
back (symlink, DB snapshot, old server). Every step is appended to a fsynced
journal *before* it is performed, so whichever server boots next (new on
success, old on rollback) can reconcile the op's terminal state from the
journal alone (§8).

Design constraints that are load-bearing, not stylistic:

- **stdlib-only, old-slot code.** The switcher binary is
  `<old slot>/.venv/bin/owlery deploy-switch` — the *currently trusted*
  version. It imports nothing from the new slot and nothing heavy (no
  `server.main`, no async framework); its only in-repo dependency is
  `server.deploy.DeployLayout`, itself stdlib-only, so the §13.1 "one sanctioned
  flip point" (`switch_current`) is reused rather than re-implemented.
- **fully detached children.** Every server the switcher launches is spawned
  double-fork + `setsid` into its own session and process group, so it outlives
  the switcher and can never be reaped by a parent's process-group cleanup —
  this machine's `multi-agent-contention` zombie incident is exactly what a
  child reaped that way would recreate (§7.2). Detachment is hygiene, not an
  option.
- **never SIGKILL the old server.** The old server shuts *itself* down; the
  switcher only waits for it. On timeout it journals `old_wont_die` and exits
  without flipping — it never signals a server that may still be finishing
  durable writes (§7.3 step 1).

The switcher runs forward-only, once per op. Resuming an interrupted switch is
the boot reconciler's job (§8, step 4), not this process's.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .deploy import CURRENT_LINK, DeployLayout

# Journal step names (docs/plans/local-deploy.md §8). `handoff` is written by the
# server before it spawns the switcher; every other name below is written by the
# switcher itself. Terminal tails (`switched_ok` / `rolled_back` / `old_wont_die`
# / `switch_error`) are what boot reconciliation keys off.
STEP_HANDOFF = "handoff"
STEP_OLD_WONT_DIE = "old_wont_die"
STEP_FLIP_DONE = "flip_done"
STEP_SWITCHED_OK = "switched_ok"
STEP_ROLLBACK_BEGIN = "rollback_begin"
STEP_ROLLED_BACK = "rolled_back"
STEP_SWITCH_ERROR = "switch_error"

# Rollback reason strings recorded in the journal detail. Boot reconciliation
# folds both into the delivery reason `health_failed` (§8); the specific string
# survives in the journal for the operator.
REASON_HEALTH_TIMEOUT = "health_timeout"
REASON_NEW_PROCESS_EXITED = "new_process_exited"

# Exit codes: 0 success, 1 a clean terminal failure (old won't die / rolled
# back), 2 an operational abort (missing/corrupt handoff). Reconciliation reads
# the journal tail, not the exit code — these are for the shell/log only.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_ERROR = 2

_now = time.monotonic


# --------------------------------------------------------------------- journal


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class Journal:
    """Append-only, per-line-fsynced switcher journal (§8).

    Each line is one JSON object `{ts, op_id, step, detail}`. `append` flushes
    and `os.fsync`s before returning, so a step is durably on disk the instant
    the switcher moves on to perform its action — the write-ahead ordering that
    lets boot reconciliation trust the tail.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def append(self, op_id: str, step: str, detail: dict[str, Any] | None = None) -> None:
        record = {
            "ts": _utcnow_iso(),
            "op_id": op_id,
            "step": step,
            "detail": detail or {},
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    def entries(self, op_id: str | None = None) -> list[dict[str, Any]]:
        """All journal records, optionally filtered to one op id, in file order.

        Tolerates a partially-written trailing line (a crash mid-append leaves at
        most one torn line): a line that fails to parse is skipped, never fatal.
        """
        out: list[dict[str, Any]] = []
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return out
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if op_id is None or record.get("op_id") == op_id:
                out.append(record)
        return out


# ------------------------------------------------------------- handoff contract


@dataclass(frozen=True, slots=True)
class Handoff:
    """The switch contract the server captures in the journal `handoff` line
    (§7.2 step 2) and the switcher reads back. Everything the detached child
    needs to flip, restart, health-check, and roll back — resolved once, at
    handoff, from the live process it is replacing."""

    op_id: str
    from_slot: str
    to_slot: str
    old_sha: str
    new_sha: str
    old_pid: int
    host: str
    port: int
    db_path: str
    snapshot_path: str
    serve_argv: list[str]
    serve_env: dict[str, str]

    @classmethod
    def from_detail(cls, op_id: str, detail: dict[str, Any]) -> "Handoff":
        required = (
            "from_slot", "to_slot", "old_sha", "new_sha",
            "old_pid", "host", "port", "db_path", "snapshot_path",
        )
        missing = [k for k in required if detail.get(k) in (None, "")]
        if missing:
            raise ValueError(f"handoff detail missing keys: {', '.join(missing)}")
        return cls(
            op_id=op_id,
            from_slot=str(detail["from_slot"]),
            to_slot=str(detail["to_slot"]),
            old_sha=str(detail["old_sha"]),
            new_sha=str(detail["new_sha"]),
            old_pid=int(detail["old_pid"]),
            host=str(detail["host"]),
            port=int(detail["port"]),
            db_path=str(detail["db_path"]),
            snapshot_path=str(detail["snapshot_path"]),
            serve_argv=list(detail.get("serve_argv") or ["serve"]),
            serve_env=dict(detail.get("serve_env") or {}),
        )


# ------------------------------------------------------- process / port helpers
#
# Module-level so integration tests exercise the real implementations while the
# fsync-ordering unit test can monkeypatch the heavy ones (spawn/health/wait) to
# isolate the journal-before-action ordering without a real flip.


def _pid_alive(pid: int) -> bool:
    """True if `pid` names a live process. `kill(pid, 0)` probes without
    signalling; `EPERM` means the process exists but is owned by another user
    (still alive)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _port_free(host: str, port: int) -> bool:
    """True if nothing accepts a TCP connection on `host:port` — a connect-based
    probe (never binds), so it cannot race the server that is about to bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((host, port))
        except OSError:
            return True
        return False


def _wait_old_gone(handoff: Handoff, timeout: float, poll: float) -> bool:
    """§7.3 step 1: wait until the old pid has exited AND its port is released.
    Returns False on timeout. Never signals the old process."""
    deadline = _now() + timeout
    while _now() < deadline:
        if not _pid_alive(handoff.old_pid) and _port_free(handoff.host, handoff.port):
            return True
        time.sleep(poll)
    return False


def _wait_port_free(host: str, port: int, timeout: float, poll: float) -> bool:
    deadline = _now() + timeout
    while _now() < deadline:
        if _port_free(host, port):
            return True
        time.sleep(poll)
    return False


# /health is always the local instance on loopback (§7.3 step 4, unauthenticated
# by design). Poll it directly, never through an ambient HTTP proxy: a system
# Clash/`http_proxy` that does not exempt 127.0.0.1 would swallow the request and
# answer 502, so the switcher would wrongly conclude the new server never came up
# and roll a healthy deploy back. An empty ProxyHandler disables all proxying —
# the same reasoning as Owlery's own loopback MCP callbacks passing
# `trust_env=False` (server/harness/assembly.py).
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fetch_health_sha(host: str, port: int, timeout: float) -> str | None:
    """GET /health and return its reported build sha, or None if unreachable /
    malformed. The sha match (not mere reachability) is what proves the *new*
    code booted, never a lingering old process (§7.3 step 4, §11)."""
    url = f"http://{host}:{port}/health"
    try:
        with _NO_PROXY_OPENER.open(url, timeout=timeout) as resp:  # noqa: S310 (loopback)
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    sha = payload.get("sha") if isinstance(payload, dict) else None
    return str(sha) if sha is not None else None


def _spawn_detached(argv: list[str], cwd: str, env: dict[str, str], log_path: str) -> int:
    """Launch `argv` fully detached — double-fork + `setsid` into its own session
    and process group, stdio redirected to `log_path` — and return the final
    (grandchild) pid via a pipe (§7.2 step 4).

    The grandchild is reparented to init immediately, so it outlives the switcher
    and is never in the switcher's process group; the switcher keeps its pid only
    to health-check and, on rollback, to terminate it."""
    read_fd, write_fd = os.pipe()
    intermediate = os.fork()
    if intermediate > 0:
        os.close(write_fd)
        os.waitpid(intermediate, 0)  # reap the intermediate; grandchild is init's
        raw = b""
        while True:
            chunk = os.read(read_fd, 64)
            if not chunk:
                break
            raw += chunk
        os.close(read_fd)
        return int(raw.decode().strip())

    # Intermediate child: become a session leader, then fork the real server.
    try:
        os.close(read_fd)
        os.setsid()
        grandchild = os.fork()
        if grandchild > 0:
            os.write(write_fd, str(grandchild).encode())
            os.close(write_fd)
            os._exit(0)
        # Grandchild: detach stdio and exec the server.
        os.close(write_fd)
        os.chdir(cwd)
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        null_fd = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null_fd, 0)
        os.execve(argv[0], argv, env)
    except BaseException:
        os._exit(127)
    os._exit(127)  # unreachable; keeps type checkers happy


def _terminate(pid: int, grace: float, poll: float) -> None:
    """SIGTERM `pid`, then SIGKILL after `grace` if it has not exited. Only ever
    called on the *new* server during rollback — probation (§7.5) guarantees it
    holds no user work, so a hard kill is safe (§7.3 step 6)."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = _now() + grace
    while _now() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(min(poll, 0.1))
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _restore_snapshot(snapshot_path: str, db_path: str) -> None:
    """Restore the pre-switch SQLite snapshot over the live DB, making the
    rollback total (§7.4). Any stale `-wal`/`-shm` sidecar of the live DB is
    removed so a leftover WAL cannot overlay the restored file. The snapshot
    itself is never deleted (a rollback keeps the snapshot it used, §7.4)."""
    shutil.copyfile(snapshot_path, db_path)
    for sidecar in (db_path + "-wal", db_path + "-shm"):
        try:
            os.unlink(sidecar)
        except FileNotFoundError:
            pass


def _flip(root: Path, slot: str) -> None:
    """The atomic `current` flip — the single sanctioned mutation point (§13.1),
    reused from `deploy.DeployLayout` so the invariant lives in exactly one
    place. Wrapped here as a module function so the ordering test can spy it."""
    DeployLayout.at(root).switch_current(slot)


# ------------------------------------------------------------------- the switch


def _serve_command(root: Path, handoff: Handoff) -> tuple[list[str], dict[str, str], str]:
    """The `<root>/current/.venv/bin/owlery serve` invocation, resolved through
    `current` so it starts whichever slot `current` currently points at — the new
    slot after the flip, the old slot after a rollback flip-back (§7.3 step 3).
    Env is the switcher's own environment (inherited from the old server, so the
    'same env contract') overlaid with the handoff's `serve_env`."""
    owlery_bin = str(root / CURRENT_LINK / ".venv" / "bin" / "owlery")
    argv = [owlery_bin, *handoff.serve_argv]
    env = {**os.environ, **handoff.serve_env}
    return argv, env, str(root / CURRENT_LINK)


def _poll_new_health(
    handoff: Handoff, new_pid: int, health_timeout: float, poll: float
) -> str | None:
    """§7.3 step 4: poll /health for the new sha until healthy or the window
    closes. Returns None on success, or a rollback reason string if the new
    process died or never reported the new sha in time."""
    deadline = _now() + health_timeout
    while _now() < deadline:
        if not _pid_alive(new_pid):
            return REASON_NEW_PROCESS_EXITED
        if _fetch_health_sha(handoff.host, handoff.port, timeout=min(5.0, health_timeout)) == handoff.new_sha:
            return None
        time.sleep(poll)
    return REASON_HEALTH_TIMEOUT


def run_switch(
    journal_path: str | os.PathLike[str],
    op_id: str,
    *,
    switch_timeout: float,
    health_timeout: float,
    term_grace: float,
    poll_interval: float,
) -> int:
    """Execute the §7.3 switcher for `op_id`, journaling every step before
    performing it. Returns a process exit code (§8 reconciliation reads the
    journal tail, not this code)."""
    journal = Journal(journal_path)
    root = journal.path.parent
    log_path = str(root / "switcher.log")

    # Read our contract from the handoff line. A missing/corrupt handoff is an
    # operational abort, not a deploy outcome — journal it and stop (§8 treats a
    # `handoff`-only tail as interrupted; we add a louder terminal record).
    handoff_detail: dict[str, Any] | None = None
    for record in journal.entries(op_id):
        if record.get("step") == STEP_HANDOFF:
            handoff_detail = record.get("detail") or {}
    if handoff_detail is None:
        journal.append(op_id, STEP_SWITCH_ERROR, {"reason": "no_handoff_line"})
        return EXIT_ERROR
    try:
        handoff = Handoff.from_detail(op_id, handoff_detail)
    except (ValueError, TypeError, KeyError) as exc:
        journal.append(op_id, STEP_SWITCH_ERROR, {"reason": f"bad_handoff: {exc}"})
        return EXIT_ERROR

    # Step 1: wait for the old server to exit and free the port. Never signal it.
    if not _wait_old_gone(handoff, switch_timeout, poll_interval):
        journal.append(
            op_id,
            STEP_OLD_WONT_DIE,
            {"old_pid": handoff.old_pid, "port": handoff.port,
             "waited_seconds": switch_timeout},
        )
        return EXIT_FAILED  # flip never happened; nothing changed

    # Step 2: journal-before-act, then flip `current` to the staged slot.
    journal.append(
        op_id, STEP_FLIP_DONE,
        {"from_slot": handoff.from_slot, "to_slot": handoff.to_slot},
    )
    _flip(root, handoff.to_slot)

    # Step 3: bring up the new slot's server, fully detached.
    argv, env, cwd = _serve_command(root, handoff)
    new_pid = _spawn_detached(argv, cwd, env, log_path)

    # Step 4: health-check the new server for the new sha.
    reason = _poll_new_health(handoff, new_pid, health_timeout, poll_interval)
    if reason is None:
        # Step 5: success.
        journal.append(
            op_id, STEP_SWITCHED_OK,
            {"slot": handoff.to_slot, "sha": handoff.new_sha, "pid": new_pid},
        )
        return EXIT_OK

    # Step 6: rollback — total return to the pre-switch state (§7.3 step 6, §7.4).
    journal.append(op_id, STEP_ROLLBACK_BEGIN, {"reason": reason})
    _terminate(new_pid, term_grace, poll_interval)
    _wait_port_free(handoff.host, handoff.port, switch_timeout, poll_interval)
    _flip(root, handoff.from_slot)
    _restore_snapshot(handoff.snapshot_path, handoff.db_path)
    old_argv, old_env, old_cwd = _serve_command(root, handoff)
    old_pid = _spawn_detached(old_argv, old_cwd, old_env, log_path)
    journal.append(
        op_id, STEP_ROLLED_BACK,
        {"reason": reason, "restored_slot": handoff.from_slot,
         "restored_sha": handoff.old_sha, "old_pid": old_pid},
    )
    return EXIT_FAILED


# --------------------------------------------------------------------- CLI glue


def build_switch_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="owlery deploy-switch",
        description="Detached deploy switcher (docs/plans/local-deploy.md §7.3).",
    )
    parser.add_argument("--journal", required=True, help="Path to journal.jsonl")
    parser.add_argument("--op", required=True, dest="op_id", help="deploy_switch op id")
    parser.add_argument("--switch-timeout", type=float, default=30.0,
                        help="Old-exit + port-free wait cap, seconds (§9)")
    parser.add_argument("--health-timeout", type=float, default=60.0,
                        help="New-server /health wait cap, seconds (§9)")
    parser.add_argument("--term-grace", type=float, default=10.0,
                        help="SIGTERM→SIGKILL grace for the new server on rollback")
    parser.add_argument("--poll-interval", type=float, default=0.2,
                        help="Poll cadence for the wait/health loops, seconds")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_switch_parser().parse_args(argv)
    return run_switch(
        args.journal,
        args.op_id,
        switch_timeout=args.switch_timeout,
        health_timeout=args.health_timeout,
        term_grace=args.term_grace,
        poll_interval=args.poll_interval,
    )


if __name__ == "__main__":
    sys.exit(main())
