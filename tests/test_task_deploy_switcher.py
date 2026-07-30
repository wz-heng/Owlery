"""Standalone integration tests for the detached deploy switcher and its
write-ahead journal (docs/plans/local-deploy.md §7.3 + §8, step 3 of §17).

The switcher is driven exactly as production drives it — as a real subprocess
(`python -m server.switcher ...`, the same code path as `owlery deploy-switch`)
against a **fake instance**: each slot's `.venv/bin/owlery` is a stub `serve`
script that binds a real port and answers `/health` with the sha baked into its
slot. Every test uses real processes, a real TCP port, and temp dirs only; no
real production path is ever touched (§14).

Covered (the §14 switcher bullets):
- old-exit wait: the switcher does not flip until the old pid exits and its port
  frees;
- `old_wont_die`: a stuck old server times the switcher out with no flip and no
  signal to the old process;
- atomic flip + health success → `switched_ok`;
- health failure → full rollback (flip-back, DB-snapshot restore, old server
  restart) → `rolled_back`;
- new-process-exit rollback;
- journal fsync ordering: the step line is durable before its action runs;
- missing-handoff abort.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

import server.switcher as switcher
from server.switcher import Journal

REPO_ROOT = Path(switcher.__file__).resolve().parent.parent

# A fake `owlery` slot binary: `serve` binds a real port and answers /health with
# the slot's own sha (read from <slot>/HEALTH_SHA, resolved through `current` so a
# flip changes which sha is reported). It records its pid to <slot>/server.pid for
# test teardown. SIGTERM exits immediately — a stand-in for graceful shutdown.
_STUB = """\
#!{python}
import json, os, signal, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def main():
    slot = Path(__file__).resolve().parents[2]   # <slot>/.venv/bin/owlery -> <slot>
    sha = (slot / "HEALTH_SHA").read_text().strip()
    (slot / "server.pid").write_text(str(os.getpid()))
    port = int(os.environ["OWLERY_PORT"])
    host = os.environ.get("OWLERY_HOST", "127.0.0.1")

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                body = json.dumps({{"status": "ok", "sha": sha}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    signal.signal(signal.SIGTERM, lambda *a: os._exit(0))
    HTTPServer((host, port), H).serve_forever()


if __name__ == "__main__":
    main()
"""

# A broken slot binary that exits immediately instead of serving — drives the
# new-process-exited rollback branch (§7.3 step 6).
_STUB_CRASH = """\
#!{python}
import sys
sys.exit(1)
"""


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _kill(pid: int) -> None:
    # Defensive: this module SIGKILLs pids recovered from a journal/pid-file, so
    # it must never turn on itself. Refuse pid<=1 (init/kernel), the test process,
    # and its parent — a stubbed spawn or a corrupt record must not be able to
    # kill the test runner or a whole process group.
    if pid <= 1 or pid in (os.getpid(), os.getppid()):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _make_slot(root: Path, slot: str, sha: str, *, crash: bool = False) -> None:
    """Create <root>/<slot> with a stub `.venv/bin/owlery` and a HEALTH_SHA."""
    slot_dir = root / slot
    bin_dir = slot_dir / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (slot_dir / "HEALTH_SHA").write_text(sha)
    stub = (_STUB_CRASH if crash else _STUB).format(python=sys.executable)
    owlery = bin_dir / "owlery"
    owlery.write_text(stub)
    owlery.chmod(0o755)


def _current_target(root: Path) -> str | None:
    link = root / "current"
    return os.readlink(link) if link.is_symlink() else None


def _write_handoff(journal_path: Path, op_id: str, detail: dict) -> None:
    Journal(journal_path).append(op_id, "handoff", detail)


def _reap_async(proc: subprocess.Popen) -> None:
    """Reap `proc` in a background thread so it does not linger as a zombie child
    of the test process. Production faithfulness: the old server is a child of
    launchd/init, not of the switcher, so its real parent reaps it and the
    switcher's `os.kill(pid, 0)` liveness probe correctly reports it gone. Without
    reaping, `os.kill` succeeds on the zombie and the switcher would wrongly see
    the old server as still alive."""
    threading.Thread(target=lambda: proc.wait(), daemon=True).start()


def _wait_health(port: int, sha: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if switcher._fetch_health_sha("127.0.0.1", port, timeout=1.0) == sha:
            return True
        time.sleep(0.05)
    return False


class DeployEnv:
    """A temp dual-slot deploy tree with a fake instance, plus pid bookkeeping so
    every detached server the switcher spawns is reaped at teardown."""

    def __init__(self, root: Path, port: int, op_id: str):
        self.root = root
        self.port = port
        self.op_id = op_id
        self.journal_path = root / "journal.jsonl"
        self.db_path = root / "owlery.db"
        self.snapshot_path = root / "snapshots" / f"{op_id}.db"
        self._procs: list[subprocess.Popen] = []

    def start_server(self, slot: str) -> subprocess.Popen:
        owlery = self.root / slot / ".venv" / "bin" / "owlery"
        env = {**os.environ, "OWLERY_PORT": str(self.port)}
        proc = subprocess.Popen([str(owlery), "serve"], env=env)
        self._procs.append(proc)
        return proc

    def switcher_argv(self, **overrides) -> list[str]:
        opts = {
            "switch-timeout": 6.0, "health-timeout": 6.0,
            "term-grace": 2.0, "poll-interval": 0.1,
        }
        opts.update({k.replace("_", "-"): v for k, v in overrides.items()})
        argv = [sys.executable, "-m", "server.switcher",
                "--journal", str(self.journal_path), "--op", self.op_id]
        for k, v in opts.items():
            argv += [f"--{k}", str(v)]
        return argv

    def run_switcher(self, **overrides) -> subprocess.CompletedProcess:
        return subprocess.run(
            self.switcher_argv(**overrides), cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=60,
        )

    def journal_pids(self) -> list[int]:
        pids = []
        for rec in Journal(self.journal_path).entries():
            detail = rec.get("detail") or {}
            for key in ("pid", "old_pid", "new_pid"):
                if isinstance(detail.get(key), int):
                    pids.append(detail[key])
        for slot in ("a", "b"):
            pid_file = self.root / slot / "server.pid"
            if pid_file.exists():
                try:
                    pids.append(int(pid_file.read_text().strip()))
                except ValueError:
                    pass
        return pids

    def cleanup(self) -> None:
        for pid in self.journal_pids():
            _kill(pid)
        for proc in self._procs:
            _kill(proc.pid)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


@pytest.fixture
def env(tmp_path: Path):
    root = tmp_path / "deploy"
    root.mkdir()
    (root / "snapshots").mkdir()
    (root / "journal.jsonl").touch()
    op_id = f"op-{uuid.uuid4().hex[:8]}"
    _make_slot(root, "a", "sha-old-aaaaaaaa")
    _make_slot(root, "b", "sha-new-bbbbbbbb")
    os.symlink("a", root / "current")
    e = DeployEnv(root, _free_port(), op_id)
    e.db_path.write_text("db-mutated-by-new")
    e.snapshot_path.write_text("db-preswitch-snapshot")
    yield e
    e.cleanup()


def _handoff_detail(env: DeployEnv, *, from_slot="a", to_slot="b",
                    old_sha="sha-old-aaaaaaaa", new_sha="sha-new-bbbbbbbb",
                    old_pid: int) -> dict:
    return {
        "from_slot": from_slot, "to_slot": to_slot,
        "old_sha": old_sha, "new_sha": new_sha,
        "old_pid": old_pid, "host": "127.0.0.1", "port": env.port,
        "db_path": str(env.db_path), "snapshot_path": str(env.snapshot_path),
        "serve_argv": ["serve"],
        "serve_env": {"OWLERY_PORT": str(env.port)},
    }


# --------------------------------------------------------------------- old-exit


def test_old_exit_wait_then_switch(env: DeployEnv):
    """The switcher must not flip while the old server still holds the port; once
    the old pid exits and the port frees, it flips and completes."""
    old = env.start_server("a")
    assert _wait_health(env.port, "sha-old-aaaaaaaa", 5.0)
    _write_handoff(env.journal_path, env.op_id, _handoff_detail(env, old_pid=old.pid))

    proc = subprocess.Popen(env.switcher_argv(), cwd=str(REPO_ROOT))
    try:
        # While the old server is alive the switcher stays in step 1: no flip.
        time.sleep(1.0)
        assert _current_target(env.root) == "a"
        steps = [r["step"] for r in Journal(env.journal_path).entries(env.op_id)]
        assert "flip_done" not in steps

        old.terminate()  # simulate the old server's graceful shutdown
        _reap_async(old)  # its real parent (launchd in prod) reaps it, not the switcher
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()

    assert _current_target(env.root) == "b"
    tail = Journal(env.journal_path).entries(env.op_id)[-1]
    assert tail["step"] == "switched_ok"
    assert tail["detail"]["sha"] == "sha-new-bbbbbbbb"
    assert _wait_health(env.port, "sha-new-bbbbbbbb", 5.0)


def test_old_wont_die_no_flip(env: DeployEnv):
    """A stuck old server times the switcher out: no flip, and the old process is
    never signalled by the switcher (§7.3 step 1 — never SIGKILL the old server)."""
    old = env.start_server("a")
    assert _wait_health(env.port, "sha-old-aaaaaaaa", 5.0)
    _write_handoff(env.journal_path, env.op_id, _handoff_detail(env, old_pid=old.pid))

    result = env.run_switcher(switch_timeout=1.5, health_timeout=2.0)

    assert result.returncode == switcher.EXIT_FAILED
    assert _current_target(env.root) == "a"           # never flipped
    assert _pid_alive(old.pid)                          # never killed
    assert _wait_health(env.port, "sha-old-aaaaaaaa", 2.0)  # old still serving
    tail = Journal(env.journal_path).entries(env.op_id)[-1]
    assert tail["step"] == "old_wont_die"
    assert "flip_done" not in [r["step"] for r in Journal(env.journal_path).entries(env.op_id)]


# --------------------------------------------------------------------- success


def test_health_success_switches_ok(env: DeployEnv):
    """Old already exiting → atomic flip to the staged slot, new server comes up
    healthy with the new sha, op ends `switched_ok`; `current` is always a valid
    symlink (never absent/dangling)."""
    old = env.start_server("a")
    assert _wait_health(env.port, "sha-old-aaaaaaaa", 5.0)
    _write_handoff(env.journal_path, env.op_id, _handoff_detail(env, old_pid=old.pid))
    old.terminate()
    old.wait(timeout=5)

    result = env.run_switcher()

    assert result.returncode == switcher.EXIT_OK
    # Atomic flip: current is a symlink pointing at a real slot 'b'.
    link = env.root / "current"
    assert link.is_symlink() and _current_target(env.root) == "b"
    assert (link.resolve() == (env.root / "b").resolve())
    steps = [r["step"] for r in Journal(env.journal_path).entries(env.op_id)]
    assert steps[-2:] == ["flip_done", "switched_ok"]
    assert _wait_health(env.port, "sha-new-bbbbbbbb", 5.0)
    # DB untouched on success (snapshot restore is a rollback-only action).
    assert env.db_path.read_text() == "db-mutated-by-new"


# -------------------------------------------------------------------- rollback


def test_health_failure_full_rollback(env: DeployEnv):
    """New server never reports the expected sha → the switcher rolls everything
    back: flip `current` to 'a', restore the DB snapshot (and drop stale WAL),
    restart the old server, terminate the failed new one, journal `rolled_back`."""
    # Make slot 'b' report a sha that never matches the handoff's new_sha.
    (env.root / "b" / "HEALTH_SHA").write_text("sha-wrong-cccccccc")
    (env.db_path.parent / "owlery.db-wal").write_text("stale-wal")

    old = env.start_server("a")
    assert _wait_health(env.port, "sha-old-aaaaaaaa", 5.0)
    _write_handoff(env.journal_path, env.op_id, _handoff_detail(env, old_pid=old.pid))
    old.terminate()
    old.wait(timeout=5)

    result = env.run_switcher(health_timeout=2.5)

    assert result.returncode == switcher.EXIT_FAILED
    # Symlink rolled back.
    assert _current_target(env.root) == "a"
    # DB snapshot restored total; stale WAL sidecar removed.
    assert env.db_path.read_text() == "db-preswitch-snapshot"
    assert not (env.db_path.parent / "owlery.db-wal").exists()
    # Snapshot the rollback used is never deleted.
    assert env.snapshot_path.read_text() == "db-preswitch-snapshot"
    # Old server restarted and serving the old sha again.
    assert _wait_health(env.port, "sha-old-aaaaaaaa", 6.0)
    entries = Journal(env.journal_path).entries(env.op_id)
    steps = [r["step"] for r in entries]
    assert steps[-3:] == ["flip_done", "rollback_begin", "rolled_back"]
    assert entries[-1]["detail"]["reason"] == switcher.REASON_HEALTH_TIMEOUT
    # The failed new (slot 'b') process was terminated.
    b_pid = int((env.root / "b" / "server.pid").read_text().strip())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and _pid_alive(b_pid):
        time.sleep(0.05)
    assert not _pid_alive(b_pid)


def test_new_process_exit_rollback(env: DeployEnv):
    """A new slot whose server exits immediately triggers the same rollback via
    the process-death branch, with reason `new_process_exited`."""
    _make_slot(env.root, "b", "sha-new-bbbbbbbb", crash=True)

    old = env.start_server("a")
    assert _wait_health(env.port, "sha-old-aaaaaaaa", 5.0)
    _write_handoff(env.journal_path, env.op_id, _handoff_detail(env, old_pid=old.pid))
    old.terminate()
    old.wait(timeout=5)

    result = env.run_switcher(health_timeout=6.0)

    assert result.returncode == switcher.EXIT_FAILED
    assert _current_target(env.root) == "a"
    assert env.db_path.read_text() == "db-preswitch-snapshot"
    entries = Journal(env.journal_path).entries(env.op_id)
    assert entries[-1]["step"] == "rolled_back"
    assert entries[-1]["detail"]["reason"] == switcher.REASON_NEW_PROCESS_EXITED
    assert _wait_health(env.port, "sha-old-aaaaaaaa", 6.0)


# ---------------------------------------------------------------- journal / abort


def test_journal_fsync_ordering(env: DeployEnv, monkeypatch):
    """The write-ahead discipline: the `flip_done` line is durably on disk before
    the flip runs. Heavy steps are stubbed so the ordering is isolated from real
    processes (§14 'journal fsync ordering — step written before action')."""
    _write_handoff(env.journal_path, env.op_id, _handoff_detail(env, old_pid=os.getpid()))

    observed: dict = {}
    real_flip = switcher._flip

    def spy_flip(root, slot):
        # At the instant of the flip the journal must already end with flip_done.
        tail = Journal(env.journal_path).entries(env.op_id)[-1]
        observed["tail_at_flip"] = tail["step"]
        return real_flip(root, slot)

    # A real, harmless throwaway stands in for the "new server" pid so the flow
    # (and the fixture's pid-based cleanup) operates on a genuine killable process
    # rather than a synthetic number.
    dummy = subprocess.Popen(["sleep", "30"])
    env._procs.append(dummy)
    monkeypatch.setattr(switcher, "_wait_old_gone", lambda *a, **k: True)
    monkeypatch.setattr(switcher, "_spawn_detached", lambda *a, **k: dummy.pid)
    monkeypatch.setattr(switcher, "_poll_new_health", lambda *a, **k: None)
    monkeypatch.setattr(switcher, "_flip", spy_flip)

    rc = switcher.run_switch(
        env.journal_path, env.op_id,
        switch_timeout=1.0, health_timeout=1.0, term_grace=1.0, poll_interval=0.01,
    )

    assert rc == switcher.EXIT_OK
    assert observed["tail_at_flip"] == "flip_done"
    assert _current_target(env.root) == "b"


def test_journal_append_is_fsynced(env: DeployEnv, monkeypatch):
    """Every `Journal.append` flushes and fsyncs before returning, and the line
    is a well-formed record readable back by `entries`."""
    fsynced: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (fsynced.append(fd), real_fsync(fd))[1])

    j = Journal(env.journal_path)
    j.append("op-x", "flip_done", {"from_slot": "a", "to_slot": "b"})

    assert fsynced, "append must os.fsync the journal fd"
    rec = j.entries("op-x")[-1]
    assert rec["step"] == "flip_done"
    assert rec["detail"] == {"from_slot": "a", "to_slot": "b"}
    assert "ts" in rec and rec["op_id"] == "op-x"


def test_missing_handoff_aborts(env: DeployEnv):
    """No `handoff` line for the op → an operational abort (`switch_error`,
    exit 2), never a flip."""
    result = env.run_switcher()

    assert result.returncode == switcher.EXIT_ERROR
    assert _current_target(env.root) == "a"
    tail = Journal(env.journal_path).entries(env.op_id)[-1]
    assert tail["step"] == "switch_error"
    assert tail["detail"]["reason"] == "no_handoff_line"


def test_corrupt_handoff_aborts(env: DeployEnv):
    """A handoff line missing required keys aborts with `switch_error`, no flip."""
    _write_handoff(env.journal_path, env.op_id, {"from_slot": "a"})  # missing the rest

    result = env.run_switcher()

    assert result.returncode == switcher.EXIT_ERROR
    assert _current_target(env.root) == "a"
    tail = Journal(env.journal_path).entries(env.op_id)[-1]
    assert tail["step"] == "switch_error"
    assert tail["detail"]["reason"].startswith("bad_handoff")
