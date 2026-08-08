"""Safe workspace and artifact handling for Task Board attempts."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import re
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

    # Capture the base BRANCH here, at worktree-creation time, alongside the
    # base commit — the delivery closure reads this snapshot at accept time and
    # never re-derives it from the source repo's live HEAD, which may have moved
    # (task-git-delivery.md §5, B1). A detached source HEAD records an empty
    # base_ref; legacy runs predating this capture record none at all.
    rc, base_ref, _ = await _run("git", "symbolic-ref", "--short", "HEAD", cwd=repo)
    if rc:
        base_ref = ""

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
        metadata={
            "branch": branch,
            "base_ref": base_ref,
            "base_head": head,
            "repository": str(repo),
        },
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


# --- Git delivery operations (task-git-delivery.md §5-§10) -------------------
#
# The delivery coordinator drives these; they never touch the DB. Each is a
# thin, bounded git subprocess wrapper. External-effect helpers (push/merge)
# authenticate ONLY through the repo's ambient credential store — no connector
# token is ever spliced into a URL, argv, or the environment (S4). Git itself
# owns transport auth via ssh-agent / credential helpers it already resolves.


def _delivery_timeout() -> float:
    return float(settings.task_delivery_op_timeout_seconds)


async def _git(
    *args: str, cwd: str | Path, timeout: float | None = None
) -> tuple[int, str, str]:
    return await _run(
        "git", *args, cwd=cwd, timeout=timeout or _delivery_timeout()
    )


def strip_remote_userinfo(url: str | None) -> str | None:
    """Drop HTTP(S) inline credentials, but preserve SSH login identities."""
    if not url:
        return url
    # ``ssh://git@host/...`` needs its `git` login user to work.  Only HTTP(S)
    # userinfo can be an inline password/token we must keep out of durable rows
    # and subprocess arguments. Scp-style ``git@host:owner/repo`` is unchanged.
    match = re.match(r"^(https?://)[^/@]*@(.*)$", url, flags=re.IGNORECASE)
    if match:
        return f"{match.group(1)}{match.group(2)}"
    return url


def parse_github_remote(url: str | None) -> tuple[str, str] | None:
    """Return ``(owner, repo)`` for a github.com remote, else None."""
    if not url:
        return None
    cleaned = strip_remote_userinfo(url) or ""
    match = re.search(
        r"github\.com[/:]([^/]+)/(.+?)(?:\.git)?/?$", cleaned
    )
    if not match:
        return None
    return match.group(1), match.group(2)


async def repo_is_clean(path: str) -> bool:
    rc, porcelain, err = await _git("status", "--porcelain", cwd=path)
    if rc:
        raise WorkspaceError(err or "Unable to inspect Git status")
    return porcelain == ""


async def current_branch(path: str) -> str | None:
    """The checked-out branch of a repo/worktree, or None if detached."""
    rc, branch, _ = await _git("symbolic-ref", "--short", "HEAD", cwd=path)
    return branch if rc == 0 and branch else None


async def resolve_head(path: str) -> str:
    rc, head, err = await _git("rev-parse", "HEAD", cwd=path)
    if rc:
        raise WorkspaceError(err or "Unable to resolve HEAD")
    return head


async def rev_exists(repo: str, rev: str) -> bool:
    rc, _, _ = await _git("cat-file", "-e", f"{rev}^{{commit}}", cwd=repo)
    return rc == 0


async def is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
    """True iff ``ancestor`` is reachable from ``descendant`` (merge-base check)."""
    rc, _, _ = await _git(
        "merge-base", "--is-ancestor", ancestor, descendant, cwd=repo
    )
    return rc == 0


async def count_commits_ahead(repo: str, base: str, head: str) -> int:
    rc, out, err = await _git("rev-list", "--count", f"{base}..{head}", cwd=repo)
    if rc:
        raise WorkspaceError(err or "Unable to count commits")
    try:
        return int(out.strip() or "0")
    except ValueError:
        return 0


async def compute_diffstat(repo: str, base: str, head: str) -> dict[str, int]:
    """Files changed / insertions / deletions between two revs."""
    rc, out, err = await _git("diff", "--numstat", f"{base}..{head}", cwd=repo)
    if rc:
        raise WorkspaceError(err or "Unable to compute diffstat")
    files = insertions = deletions = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        # Binary files report "-" for counts; treat as zero lines.
        if parts[0].isdigit():
            insertions += int(parts[0])
        if parts[1].isdigit():
            deletions += int(parts[1])
    return {"files": files, "insertions": insertions, "deletions": deletions}


async def commit_all(
    worktree: str, *, author_name: str, author_email: str, message: str
) -> dict[str, Any]:
    """Stage every change and create ONE owned commit; no-op if already clean.

    Never amends or rebases prior commits (§6). Returns the resulting HEAD and
    whether a commit was actually created (re-reading porcelain makes a
    crash-then-inspect re-run idempotent-by-effect).
    """
    if await repo_is_clean(worktree):
        return {"head": await resolve_head(worktree), "committed": False}
    rc, _, err = await _git("add", "-A", cwd=worktree)
    if rc:
        raise WorkspaceError(err or "Unable to stage worktree changes")
    rc, _, err = await _git(
        "-c", f"user.name={author_name}",
        "-c", f"user.email={author_email}",
        "commit", "-m", message,
        cwd=worktree,
    )
    if rc:
        raise WorkspaceError(err or "Unable to create delivery commit")
    return {"head": await resolve_head(worktree), "committed": True}


async def remote_url(repo: str, remote: str) -> str | None:
    rc, url, _ = await _git("remote", "get-url", remote, cwd=repo)
    if rc or not url:
        return None
    return strip_remote_userinfo(url.strip())


async def remote_branch_tip(repo: str, remote: str, branch: str) -> str | None:
    """The remote branch's sha via ls-remote, or None if it does not exist."""
    rc, out, _ = await _git(
        "ls-remote", "--heads", remote, branch, cwd=repo
    )
    if rc or not out:
        return None
    return out.split()[0] if out.split() else None


