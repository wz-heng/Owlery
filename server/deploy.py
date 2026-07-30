"""Local deploy-and-restart: dual-slot layout, `owlery deploy init`, and the
fail-closed precondition guard.

This is step 1 of docs/plans/local-deploy.md §17 — the layout and its one-time
migration, plus the guard every later deploy verb calls before doing anything.
No op machinery (`deploy_stage` / `deploy_switch`, the switcher, the journal
reconciler) lives here yet; those are steps 2–4 and build on these primitives.

The invariant that makes the whole feature safe (§13.1) lives in exactly one
place — `switch_current` — the `current` symlink is only ever moved by an
atomic rename, never edited in place. Everything else is plumbing around it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Layout names (docs/plans/local-deploy.md §3). `current` is the ONLY path
# anything runs through; the two slots are full, self-sufficient checkouts.
CURRENT_LINK = "current"
SLOTS: tuple[str, str] = ("a", "b")
JOURNAL_NAME = "journal.jsonl"
SNAPSHOTS_DIR = "snapshots"

# reason_kind values this module can emit (the fail-closed subset of §9). The
# op-layer reasons (deploy_locked, not_idle, stage_failed, …) arrive with their
# ops in later steps.
REASON_NOT_INITIALIZED = "deploy_not_initialized"
REASON_NOT_VIA_CURRENT = "not_running_via_current"


class DeployError(RuntimeError):
    """A `deploy init` step failed. Message is user-facing and secret-free."""


# A command runner is the single subprocess seam of `deploy_init`, so tests can
# drive the whole init flow against temp dirs with a fake instance (no real
# git clone / venv / bun build). It runs `argv` in `cwd` and returns captured
# stdout; it MUST raise DeployError on a non-zero exit.
CommandRunner = Callable[[list[str], Path], str]


def _default_runner(argv: list[str], cwd: Path) -> str:
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise DeployError(f"command not found: {argv[0]}") from exc
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or exc.stdout or "").strip()[-2000:]
        raise DeployError(
            f"`{' '.join(argv)}` failed (exit {exc.returncode}): {tail}"
        ) from exc
    return proc.stdout


# --------------------------------------------------------------------- layout


@dataclass(frozen=True, slots=True)
class DeployLayout:
    """The dual-slot tree rooted at `root` (docs/plans/local-deploy.md §3)."""

    root: Path

    @classmethod
    def at(cls, root: str | os.PathLike[str]) -> "DeployLayout":
        return cls(Path(root))

    @property
    def current_link(self) -> Path:
        return self.root / CURRENT_LINK

    @property
    def journal_path(self) -> Path:
        return self.root / JOURNAL_NAME

    @property
    def snapshots_path(self) -> Path:
        return self.root / SNAPSHOTS_DIR

    def slot_path(self, slot: str) -> Path:
        if slot not in SLOTS:
            raise ValueError(f"unknown slot {slot!r}; expected one of {SLOTS}")
        return self.root / slot

    def current_slot(self) -> str | None:
        """The slot `current` points at ('a'/'b'), or None if the link is
        missing, not a symlink, or resolves outside the two known slots.

        Resolution is by realpath so a link written as the relative target
        `a` and a slot reached through a symlinked root still compare equal."""
        link = self.current_link
        if not link.is_symlink():
            return None
        try:
            target = link.resolve()
        except OSError:
            return None
        for slot in SLOTS:
            slot_dir = self.slot_path(slot)
            # Require the slot to actually exist: a dangling `current` (target
            # slot missing) is a broken/incomplete layout and must read as "no
            # current" so the fail-closed guard refuses rather than trusts it.
            if target == slot_dir.resolve() and slot_dir.is_dir():
                return slot
        return None

    def idle_slot(self) -> str:
        """The slot `current` does NOT point at — where the next deploy stages.
        Defaults to 'b' before any `current` exists so init stages 'a' live."""
        return "b" if self.current_slot() != "b" else "a"

    def switch_current(self, slot: str) -> None:
        """Point `current` at `slot` via the atomic symlink+rename idiom — the
        one operation permitted to move `current` (§3, invariant §13.1).

        The link target is stored relative (`a`/`b`) so the whole tree can be
        relocated. `os.replace` renames the staged link over any existing
        `current` atomically: no window in which `current` is absent or dangling.
        """
        if slot not in SLOTS:
            raise ValueError(f"unknown slot {slot!r}; expected one of {SLOTS}")
        staged = self.root / f".{CURRENT_LINK}.{slot}.staged"
        if staged.is_symlink() or staged.exists():
            staged.unlink()
        os.symlink(slot, staged)
        os.replace(staged, self.current_link)


# ------------------------------------------------------------------ deploy init


@dataclass(frozen=True, slots=True)
class InitResult:
    root: Path
    live_slot: str
    commit: str
    start_command: str
    env_hint: str
    already_initialized: bool


def _git_head(from_checkout: Path, runner: CommandRunner) -> str:
    out = runner(["git", "rev-parse", "HEAD"], from_checkout).strip()
    if not out:
        raise DeployError(f"could not resolve HEAD of {from_checkout}")
    return out


def _clone_slot(
    from_checkout: Path, dst: Path, commit: str, runner: CommandRunner
) -> None:
    """Clone `from_checkout` into `dst` and detach it at the exact `commit`."""
    runner(["git", "clone", str(from_checkout), str(dst)], dst.parent)
    runner(["git", "checkout", "--detach", commit], dst)


def _build_slot(slot_dir: Path, runner: CommandRunner) -> None:
    """Make a cloned slot runnable: venv at its final path (venvs are not
    relocatable — building in place is what makes the flip skew-free, §3) and a
    built `web/dist`."""
    runner([sys.executable, "-m", "venv", ".venv"], slot_dir)
    runner([str(slot_dir / ".venv" / "bin" / "pip"), "install", "-e", "."], slot_dir)
    runner(["bun", "install"], slot_dir / "web")
    runner(["bun", "run", "build"], slot_dir / "web")


def deploy_init(
    root: str | os.PathLike[str],
    from_checkout: str | os.PathLike[str],
    *,
    runner: CommandRunner = _default_runner,
) -> InitResult:
    """One-time migration of a single checkout to the dual-slot layout
    (docs/plans/local-deploy.md §3.1). Offline and idempotent.

    Fresh: clone `--from` into slot `a` at its current HEAD, build it, clone an
    idle slot `b` (first deploy stages it), create `current -> a`, and lay down
    the journal + snapshots dir. Re-run on an already-initialized root: a
    no-op that re-prints the canonical start command and NEVER re-clones — the
    running slot must not be clobbered underfoot.
    """
    layout = DeployLayout.at(Path(root).expanduser())
    src = Path(from_checkout).expanduser().resolve()
    if not (src / ".git").exists():
        raise DeployError(f"--from is not a git checkout (no .git): {src}")

    layout.root.mkdir(parents=True, exist_ok=True)
    # Ancillary dirs/files are cheap and safe to ensure on every run.
    layout.snapshots_path.mkdir(exist_ok=True)
    layout.journal_path.touch(exist_ok=True)

    existing = layout.current_slot()
    if existing is not None and layout.slot_path(existing).exists():
        # Already initialized. Do not touch the live slot; just report.
        commit = _git_head(layout.slot_path(existing), runner)
        return _result(layout, existing, commit, already=True)

    commit = _git_head(src, runner)
    live, idle = "a", "b"
    _clone_slot(src, layout.slot_path(live), commit, runner)
    _build_slot(layout.slot_path(live), runner)
    # Idle slot is cloned but not built — the first deploy_stage builds it (§3.1).
    if not layout.slot_path(idle).exists():
        _clone_slot(src, layout.slot_path(idle), commit, runner)
    layout.switch_current(live)
    return _result(layout, live, commit, already=False)


def _result(
    layout: DeployLayout, slot: str, commit: str, *, already: bool
) -> InitResult:
    current_bin = layout.current_link / ".venv" / "bin" / "owlery"
    return InitResult(
        root=layout.root,
        live_slot=slot,
        commit=commit,
        start_command=f"{current_bin} serve",
        env_hint=f"OWLERY_DEPLOY_ROOT={layout.root}",
        already_initialized=already,
    )


def format_init_report(result: InitResult) -> str:
    """The human summary `owlery deploy init` prints on success."""
    head = (
        "Deploy layout already initialized."
        if result.already_initialized
        else "Deploy layout initialized."
    )
    return (
        f"{head}\n"
        f"  root:    {result.root}\n"
        f"  live:    slot {result.live_slot} @ {result.commit[:12]}\n"
        f"  current: {result.root / CURRENT_LINK} -> {result.live_slot}\n\n"
        "Point the server at this tree and start it via `current`:\n"
        f"  export {result.env_hint}\n"
        f"  {result.start_command}\n\n"
        "Update your launchd plist / shell alias to that command once; the\n"
        "symlink flip keeps it correct across every future deploy. The original\n"
        f"checkout is left untouched — retire it manually."
    )


# ------------------------------------------------------------ fail-closed guard


@dataclass(frozen=True, slots=True)
class DeployPrecheck:
    """Verdict of the fail-closed guard every deploy verb runs first (§3.1)."""

    ok: bool
    reason_kind: str | None = None
    message: str = ""


def _server_root() -> Path:
    """Repo root of the running server — the dir a proper slot server resolves
    its code and `web/dist` from. `server/deploy.py` → parent is `server/`,
    parent.parent is the checkout root, mirroring `cli.do_serve`. `.resolve()`
    fully expands symlinks so it compares equal to `current`'s realpath."""
    return Path(__file__).resolve().parent.parent


