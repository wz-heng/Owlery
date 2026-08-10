"""Dual-slot layout, `owlery deploy init`, and the fail-closed guard
(docs/plans/local-deploy.md §3/§3.1, step 1 of §17).

All hermetic: temp dirs and a fake command runner that simulates git/venv/bun
by materializing the filesystem artifacts each step would produce. No real
clone, venv, or build — and never a real production path.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from server.cli import build_parser
from server.config import Settings
from server.deploy import (
    ENV_FILE,
    REASON_NOT_INITIALIZED,
    REASON_NOT_VIA_CURRENT,
    DeployError,
    DeployLayout,
    config_probe_source,
    deploy_init,
    deploy_precheck,
    format_init_report,
    stage_slot,
)

FAKE_SHA = "0123456789abcdef0123456789abcdef01234567"


class FakeRunner:
    """Simulates deploy_init's subprocess steps by creating what each would
    leave on disk, so the whole init flow runs offline against temp dirs."""

    def __init__(self, head: str = FAKE_SHA) -> None:
        self.head = head
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, argv: list[str], cwd: Path) -> str:
        self.calls.append((argv, cwd))
        if argv[:2] == ["git", "rev-parse"]:
            return self.head + "\n"
        if argv[:2] == ["git", "clone"]:
            dst = Path(argv[3])
            (dst / ".git").mkdir(parents=True, exist_ok=True)
            (dst / "web").mkdir(parents=True, exist_ok=True)
            return ""
        if argv[:2] == ["git", "checkout"]:
            return ""
        if argv[1:3] == ["-m", "venv"]:
            bindir = cwd / ".venv" / "bin"
            bindir.mkdir(parents=True, exist_ok=True)
            for exe in ("owlery", "python", "pip"):
                (bindir / exe).touch()
            return ""
        if Path(argv[0]).name == "pip":
            return ""
        if argv[0] == "bun" and argv[1:2] == ["run"]:
            dist = cwd / "dist"
            dist.mkdir(parents=True, exist_ok=True)
            (dist / "index.html").touch()
            return ""
        if argv[0] == "bun":
            return ""
        raise AssertionError(f"unexpected command: {argv}")


class StageRunnerSpy:
    """Simulates `stage_slot`'s subprocess steps by materializing what each
    would leave on disk, and records whether the slot's `.env` was already in
    place when the config probe ran."""

    def __init__(self, *, config_probe_error: str | None = None) -> None:
        self.config_probe_error = config_probe_error
        self.steps: list[str] = []
        self.env_present_at_config_probe: bool | None = None

    def __call__(self, argv: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
        if argv[:2] == ["git", "clone"]:
            dst = Path(argv[3])
            (dst / ".git").mkdir(parents=True, exist_ok=True)
            (dst / "web").mkdir(exist_ok=True)
            self.steps.append("clone")
        elif argv[:2] == ["git", "fetch"]:
            self.steps.append("fetch")
        elif argv[:2] == ["git", "checkout"]:
            self.steps.append("checkout")
        elif argv[1:3] == ["-m", "venv"]:
            bindir = cwd / ".venv" / "bin"
            bindir.mkdir(parents=True, exist_ok=True)
            for exe in ("python", "pip", "owlery"):
                (bindir / exe).touch()
            self.steps.append("venv")
        elif Path(argv[0]).name == "pip":
            self.steps.append("pip")
        elif argv[0] == "bun":
            self.steps.append("bun")
        elif argv[1:2] == ["-c"] and "resolved_deploy_root" in argv[2]:
            self.steps.append("config_probe")
            self.env_present_at_config_probe = (cwd / ENV_FILE).is_symlink()
            if self.config_probe_error:
                return 1, self.config_probe_error
        elif argv[1:2] == ["-c"]:
            self.steps.append("import_probe")
        else:
            raise AssertionError(f"unexpected command: {argv}")
        return 0, "ok"


def _fake_source(tmp_path: Path, *, env_body: str | None = None) -> Path:
    src = tmp_path / "checkout"
    (src / ".git").mkdir(parents=True)
    (src / "web").mkdir()
    if env_body is not None:
        (src / ENV_FILE).write_text(env_body)
    return src


# --------------------------------------------------------------------- layout


def test_switch_current_is_atomic_relative_symlink(tmp_path: Path):
    layout = DeployLayout.at(tmp_path)
    layout.slot_path("a").mkdir()
    layout.slot_path("b").mkdir()

    layout.switch_current("a")
    assert layout.current_link.is_symlink()
    # Relative target keeps the tree relocatable.
    import os

    assert os.readlink(layout.current_link) == "a"
    assert layout.current_slot() == "a"

    # Re-flipping overwrites the existing link with no absent/dangling window.
    layout.switch_current("b")
    assert layout.current_slot() == "b"
    # No staging leftovers.
    assert not (tmp_path / ".current.b.staged").exists()


def test_idle_slot_tracks_current(tmp_path: Path):
    layout = DeployLayout.at(tmp_path)
    for slot in ("a", "b"):
        layout.slot_path(slot).mkdir()
    assert layout.idle_slot() == "b"  # nothing current yet → stage 'a', idle 'b'
    layout.switch_current("a")
    assert layout.idle_slot() == "b"
    layout.switch_current("b")
    assert layout.idle_slot() == "a"


def test_current_slot_none_when_dangling_or_foreign(tmp_path: Path):
    import os

    layout = DeployLayout.at(tmp_path)
    assert layout.current_slot() is None  # no link at all
    os.symlink("a", layout.current_link)  # dangling (slot a absent)
    assert layout.current_slot() is None
    layout.current_link.unlink()
    os.symlink(str(tmp_path / "elsewhere"), layout.current_link)
    (tmp_path / "elsewhere").mkdir()
    assert layout.current_slot() is None  # resolves outside the two slots


def test_slot_path_rejects_unknown_slot(tmp_path: Path):
    with pytest.raises(ValueError):
        DeployLayout.at(tmp_path).slot_path("c")


def test_current_slot_rejects_symlinked_slot(tmp_path: Path):
    """A slot that is itself a symlink (possibly out of the tree) must read as
    'no current' — trusting it would let `current -> a -> /outside` pass every
    guard and deploy outside deploy_root (invariant §13.9), the opposite of
    fail-closed."""
    import os

    layout = DeployLayout.at(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, layout.slot_path("a"))  # slot a is a symlink out of tree
    os.symlink("a", layout.current_link)
    assert layout.current_slot() is None


def test_switch_current_rejects_missing_or_symlinked_slot(tmp_path: Path):
    """The one sanctioned flip point validates its target rather than trusting
    the caller: a missing or symlinked slot is refused, and no dangling/foreign
    `current` is left behind."""
    import os

    layout = DeployLayout.at(tmp_path)
    with pytest.raises(DeployError):
        layout.switch_current("a")  # slot a does not exist

    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, layout.slot_path("b"))  # slot b is a symlink out of tree
    with pytest.raises(DeployError):
        layout.switch_current("b")

    assert not layout.current_link.is_symlink()
    assert not (tmp_path / ".current.a.staged").exists()
    assert not (tmp_path / ".current.b.staged").exists()


# ------------------------------------------------------------------ deploy init


def test_deploy_init_creates_full_layout(tmp_path: Path):
    src = _fake_source(tmp_path)
    root = tmp_path / "deploy"
    runner = FakeRunner()

    result = deploy_init(root, src, runner=runner)

    layout = DeployLayout.at(root)
    assert layout.slot_path("a").is_dir()
    assert layout.slot_path("b").is_dir()
    assert layout.current_slot() == "a"
    assert layout.journal_path.is_file()
    assert layout.snapshots_path.is_dir()
    # Slot a was built (venv + dist); slot b was only cloned.
    assert (layout.slot_path("a") / ".venv" / "bin" / "owlery").exists()
    assert (layout.slot_path("a") / "web" / "dist" / "index.html").exists()
    assert not (layout.slot_path("b") / ".venv").exists()

    assert result.live_slot == "a"
    assert result.commit == FAKE_SHA
    assert result.already_initialized is False
    assert result.start_command == f"{root / 'current' / '.venv' / 'bin' / 'owlery'} serve"
    assert result.env_hint == f"OWLERY_DEPLOY_ROOT={root}"


def test_deploy_init_is_idempotent_and_never_reclones(tmp_path: Path):
    src = _fake_source(tmp_path)
    root = tmp_path / "deploy"
    deploy_init(root, src, runner=FakeRunner())

    # A second run must not clone/build again — the live slot stays untouched.
    second_runner = FakeRunner()
    result = deploy_init(root, src, runner=second_runner)

    assert result.already_initialized is True
    assert result.live_slot == "a"
    clone_calls = [c for c in second_runner.calls if c[0][:2] == ["git", "clone"]]
    venv_calls = [c for c in second_runner.calls if c[0][1:3] == ["-m", "venv"]]
    assert clone_calls == []
    assert venv_calls == []


class FailingBuildRunner(FakeRunner):
    """Clones fine but fails the venv build — an init that dies AFTER the slot-a
    clone but BEFORE the `current` flip, leaving a half-built slot-a dir."""

    def __call__(self, argv: list[str], cwd: Path) -> str:
        if argv[1:3] == ["-m", "venv"]:
            raise DeployError("simulated venv failure")
        return super().__call__(argv, cwd)


def test_deploy_init_recovers_from_partial_failure(tmp_path: Path):
    src = _fake_source(tmp_path)
    root = tmp_path / "deploy"
    layout = DeployLayout.at(root)

    # First run dies mid slot-a build: the clone left <root>/a on disk, but the
    # flip never happened, so there is no valid `current`.
    with pytest.raises(DeployError):
        deploy_init(root, src, runner=FailingBuildRunner())
    assert layout.slot_path("a").exists()  # debris from the failed run
    assert layout.current_slot() is None  # never flipped → not initialized

    # A re-run must SUCCEED, not choke on the pre-existing slot-a dir (a real
    # `git clone` refuses a non-empty target). The debris is cleared and rebuilt.
    result = deploy_init(root, src, runner=FakeRunner())
    assert result.already_initialized is False
    assert result.live_slot == "a"
    assert layout.current_slot() == "a"
    assert (layout.slot_path("a") / ".venv" / "bin" / "owlery").exists()


def test_deploy_init_report_names_the_start_command(tmp_path: Path):
    src = _fake_source(tmp_path)
    root = tmp_path / "deploy"
    result = deploy_init(root, src, runner=FakeRunner())
    report = format_init_report(result)
    assert "initialized" in report.lower()
    assert result.start_command in report
    assert result.env_hint in report


def test_deploy_init_rejects_non_git_source(tmp_path: Path):
    src = tmp_path / "not-a-repo"
    src.mkdir()
    with pytest.raises(DeployError, match="not a git checkout"):
        deploy_init(tmp_path / "deploy", src, runner=FakeRunner())


# --------------------------------------------------- slot runtime config (.env)
#
# A slot is only "self-sufficient" (§3) if it can resolve its runtime config.
# It cannot inherit one from the environment: pydantic reads `.env` into
# Settings WITHOUT exporting to os.environ, so the switcher's "same env"
# handoff carries nothing a `.env` supplied. And `.env` is gitignored, so the
# slot's own checkout never brings one. The deploy root owns the canonical
# file and every slot links to it.


def test_deploy_init_seeds_root_env_and_links_both_slots(tmp_path: Path):
    src = _fake_source(tmp_path, env_body="OWLERY_DB_PATH=/abs/live.db\n")
    root = tmp_path / "deploy"

    deploy_init(root, src, runner=FakeRunner())

    canonical = root / ENV_FILE
    assert canonical.is_file()
    assert canonical.read_text() == "OWLERY_DB_PATH=/abs/live.db\n"
    # Secrets live here (auth token, bridge keys) — owner-only.
    assert canonical.stat().st_mode & 0o077 == 0

    layout = DeployLayout.at(root)
    for slot in ("a", "b"):
        link = layout.slot_path(slot) / ENV_FILE
        assert link.is_symlink(), f"slot {slot} has no .env link"
        # Relative, so the whole tree stays relocatable.
        assert os.readlink(link) == f"../{ENV_FILE}"
        assert link.read_text() == "OWLERY_DB_PATH=/abs/live.db\n"


def test_deploy_init_never_overwrites_an_existing_root_env(tmp_path: Path):
    src = _fake_source(tmp_path, env_body="OWLERY_DB_PATH=/from/source.db\n")
    root = tmp_path / "deploy"
    root.mkdir(parents=True)
    (root / ENV_FILE).write_text("OWLERY_DB_PATH=/already/live.db\n")

    deploy_init(root, src, runner=FakeRunner())

    # The deploy root's config is authoritative once it exists — re-running
    # init must never clobber the live instance's settings with a checkout's.
    assert (root / ENV_FILE).read_text() == "OWLERY_DB_PATH=/already/live.db\n"


def test_deploy_init_without_source_env_leaves_no_dangling_links(tmp_path: Path):
    # Config supplied purely by exported env vars is legitimate; init must not
    # invent a `.env`, nor leave slot links pointing at a file that isn't there.
    src = _fake_source(tmp_path)
    root = tmp_path / "deploy"

    deploy_init(root, src, runner=FakeRunner())

    assert not (root / ENV_FILE).exists()
    layout = DeployLayout.at(root)
    for slot in ("a", "b"):
        assert not (layout.slot_path(slot) / ENV_FILE).is_symlink()


# ------------------------------------------------------------- the config probe
#
# These run the probe for real, in a subprocess, against a temp `.env` — the
# exact mechanism a staged slot uses. A stubbed probe could not have caught
# the relative-db_path bug, because the bug IS the resolution.


def _run_probe(cwd: Path, expected_root: Path) -> tuple[int, str]:
    """Run the probe with the slot's cwd and a clean env (no ambient OWLERY_*
    leaking in from the developer's shell)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("OWLERY_", "OCTOPUS_"))
    }
    proc = subprocess.run(
        [sys.executable, "-c", config_probe_source(expected_root)],
        cwd=str(cwd), capture_output=True, text=True, env=env, timeout=120,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_config_probe_passes_on_a_correctly_configured_slot(tmp_path: Path):
    root = tmp_path / "deploy"
    slot = root / "a"
    slot.mkdir(parents=True)
    (slot / ENV_FILE).write_text(
        f"OWLERY_DEPLOY_ROOT={root}\nOWLERY_DB_PATH={tmp_path / 'live.db'}\n"
    )

    rc, out = _run_probe(slot, root)

    assert rc == 0, out


def test_config_probe_rejects_a_relative_db_path(tmp_path: Path):
    # The exact production failure: db_path resolves against the server's cwd,
    # so each slot silently opens its OWN empty database instead of the live one.
    root = tmp_path / "deploy"
    slot = root / "a"
    slot.mkdir(parents=True)
    (slot / ENV_FILE).write_text(f"OWLERY_DEPLOY_ROOT={root}\nOWLERY_DB_PATH=owlery.db\n")

    rc, out = _run_probe(slot, root)

    assert rc != 0
    assert "db_path" in out and "relative" in out


def test_config_probe_rejects_an_unresolvable_deploy_root(tmp_path: Path):
    # deploy_root unset ⇒ /health reports sha=null ⇒ the switcher can never
    # match the handoff sha ⇒ every switch health-times-out AND its rollback
    # (which restarts the old server the same way) fails identically.
    root = tmp_path / "deploy"
    slot = root / "a"
    slot.mkdir(parents=True)
    (slot / ENV_FILE).write_text(f"OWLERY_DB_PATH={tmp_path / 'live.db'}\n")

    rc, out = _run_probe(slot, root)

    assert rc != 0
    assert "deploy_root" in out


def test_stage_slot_links_env_then_probes_the_config(tmp_path: Path):
    root = tmp_path / "deploy"
    root.mkdir(parents=True)
    (root / ENV_FILE).write_text("OWLERY_DB_PATH=/abs/live.db\n")
    layout = DeployLayout.at(root)
    runner = StageRunnerSpy()

    result = stage_slot(
        layout, "b", repo_path=str(tmp_path / "repo"), sha=FAKE_SHA,
        timeout=60, runner=runner,
    )

    assert result.ok, result.output
    # A fresh clone carries no `.env` (gitignored) — stage must supply it.
    link = layout.slot_path("b") / ENV_FILE
    assert link.is_symlink() and os.readlink(link) == f"../{ENV_FILE}"
    assert link.read_text() == "OWLERY_DB_PATH=/abs/live.db\n"
    # …and it must already be in place by the time the probe reads it.
    assert runner.env_present_at_config_probe is True


def test_stage_slot_fails_closed_when_the_slot_cannot_resolve_config(tmp_path: Path):
    root = tmp_path / "deploy"
    root.mkdir(parents=True)
    layout = DeployLayout.at(root)
    runner = StageRunnerSpy(config_probe_error="db_path 'owlery.db' is relative")

    result = stage_slot(
        layout, "b", repo_path=str(tmp_path / "repo"), sha=FAKE_SHA,
        timeout=60, runner=runner,
    )

    # Fail the STAGE, so the slot is never eligible to be switched into. A slot
    # that boots but serves an empty DB cannot be recovered by rollback.
    assert result.ok is False
    assert result.failed_step == "config_probe"
    assert "relative" in result.output


def test_stage_slot_leaves_no_dangling_env_link_without_a_root_env(tmp_path: Path):
    root = tmp_path / "deploy"
    root.mkdir(parents=True)
    layout = DeployLayout.at(root)

    result = stage_slot(
        layout, "b", repo_path=str(tmp_path / "repo"), sha=FAKE_SHA,
        timeout=60, runner=StageRunnerSpy(),
    )

    # Exported-env configuration is valid; the probe (not the link) is the
    # arbiter, so an absent root `.env` must not leave a broken symlink behind.
    assert result.ok, result.output
    assert not (layout.slot_path("b") / ENV_FILE).is_symlink()


def test_config_probe_rejects_a_slot_pointing_at_a_foreign_deploy_root(tmp_path: Path):
    root = tmp_path / "deploy"
    slot = root / "a"
    slot.mkdir(parents=True)
    (slot / ENV_FILE).write_text(
        f"OWLERY_DEPLOY_ROOT={tmp_path / 'somewhere-else'}\n"
        f"OWLERY_DB_PATH={tmp_path / 'live.db'}\n"
    )

    rc, out = _run_probe(slot, root)

    assert rc != 0
    assert "deploy_root" in out


# ------------------------------------------------------------ fail-closed guard


def _init_layout(tmp_path: Path) -> DeployLayout:
    layout = DeployLayout.at(tmp_path / "deploy")
    layout.root.mkdir(parents=True)
    for slot in ("a", "b"):
        layout.slot_path(slot).mkdir()
    layout.switch_current("a")
    return layout


def test_precheck_blocks_when_deploy_root_unset():
    verdict = deploy_precheck(Settings(deploy_root=""))
    assert not verdict.ok
    assert verdict.reason_kind == REASON_NOT_INITIALIZED
    assert "deploy init" in verdict.message


def test_precheck_blocks_when_layout_missing(tmp_path: Path):
    root = tmp_path / "deploy"
    root.mkdir()
    verdict = deploy_precheck(Settings(deploy_root=str(root)))
    assert not verdict.ok
    assert verdict.reason_kind == REASON_NOT_INITIALIZED
    assert "deploy init" in verdict.message


def test_precheck_blocks_debug_server(tmp_path: Path):
    layout = _init_layout(tmp_path)
    verdict = deploy_precheck(
        Settings(deploy_root=str(layout.root), debug=True),
        server_root=layout.slot_path("a"),
    )
    assert not verdict.ok
    assert verdict.reason_kind == REASON_NOT_VIA_CURRENT
    assert "debug" in verdict.message.lower()


def test_precheck_blocks_when_not_running_via_current(tmp_path: Path):
    layout = _init_layout(tmp_path)
    verdict = deploy_precheck(
        Settings(deploy_root=str(layout.root)),
        server_root=layout.slot_path("b"),  # code lives in the idle slot
    )
    assert not verdict.ok
    assert verdict.reason_kind == REASON_NOT_VIA_CURRENT
    assert "Restart" in verdict.message


def test_precheck_ok_when_running_via_current(tmp_path: Path):
    layout = _init_layout(tmp_path)
    verdict = deploy_precheck(
        Settings(deploy_root=str(layout.root)),
        server_root=layout.slot_path("a"),  # current -> a
    )
    assert verdict.ok
    assert verdict.reason_kind is None


# --------------------------------------------------------------------- CLI wiring


def test_cli_parses_deploy_init():
    args = build_parser().parse_args(["deploy", "init", "--root", "/x", "--from", "/y"])
    assert args.command == "deploy"
    assert args.deploy_command == "init"
    assert args.root == "/x"
    assert args.from_checkout == "/y"


def test_cli_deploy_init_from_defaults_to_none():
    args = build_parser().parse_args(["deploy", "init", "--root", "/x"])
    assert args.from_checkout is None
