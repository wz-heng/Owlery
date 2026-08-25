from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from server.config import settings
from server.task_board import workspaces as ws_module
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


def _origin_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A source repo with a bare `origin` already advertising `main`."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    # Force the bare repo's HEAD symref to `main` regardless of the host's
    # `init.defaultBranch` — otherwise `ls-remote --symref origin HEAD`
    # advertises whatever the host defaulted to, not the branch pushed below.
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=bare, check=True)
    source = tmp_path / "repo"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "owlery@example.invalid"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Owlery Test"], cwd=source, check=True)
    (source / "tracked.txt").write_text("base")
    subprocess.run(["git", "add", "tracked.txt"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=source, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=source, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=source, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=source, check=True)
    return source, bare


def _rev_parse(repo: Path, rev: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", rev], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.asyncio
async def test_git_worktree_origin_base_ignores_dirty_and_diverged_local_head(
    task_roots, tmp_path: Path
):
    """repo-consolidation.md §3: with an origin remote, the basis is origin's
    default branch tip — never the local HEAD — and a dirty source repo no
    longer blocks prepare (fetch-success + dirty-source-still-prepares forms).
    """
    source, bare = _origin_repo(tmp_path)
    origin_head = _rev_parse(bare, "main")

    # Diverge and dirty the local checkout after pushing to origin.
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "local only"], cwd=source, check=True)
    (source / "tracked.txt").write_text("dirty and uncommitted")

    prepared = await prepare_workspace(
        mode="git_worktree", source_dir=str(source), task_id="t1", run_id="run1", attempt_no=1,
    )
    assert prepared.metadata["base_ref"] == "main"
    assert prepared.metadata["base_head"] == origin_head
    assert prepared.metadata["base_origin_degraded"] is False
    # The worktree is checked out at origin's tip content, not the dirty local one.
    assert (Path(prepared.path) / "tracked.txt").read_text() == "base"

    # The source repo itself is untouched — still dirty, never required to be clean.
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=source, check=True, capture_output=True, text=True
    ).stdout
    assert porcelain.strip() != ""


@pytest.mark.asyncio
async def test_git_worktree_origin_base_degrades_to_cached_tracking_ref(
    task_roots, tmp_path: Path
):
    """A live fetch failure (origin unreachable) falls back to the
    remote-tracking ref a prior successful prepare already cached — never to
    local HEAD — and records the degraded path in metadata."""
    source, bare = _origin_repo(tmp_path)
    origin_head = _rev_parse(bare, "main")

    live = await prepare_workspace(
        mode="git_worktree", source_dir=str(source), task_id="t1", run_id="run1", attempt_no=1,
    )
    assert live.metadata["base_origin_degraded"] is False

    # Origin becomes unreachable; only the local remote-tracking ref survives.
    subprocess.run(
        ["git", "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git")],
        cwd=source, check=True,
    )

    degraded = await prepare_workspace(
        mode="git_worktree", source_dir=str(source), task_id="t1", run_id="run2", attempt_no=2,
    )
    assert degraded.metadata["base_ref"] == "main"
    assert degraded.metadata["base_head"] == origin_head
    assert degraded.metadata["base_origin_degraded"] is True


@pytest.mark.asyncio
async def test_git_worktree_origin_base_falls_back_when_fetch_fails_after_ls_remote_succeeds(
    task_roots, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A live `ls-remote --symref` success does not guarantee the subsequent
    `fetch` also succeeds (transient network blip). That must still degrade
    to a prior successful prepare's cached tracking ref, not reject outright
    just because the branch name it just resolved has no fresh fetch."""
    source, bare = _origin_repo(tmp_path)
    origin_head = _rev_parse(bare, "main")

    live = await prepare_workspace(
        mode="git_worktree", source_dir=str(source), task_id="t1", run_id="run1", attempt_no=1,
    )
    assert live.metadata["base_origin_degraded"] is False

    real_run = ws_module._run

    async def flaky_fetch(*argv, **kwargs):
        if argv[:2] == ("git", "fetch"):
            return 1, "", "simulated network failure mid-fetch"
        return await real_run(*argv, **kwargs)

    monkeypatch.setattr(ws_module, "_run", flaky_fetch)

    degraded = await prepare_workspace(
        mode="git_worktree", source_dir=str(source), task_id="t1", run_id="run2", attempt_no=2,
    )
    assert degraded.metadata["base_ref"] == "main"
    assert degraded.metadata["base_head"] == origin_head
    assert degraded.metadata["base_origin_degraded"] is True


@pytest.mark.asyncio
async def test_git_worktree_origin_base_rejects_when_unreachable_and_uncached(
    task_roots, tmp_path: Path
):
    """Neither a live fetch nor a cached remote-tracking ref is available —
    the only condition under which an origin-remote repo's prepare is
    rejected outright (never silently falling back to local HEAD)."""
    source, bare = _origin_repo(tmp_path)
    # Break origin before any prepare ever ran against this repo, so no
    # remote-tracking HEAD cache was ever written by a live resolution.
    subprocess.run(
        ["git", "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git")],
        cwd=source, check=True,
    )

    with pytest.raises(WorkspaceError, match="origin base is unavailable"):
        await prepare_workspace(
            mode="git_worktree", source_dir=str(source), task_id="t1", run_id="run1", attempt_no=1,
        )