def deploy_precheck(settings, *, server_root: Path | None = None) -> DeployPrecheck:
    """Refuse deploy unless the instance is a fully-initialized slot server
    (docs/plans/local-deploy.md §3.1). Fail-closed: any doubt is a refusal that
    names the exact command to fix it. Never mutates anything.

    Guard order and reasons (§9/§12):
      1. no `deploy_root`               → deploy_not_initialized
      2. `deploy_root` set, no `current` → deploy_not_initialized (run init)
      3. `settings.debug` (reload dev)   → not_running_via_current
      4. code not under `current`        → not_running_via_current (restart)
    """
    root = settings.resolved_deploy_root
    if not root:
        return DeployPrecheck(
            False,
            REASON_NOT_INITIALIZED,
            "Local deploy is disabled: set OWLERY_DEPLOY_ROOT and run "
            "`owlery deploy init --root <deploy_root> --from <this checkout>`.",
        )

    layout = DeployLayout.at(root)
    slot = layout.current_slot()
    if slot is None:
        return DeployPrecheck(
            False,
            REASON_NOT_INITIALIZED,
            f"No slot layout at {root}: run "
            f"`owlery deploy init --root {root} --from <this checkout>`.",
        )

    where = server_root or _server_root()
    if getattr(settings, "debug", False):
        return DeployPrecheck(
            False,
            REASON_NOT_VIA_CURRENT,
            "Refusing to deploy from a debug/reload dev server. Start without "
            f"OWLERY_DEBUG via `{layout.current_link / '.venv' / 'bin' / 'owlery'} serve`.",
        )

    if layout.current_link.resolve() != where.resolve():
        return DeployPrecheck(
            False,
            REASON_NOT_VIA_CURRENT,
            "This server is not running through the deploy layout "
            f"(code at {where}, current -> {layout.current_link.resolve()}). "
            f"Restart via `{layout.current_link / '.venv' / 'bin' / 'owlery'} serve`.",
        )

    return DeployPrecheck(True)
