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
import shutil
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

# Runtime config (§3.2). The deploy root owns the canonical `.env`; each slot
# carries a relative symlink to it. Both halves are load-bearing:
#
#   * The slot cannot inherit config from the environment. pydantic reads
#     `.env` into Settings but never exports to os.environ, so the switcher's
#     "same env" handoff (§7.3 step 3) carries nothing a `.env` supplied.
#   * The slot cannot get one from its own checkout either — `.env` is
#     gitignored, so a slot is created without any runtime config at all.
#
# Without this, a slot silently falls back to every default: `db_path` is
# relative, so the server opens an empty DB next to its cwd instead of the
# live one, and `deploy_root` is empty, so /health reports sha=null and every
# switch health-times-out with a rollback that fails the exact same way.
ENV_FILE = ".env"

# reason_kind values this module can emit (the fail-closed subset of §9). The
# switch-layer reasons (not_idle, health_failed, old_wont_die) arrive with §17
# step 4.
REASON_NOT_INITIALIZED = "deploy_not_initialized"
REASON_NOT_VIA_CURRENT = "not_running_via_current"
REASON_DEPLOY_LOCKED = "deploy_locked"
REASON_STAGE_FAILED = "stage_failed"


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

    @staticmethod
    def _is_real_slot(slot_dir: Path) -> bool:
        """A usable slot is a real directory entry directly under the deploy
        root — never a symlink and never a missing/dangling path.

        Rejecting symlinks is fail-closed and load-bearing: a slot symlinked to
        an out-of-root target would let `current -> a -> /outside` pass every
        guard, deploying and (later) mutating a path outside `deploy_root` —
        exactly what invariant §13.9 forbids. `is_dir()`/`resolve()` both follow
        symlinks, so the `is_symlink()` check is what actually pins the slot
        inside the tree."""
        return slot_dir.is_dir() and not slot_dir.is_symlink()

    def current_slot(self) -> str | None:
        """The slot `current` points at ('a'/'b'), or None if the link is
        missing, not a symlink, or resolves outside the two known real slots.

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
            # Require a real slot dir: a dangling `current` (target missing) or a
            # slot that is itself a symlink is a broken/foreign layout and must
            # read as "no current" so the fail-closed guard refuses rather than
            # trusts it.
            if self._is_real_slot(slot_dir) and target == slot_dir.resolve():
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
        # Never point `current` at a missing or symlinked slot: this is the one
        # sanctioned flip point, so it validates the target rather than trusting
        # the caller to have built the slot — a bad flip would leave a permanent
        # dangling/foreign `current` that every later guard would then key off.
        slot_dir = self.slot_path(slot)
        if not self._is_real_slot(slot_dir):
            raise DeployError(
                f"cannot flip current to slot {slot!r}: {slot_dir} is not a "
                "real slot directory (missing, or a symlink out of the tree)"
            )
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


def _seed_root_env(src_checkout: Path, root: Path) -> None:
    """Give the deploy root its canonical `.env`, copied once from the checkout
    being migrated (§3.2).

    Never overwrites: once the root has a `.env` it is the live instance's
    authoritative config, and a later `deploy init` re-run must not clobber it
    with whatever a checkout happens to carry. A checkout with no `.env`
    (config supplied by exported env vars instead) seeds nothing — that is a
    valid setup, and `config_probe` is what actually decides whether a slot can
    resolve its settings."""
    src, dst = src_checkout / ENV_FILE, root / ENV_FILE
    if dst.exists() or not src.is_file():
        return
    shutil.copy2(src, dst)
    dst.chmod(0o600)  # auth token + bridge secrets


def _link_slot_env(root: Path, slot_dir: Path) -> None:
    """Point `<slot>/.env` at the deploy root's canonical file.

    Relative (`../.env`) so the tree stays relocatable, and re-created on every
    stage because `git clone` into a fresh slot dir starts with no `.env` at
    all. Idempotent and self-healing: an existing link/file is replaced. A root
    with no `.env` leaves no link rather than a dangling one."""
    src = root / ENV_FILE
    dst = slot_dir / ENV_FILE
    if not src.is_file():
        return
    if dst.is_symlink() or dst.exists():
        dst.unlink()
    dst.symlink_to(Path("..") / ENV_FILE)


def config_probe_source(expected_root: Path | str) -> str:
    """Python source for the stage-time config probe (§5.6).

    Runs inside the staged slot, with the slot's own interpreter and cwd —
    the same way `owlery serve` will run there — and fails the stage if the
    settings that slot would boot with are unusable. It deliberately re-derives
    them rather than trusting `.env` to exist: a slot configured by exported
    env vars is equally valid, and only the resolved values matter.

    The two checks are precisely the two ways a slot can look healthy and still
    destroy a deploy (both observed in production, 2026-08-09):

      * a relative `db_path` resolves against the server's cwd, so the slot
        opens an empty database of its own instead of the live one;
      * an unset/foreign `deploy_root` makes /health report `sha: null`, which
        the switcher can never match against the handoff sha — so the switch
        health-times-out and the rollback, restarting the old server through
        the identical path, fails the identical way (`rollback_incomplete`).
    """
    return (
        "import os, sys\n"
        "from server.config import settings\n"
        f"expected = {str(expected_root)!r}\n"
        "root, db = settings.resolved_deploy_root, settings.db_path\n"
        "bad = []\n"
        "if not root:\n"
        "    bad.append('deploy_root is unset, so this slot would report "
        "sha=null at /health and fail every switch health check')\n"
        "elif os.path.realpath(root) != os.path.realpath(expected):\n"
        "    bad.append(f'deploy_root {root!r} is not this deploy tree "
        "({expected!r})')\n"
        "if not os.path.isabs(db):\n"
        "    bad.append(f'db_path {db!r} is relative, so this slot would open "
        "its own empty database instead of the live one')\n"
        "if bad:\n"
        "    sys.stderr.write('; '.join(bad) + chr(10))\n"
        "    sys.exit(1)\n"
    )


def slot_env_problem(layout: "DeployLayout", slot: str) -> str | None:
    """Fail-closed pre-flip guard: the slot about to go live must be able to see
    runtime config. Returns None if it can, else a user-facing reason.

    Deliberately a filesystem check rather than a re-run of the stage probe.
    This executes on the switch path, immediately before the point of no
    return, and must not depend on spawning the target slot's interpreter.
    `stage_slot` already ran the full probe; what can still have changed
    between stage and switch is the config itself — a `.env` deleted, or a slot
    staged by a build that predates §3.2 and so never had one linked.

    It is worth guarding this cheaply because the failure is not recoverable:
    the rollback restarts the OLD server through the identical spawn path, so
    a config fault fails the rollback in exactly the same way it failed the
    switch, and the deploy ends in `rollback_incomplete` with nothing serving.

    Config supplied by exported env vars needs no `.env` at all, so an
    environment that already carries the deploy root is equally acceptable."""
    if os.environ.get("OWLERY_DEPLOY_ROOT") or os.environ.get("OCTOPUS_DEPLOY_ROOT"):
        return None
    slot_env = layout.slot_path(slot) / ENV_FILE
    canonical = layout.root / ENV_FILE
    # is_file() follows symlinks, so a dangling link fails here too.
    if slot_env.is_file():
        return None
    consequence = (
        "it would boot with default settings — opening an empty database "
        "beside its own directory instead of the live one, and reporting "
        "sha=null at /health so the switch could never pass its health check"
    )
    if canonical.is_file():
        return (
            f"slot {slot} has no readable {ENV_FILE} (expected a link to "
            f"{canonical}); {consequence}. Re-stage the slot, or run "
            "`owlery deploy init` to relink it."
        )
    return (
        f"no runtime config found: neither {slot_env} nor {canonical} exists, "
        f"and OWLERY_DEPLOY_ROOT is not exported; {consequence}."
    )


def _remove_slot(slot_dir: Path) -> None:
    """Remove leftover slot debris (dir, file, or symlink). Only ever called on
    a slot with no valid `current` pointing at it, so it never deletes a live
    installation — see `deploy_init`."""
    if slot_dir.is_symlink() or slot_dir.is_file():
        slot_dir.unlink()
    elif slot_dir.is_dir():
        shutil.rmtree(slot_dir)


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
    _seed_root_env(src, layout.root)

    existing = layout.current_slot()
    if existing is not None and layout.slot_path(existing).exists():
        # Already initialized. Do not touch the live slot's CODE; re-linking
        # `.env` is config-only, idempotent, and self-heals a tree initialized
        # before slots carried one (§3.2).
        for slot in SLOTS:
            if layout.slot_path(slot).is_dir():
                _link_slot_env(layout.root, layout.slot_path(slot))
        commit = _git_head(layout.slot_path(existing), runner)
        return _result(layout, existing, commit, already=True)

    commit = _git_head(src, runner)
    live, idle = "a", "b"
    # We only reach here with no valid `current` (the already-initialized branch
    # returned above), so nothing is live: any slot dir present is debris from a
    # prior init that died before the flip (e.g. a venv/build failure between the
    # clone and `switch_current`). Clear both so this re-run is truly idempotent
    # — a real `git clone` refuses a non-empty target. A live slot is never
    # reached here, so this can never delete a running instance's code (§3.1).
    for slot in (live, idle):
        _remove_slot(layout.slot_path(slot))
    _clone_slot(src, layout.slot_path(live), commit, runner)
    _link_slot_env(layout.root, layout.slot_path(live))
    _build_slot(layout.slot_path(live), runner)
    # Idle slot is cloned but not built — the first deploy_stage builds it (§3.1).
    _clone_slot(src, layout.slot_path(idle), commit, runner)
    _link_slot_env(layout.root, layout.slot_path(idle))
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
        f"  current: {result.root / CURRENT_LINK} -> {result.live_slot}\n"
        f"  config:  {result.root / ENV_FILE} (linked into every slot)\n\n"
        "Runtime settings come from that file — edit it there, not in a\n"
        "checkout: each slot carries only a link to it, and a slot that cannot\n"
        "read it is refused at stage and switch time. If you configure by\n"
        "environment instead, export it:\n"
        f"  export {result.env_hint}\n\n"
        "Start the server via `current`:\n"
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


# ------------------------------------------------------------- deploy_stage (§5)
#
# Staging prepares the idle slot completely while the running server is untouched
# (docs/plans/local-deploy.md §5). It is a pure filesystem/subprocess pipeline
# with no DB or op knowledge — the coordinator owns the op lifecycle, the global
# lock, and the `deployments` row; this only turns an idle slot into a runnable
# checkout of an exact sha, or reports which step failed with its full output.


@dataclass(frozen=True, slots=True)
class StageResult:
    """Outcome of the §5 pipeline. On failure, ``failed_step`` names the step and
    ``output`` is that command's full combined stdout+stderr (secret-free — the
    pipeline only runs git/venv/pip/bun/python against local paths, §10)."""

    ok: bool
    slot: str
    sha: str
    failed_step: str | None = None
    output: str = ""


# A staging runner runs ``argv`` in ``cwd`` under a per-subprocess wall-clock cap
# (``deploy_stage_timeout_seconds``, §9) and returns ``(returncode, combined
# output)`` — it never raises for a non-zero exit, so the pipeline can capture
# the failing step's output. Tests inject a fake that materializes each step's
# on-disk effect without a real clone/venv/build.
StageRunner = Callable[[list[str], Path, int], tuple[int, str]]


def _default_stage_runner(argv: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, f"command not found: {argv[0]}"
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(partial, bytes):  # text=True normally yields str
            partial = partial.decode("utf-8", "replace")
        return 124, f"timed out after {timeout}s: {' '.join(argv)}\n{partial}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def stage_slot(
    layout: DeployLayout,
    slot: str,
    *,
    repo_path: str,
    sha: str,
    timeout: int,
    runner: StageRunner = _default_stage_runner,
) -> StageResult:
    """Prepare ``slot`` into a runnable checkout of ``sha`` (docs/plans/
    local-deploy.md §5, steps 1-5). The caller resolves and owns the slot; this
    never touches ``current`` and never mutates the live slot by construction.

    Steps, each bounded by ``timeout`` and short-circuiting on the first
    non-zero exit:

      0. clone the board repo into the slot if it has no checkout yet — the idle
         slot may be staged lazily on the first deploy (§3.1);
      1. ``git fetch <repo_path> <sha>`` — bring the exact remote-verified
         object into the slot. ``repo_path`` is the configured remote URL, not
         an unchecked local worktree (§2);
      2. ``git checkout --detach <sha>`` — the exact sha, never a branch name
         that could move (§5.2);
      3. create the slot venv at its final path if absent, then
         ``pip install -e .`` (venvs are not relocatable — building in place is
         what keeps the flip skew-free, §3/§5.3);
      4. ``bun install`` + ``bun run build`` in the slot's ``web`` (§5.4);
      5. ``python -c "import server.main"`` — an import crash is caught here, at
         stage time, not at switch time (§5.5);
      6. ``config_probe`` — the slot must resolve usable runtime settings with
         its own interpreter and cwd (§5.6). A slot that cannot is worse than
         one that fails to build: it starts, answers /health, and quietly
         serves an empty database, and the switch that flips into it cannot be
         rolled back (§3.2).

    `.env` is linked into the slot right after the checkout, since a fresh git
    clone never carries one.
    """
    slot_dir = layout.slot_path(slot)

    # Fail closed if the idle slot is anything but a real directory or absent
    # (§13.9): a slot symlinked out of the tree — or a stray file — would let the
    # pipeline fetch/build/checkout into a path outside deploy_root. Only a real
    # dir (re-stage of an established slot) or a missing path (lazy first-deploy
    # clone) is allowed. is_dir()/exists() follow symlinks, so is_symlink() is
    # what actually pins the target inside the tree.
    if slot_dir.is_symlink() or (slot_dir.exists() and not slot_dir.is_dir()):
        return StageResult(
            ok=False, slot=slot, sha=sha, failed_step="slot_guard",
            output=(
                f"idle slot {slot_dir} is not a real directory (a symlink or "
                "file); refusing to stage outside the deploy tree"
            ),
        )

    venv_python = slot_dir / ".venv" / "bin" / "python"
    venv_pip = slot_dir / ".venv" / "bin" / "pip"
    web_dir = slot_dir / "web"

    # (name, argv, cwd) in order. Conditional steps are omitted when their
    # artifact already exists so re-staging an established slot skips clone/venv.
    steps: list[tuple[str, list[str], Path]] = []
    if not (slot_dir / ".git").is_dir():
        steps.append(("clone", ["git", "clone", repo_path, str(slot_dir)], layout.root))
    steps.append(("fetch", ["git", "fetch", repo_path, sha], slot_dir))
    steps.append(("checkout", ["git", "checkout", "--detach", sha], slot_dir))
    if not venv_python.exists():
        steps.append(("venv", [sys.executable, "-m", "venv", ".venv"], slot_dir))
    steps.append(("pip", [str(venv_pip), "install", "-e", "."], slot_dir))
    steps.append(("bun_install", ["bun", "install"], web_dir))
    steps.append(("build", ["bun", "run", "build"], web_dir))
    steps.append(("import_probe", [str(venv_python), "-c", "import server.main"], slot_dir))
    steps.append(
        ("config_probe", [str(venv_python), "-c", config_probe_source(layout.root)], slot_dir)
    )

    for name, argv, cwd in steps:
        rc, out = runner(argv, cwd, timeout)
        if rc != 0:
            return StageResult(ok=False, slot=slot, sha=sha, failed_step=name, output=out)
        if name == "checkout":
            # The clone/checkout above just materialized this slot from git, and
            # `.env` is gitignored — so the slot has no runtime config until we
            # link it (§3.2). Must happen before `config_probe` reads it, and
            # before anything else boots code in here.
            _link_slot_env(layout.root, slot_dir)
    return StageResult(ok=True, slot=slot, sha=sha)
