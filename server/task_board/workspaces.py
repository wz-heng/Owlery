"""Safe workspace and artifact handling for Task Board attempts."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import settings

MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
MAX_ARTIFACT_TOTAL_BYTES = 100 * 1024 * 1024


class WorkspaceError(RuntimeError):
    """A workspace or artifact request failed validation."""


@dataclass(frozen=True)
class PreparedWorkspace:
    mode: str
    path: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class CapturedArtifact:
    name: str
    stored_path: str
    source_path: str
    mime_type: str | None
    size: int
    sha256: str


def _inside(path: Path, root: Path, *, allow_root: bool = False) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return allow_root or bool(rel.parts)


def _private_attempt_path(task_id: str, run_id: str) -> Path:
    root = Path(settings.resolved_task_workspaces_dir).resolve(strict=False)
    return root / task_id / run_id


async def _run(
    *argv: str, cwd: str | Path | None = None, timeout: float = 30.0
) -> tuple[int, str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except FileNotFoundError as exc:
        raise WorkspaceError(f"Required command is unavailable: {argv[0]}") from exc
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise WorkspaceError(f"Command timed out: {' '.join(argv)}") from exc
    return (
        process.returncode or 0,
        stdout.decode(errors="replace").strip(),
        stderr.decode(errors="replace").strip(),
    )


def _validate_source(source_dir: str) -> Path:
    source = Path(source_dir).expanduser().resolve(strict=True)
    if not source.is_dir():
        raise WorkspaceError(f"Workspace source is not a directory: {source_dir}")
    return source


async def prepare_workspace(
    *,
    mode: str,
    source_dir: str,
    task_id: str,
    run_id: str,
    attempt_no: int,
) -> PreparedWorkspace:
    """Prepare a run workspace without ever deleting an existing attempt."""
    source = _validate_source(source_dir)
    if mode == "shared":
        return PreparedWorkspace(mode=mode, path=str(source), metadata={})
    if mode not in {"copy", "git_worktree"}:
        raise WorkspaceError(f"Unknown workspace mode: {mode}")

    destination = _private_attempt_path(task_id, run_id)
    root = Path(settings.resolved_task_workspaces_dir).resolve(strict=False)
    if not _inside(destination.resolve(strict=False), root):
        raise WorkspaceError("Task workspace path escaped its configured root")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise WorkspaceError(f"Task workspace already exists: {destination}")

    if mode == "copy":
        try:
            await asyncio.to_thread(
                shutil.copytree, source, destination, symlinks=True
            )
        except Exception:
            # A partial copy is not an inspectable run workspace. The exact,
            # validated destination is safe to remove; no glob/root deletion.
            if destination.exists() and _inside(destination.resolve(strict=False), root):
                await asyncio.to_thread(shutil.rmtree, destination, True)
            raise
        return PreparedWorkspace(mode=mode, path=str(destination), metadata={})

    rc, top, err = await _run("git", "rev-parse", "--show-toplevel", cwd=source)
    if rc or not top:
        raise WorkspaceError(err or "git_worktree requires a Git repository")
    repo = Path(top).resolve(strict=True)
    rc, porcelain, err = await _run("git", "status", "--porcelain", cwd=repo)
    if rc:
        raise WorkspaceError(err or "Unable to inspect Git status")
    if porcelain:
        raise WorkspaceError("git_worktree requires a clean source repository")

    branch = f"owlery/task-{task_id}-run-{attempt_no}"
    rc, _, err = await _run(
        "git", "worktree", "add", "-b", branch, str(destination), "HEAD", cwd=repo
    )
    if rc:
        if destination.exists() and _inside(destination.resolve(strict=False), root):
            await asyncio.to_thread(shutil.rmtree, destination, True)
        raise WorkspaceError(err or "Unable to create Git worktree")
    rc, head, err = await _run("git", "rev-parse", "HEAD", cwd=destination)
    if rc:
        raise WorkspaceError(err or "Unable to inspect created Git worktree")
    return PreparedWorkspace(
        mode=mode,
        path=str(destination),
        metadata={"branch": branch, "base_head": head, "repository": str(repo)},
    )


async def inspect_git_workspace(path: str) -> dict[str, str]:
    """Return terminal Git evidence for a worktree run."""
    rc, branch, err = await _run("git", "branch", "--show-current", cwd=path)
    if rc:
        raise WorkspaceError(err or "Unable to inspect Git branch")
    rc, head, err = await _run("git", "rev-parse", "HEAD", cwd=path)
    if rc:
        raise WorkspaceError(err or "Unable to inspect Git HEAD")
    rc, status, err = await _run("git", "status", "--porcelain", cwd=path)
    if rc:
        raise WorkspaceError(err or "Unable to inspect Git status")
    return {"branch": branch, "head": head, "porcelain": status}


def _artifact_source(workspace: str, requested: str) -> tuple[Path, str]:
    root = Path(workspace).expanduser().resolve(strict=True)
    if Path(requested).is_absolute():
        raise WorkspaceError("Artifact paths must be relative to the run workspace")
    candidate = (root / requested).resolve(strict=True)
    if not _inside(candidate, root):
        raise WorkspaceError(f"Artifact escapes the run workspace: {requested}")
    if not candidate.is_file():
        raise WorkspaceError(f"Artifact is not a regular file: {requested}")
    return candidate, candidate.relative_to(root).as_posix()


def _copy_artifact(
    *, source: Path, destination: Path
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".artifact-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            while chunk := input_file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_ARTIFACT_BYTES:
                    raise WorkspaceError("Artifact grew beyond the 25 MiB limit while copying")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp_name, destination)
        dir_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    return size, digest.hexdigest()


async def capture_artifacts(
    *,
    workspace: str,
    task_id: str,
    run_id: str,
    artifacts: list[dict[str, str]],
) -> list[CapturedArtifact]:
    """Validate and durably copy worker-declared artifacts."""
    artifact_root = Path(settings.resolved_task_artifacts_dir).resolve(strict=False)
    destination_root = artifact_root / task_id / run_id
    if not _inside(destination_root.resolve(strict=False), artifact_root):
        raise WorkspaceError("Artifact destination escaped its configured root")

    seen: set[str] = set()
    prepared: list[tuple[str, Path, str]] = []
    declared_bytes = 0
    for item in artifacts:
        requested = str(item.get("path", "")).strip()
        if not requested:
            raise WorkspaceError("Every artifact requires a path")
        source, source_path = _artifact_source(workspace, requested)
        source_size = source.stat().st_size
        if source_size > MAX_ARTIFACT_BYTES:
            raise WorkspaceError(
                f"Artifact exceeds {MAX_ARTIFACT_BYTES // (1024 * 1024)} MiB: {requested}"
            )
        declared_bytes += source_size
        if declared_bytes > MAX_ARTIFACT_TOTAL_BYTES:
            raise WorkspaceError("Declared artifacts exceed the 100 MiB run limit")
        name = str(item.get("name") or source.name).strip()
        if not name or name in {".", ".."} or Path(name).name != name:
            raise WorkspaceError(f"Invalid artifact name: {name!r}")
        if name in seen:
            raise WorkspaceError(f"Duplicate artifact name: {name}")
        seen.add(name)
        prepared.append((name, source, source_path))

    captured: list[CapturedArtifact] = []
    try:
        for name, source, source_path in prepared:
            destination = destination_root / name
            if destination.exists() or destination.is_symlink():
                raise WorkspaceError(f"Artifact already exists: {name}")
            size, digest = await asyncio.to_thread(
                _copy_artifact, source=source, destination=destination
            )
            captured.append(
                CapturedArtifact(
                    name=name,
                    stored_path=str(destination),
                    source_path=source_path,
                    mime_type=mimetypes.guess_type(name)[0],
                    size=size,
                    sha256=digest,
                )
            )
    except Exception:
        await discard_captured_artifacts(captured)
        raise
    return captured


async def discard_captured_artifacts(artifacts: list[CapturedArtifact]) -> None:
    """Remove uncommitted captures after a terminal repository CAS loses."""
    root = Path(settings.resolved_task_artifacts_dir).resolve(strict=False)
    for artifact in artifacts:
        target = Path(artifact.stored_path).resolve(strict=False)
        if _inside(target, root):
            await asyncio.to_thread(target.unlink, missing_ok=True)


async def cleanup_private_workspace(path: str) -> None:
    """Explicitly remove one private attempt path, never a shared/root path."""
    root = Path(settings.resolved_task_workspaces_dir).resolve(strict=True)
    target = Path(path).expanduser().resolve(strict=True)
    if not _inside(target, root) or len(target.relative_to(root).parts) != 2:
        raise WorkspaceError("Refusing to clean a path outside an attempt workspace")
    await asyncio.to_thread(shutil.rmtree, target)


async def delete_artifact_bytes(path: str) -> None:
    """Delete captured bytes after repository tombstoning validation."""
    root = Path(settings.resolved_task_artifacts_dir).resolve(strict=True)
    target = Path(path).expanduser().resolve(strict=True)
    if not _inside(target, root):
        raise WorkspaceError("Refusing to delete an artifact outside its root")
    if target.is_dir():
        raise WorkspaceError("Artifact path points to a directory")
    target.unlink(missing_ok=True)