async def remote_release_ref_tip(repo: str, remote: str, ref: str) -> str:
    """Resolve one configured release branch at the remote to an immutable SHA.

    Release deployment accepts a branch/ref *only* as a lookup key.  The
    caller persists and stages the returned SHA, never the mutable branch name.
    Tags and arbitrary revisions are intentionally excluded from the first
    release-line UI: a board may publish only a branch under ``refs/heads``.
    """
    clean = ref.strip()
    if clean.startswith("refs/heads/"):
        qualified = clean
    elif clean and not clean.startswith("refs/") and not any(c.isspace() for c in clean):
        qualified = f"refs/heads/{clean}"
    else:
        raise WorkspaceError("release ref must name a branch")
    rc, out, err = await _git("ls-remote", "--refs", remote, qualified, cwd=repo)
    if rc:
        raise WorkspaceError(err or "unable to query the configured Git remote")
    fields = out.split()
    if len(fields) < 2 or fields[1] != qualified:
        raise WorkspaceError(f"release ref {clean!r} was not found at the configured remote")
    sha = fields[0]
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise WorkspaceError("configured release ref did not resolve to a commit SHA")
    return sha.lower()


async def push_branch(
    repo: str, remote: str, branch: str, *, force_with_lease: bool = False
) -> dict[str, Any]:
    """Push ``branch`` to ``remote`` using ambient git credentials only (S4).

    The caller has already enforced the non-fast-forward destructive guard;
    ``force_with_lease`` is honored only when that guard confirmed it. A bare
    ``--force`` is never used.
    """
    args = ["push", "--set-upstream"]
    if force_with_lease:
        args.append("--force-with-lease")
    args += [remote, branch]
    rc, out, err = await _git(*args, cwd=repo)
    if rc:
        raise WorkspaceError(err or out or "git push failed")
    remote_sha = await remote_branch_tip(repo, remote, branch)
    return {
        "pushed_ref": f"refs/heads/{branch}",
        "remote_sha": remote_sha,
        "remote": remote,
    }


async def merge_branch(
    repo: str, base_ref: str, branch: str, *, strategy: str
) -> dict[str, Any]:
    """Conservatively merge ``branch`` into ``base_ref`` in the source repo.

    The caller has verified the repo is clean and checked out on ``base_ref``.
    Never forces or auto-resolves; a conflict/non-fast-forward aborts cleanly
    and reports ``conflicted`` (§9).
    """
    if strategy == "fast_forward_only":
        rc, out, err = await _git("merge", "--ff-only", branch, cwd=repo)
        if rc:
            return {"merged": False, "conflicted": True, "detail": err or out}
    elif strategy == "no_conflict_merge":
        rc, out, err = await _git(
            "merge", "--no-ff", "--no-edit", branch, cwd=repo
        )
        if rc:
            await _git("merge", "--abort", cwd=repo)
            return {"merged": False, "conflicted": True, "detail": err or out}
    else:
        raise WorkspaceError(f"unknown merge strategy: {strategy}")
    return {"merged": True, "conflicted": False, "head": await resolve_head(repo)}


async def remove_git_worktree(repo: str, path: str, *, force: bool = False) -> None:
    """Deregister and remove a worktree, then prune stale registrations (§10).

    This is the fix for the historical leak where ``cleanup_private_workspace``
    removed the directory without deregistering it. Idempotent: a
    already-removed worktree prunes clean.
    """
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(path)
    rc, out, err = await _git(*args, cwd=repo)
    # A missing worktree is not an error for teardown — prune reconciles it.
    if rc and "is not a working tree" not in (err + out) and Path(path).exists():
        raise WorkspaceError(err or out or "Unable to remove Git worktree")
    await _git("worktree", "prune", cwd=repo)


async def delete_branch(repo: str, branch: str, *, force: bool = False) -> None:
    """Delete a local branch (idempotent — an absent branch is a no-op)."""
    flag = "-D" if force else "-d"
    rc, out, err = await _git("branch", flag, branch, cwd=repo)
    if rc and "not found" not in (err + out):
        raise WorkspaceError(err or out or f"Unable to delete branch {branch}")
