from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from server.config import settings
from server.task_board.workspaces import (
    WorkspaceError,
    capture_artifacts,
    cleanup_private_workspace,
    inspect_git_workspace,
    prepare_workspace,
)


@pytest.fixture
def task_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    workspaces = tmp_path / "task-workspaces"
    artifacts = tmp_path / "task-artifacts"
    workspaces.mkdir()
    artifacts.mkdir()
    monkeypatch.setattr(settings, "task_workspaces_dir", str(workspaces))
    monkeypatch.setattr(settings, "task_artifacts_dir", str(artifacts))
    return workspaces, artifacts


@pytest.mark.asyncio
async def test_shared_workspace_uses_canonical_source(task_roots, tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    prepared = await prepare_workspace(
        mode="shared", source_dir=str(source), task_id="task1", run_id="run1", attempt_no=1
    )
    assert prepared.path == str(source.resolve())
    assert prepared.metadata == {}


@pytest.mark.asyncio
async def test_copy_workspace_is_private_and_preserves_symlinks(task_roots, tmp_path: Path):
    workspaces, _ = task_roots
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.txt").write_text("hello")
    (source / "link").symlink_to("note.txt")

    prepared = await prepare_workspace(
        mode="copy", source_dir=str(source), task_id="task1", run_id="run1", attempt_no=1
    )
    destination = workspaces / "task1" / "run1"
    assert prepared.path == str(destination)
    assert (destination / "note.txt").read_text() == "hello"
    assert (destination / "link").is_symlink()

    with pytest.raises(WorkspaceError, match="already exists"):
        await prepare_workspace(
            mode="copy", source_dir=str(source), task_id="task1", run_id="run1", attempt_no=1
        )


@pytest.mark.asyncio
async def test_artifact_capture_hashes_and_rejects_symlink_escape(task_roots, tmp_path: Path):
    _, artifacts_root = task_roots
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    content = b"durable evidence\n"
    (workspace / "report.txt").write_bytes(content)

    captured = await capture_artifacts(
        workspace=str(workspace),
        task_id="task1",
        run_id="run1",
        artifacts=[{"path": "report.txt", "name": "evidence.txt"}],
    )
    assert len(captured) == 1
    assert captured[0].sha256 == hashlib.sha256(content).hexdigest()
    assert captured[0].size == len(content)
    assert Path(captured[0].stored_path) == artifacts_root / "task1" / "run1" / "evidence.txt"

    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (workspace / "escape").symlink_to(outside)
    with pytest.raises(WorkspaceError, match="escapes"):
        await capture_artifacts(
            workspace=str(workspace),
            task_id="task1",
            run_id="run2",
            artifacts=[{"path": "escape"}],
        )


@pytest.mark.asyncio
async def test_cleanup_refuses_root_and_shared_paths(task_roots, tmp_path: Path):
    workspaces, _ = task_roots
    attempt = workspaces / "task1" / "run1"
    attempt.mkdir(parents=True)
    await cleanup_private_workspace(str(attempt))
    assert not attempt.exists()

    with pytest.raises(WorkspaceError, match="Refusing"):
        await cleanup_private_workspace(str(workspaces))
    shared = tmp_path / "shared"
    shared.mkdir()
    with pytest.raises(WorkspaceError, match="Refusing"):
        await cleanup_private_workspace(str(shared))


@pytest.mark.asyncio
async def test_git_worktree_requires_clean_repo_and_records_state(task_roots, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "owlery@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Owlery Test"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("base")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)

    prepared = await prepare_workspace(
        mode="git_worktree",
        source_dir=str(repo),
        task_id="abc123",
        run_id="run1",
        attempt_no=2,
    )
    assert prepared.metadata["branch"] == "owlery/task-abc123-run-2"
    state = await inspect_git_workspace(prepared.path)
    assert state["branch"] == "owlery/task-abc123-run-2"
    assert state["porcelain"] == ""

    (repo / "tracked.txt").write_text("dirty")
    with pytest.raises(WorkspaceError, match="clean"):
        await prepare_workspace(
            mode="git_worktree",
            source_dir=str(repo),
            task_id="abc124",
            run_id="run2",
            attempt_no=1,
        )
