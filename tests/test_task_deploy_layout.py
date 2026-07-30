"""Dual-slot layout, `owlery deploy init`, and the fail-closed guard
(docs/plans/local-deploy.md §3/§3.1, step 1 of §17).

All hermetic: temp dirs and a fake command runner that simulates git/venv/bun
by materializing the filesystem artifacts each step would produce. No real
clone, venv, or build — and never a real production path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from server.cli import build_parser
from server.config import Settings
from server.deploy import (
    REASON_NOT_INITIALIZED,
    REASON_NOT_VIA_CURRENT,
    DeployError,
    DeployLayout,
    deploy_init,
    deploy_precheck,
    format_init_report,
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


def _fake_source(tmp_path: Path) -> Path:
    src = tmp_path / "checkout"
    (src / ".git").mkdir(parents=True)
    (src / "web").mkdir()
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
