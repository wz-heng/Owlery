"""Git delivery coordinator (task-git-delivery.md §3-§10, §16).

Drives the at-most-once local and external operations that turn a completed
git_worktree run's branch into a durable delivery. All DB state changes go
through ``TaskRepository`` CAS methods; all git work goes through ``workspaces``;
the only hosting-platform writes are PR creation via an existing connector
credential. Nothing here is auto-retried — a crash mid-op is recovered as
``interrupted`` by the manager's boot path, never re-executed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .. import deploy, switcher
from ..config import settings
from ..connector_manager import ConnectorManager
from ..deploy_admission import DeployAdmissionClosedError, DeployAdmissionGate
from . import workspaces as ws
from .deploy_quiesce import BusySource, DeployQuiesce
from .models import (
    DELIVERY_RETENTIONS,
    BoardRecord,
    DeliveryConfirmationRequired,
    DeliveryRecord,
    DeployLockedError,
    DeploymentRecord,
    ReleaseDeploymentOpRecord,
    ReleaseDeploymentRecord,
    RunRecord,
    TaskConflictError,
    TaskNotFoundError,
    TaskRecord,
    TaskValidationError,
)
from .repository import TaskRepository, task_repository

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"
_GH_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# Delivery states a settled delivery can begin a NEW goal op from (§4.1.1: an
# explicit new op may re-act from delivered/blocked/conflicted).
_GOAL_START_STATES = frozenset({"ready", "delivered", "blocked", "conflicted"})
_TEARDOWN_START_STATES = frozenset(
    {"ready", "delivered", "blocked", "conflicted", "failed"}
)

NotifyTerminal = Callable[[TaskRecord, DeliveryRecord], Awaitable[None]]

# Delivery states from which a deploy_switch may legally begin (§4.1.1). A switch
# deploys an already-settled delivery, so the same set the git goal ops use.
_SWITCH_START_STATES = _GOAL_START_STATES


class DeploySwitchAbort(Exception):
    """A `deploy_switch` handoff aborted BEFORE the point of no return (before the
    switcher was spawned / the server told to shut down). Carries the reason so
    the coordinator can fail the op durably and — if it had drained — hand the
    paused producers back (docs/plans/local-deploy.md §7.1/§7.2). This never
    escapes `deploy_switch`."""

    def __init__(
        self,
        reason: str,
        *,
        busy: list[BusySource] | None = None,
        message: str | None = None,
    ) -> None:
        super().__init__(message or reason)
        self.reason = reason
        self.busy = busy
        self.message = message


@dataclass(slots=True)
class _SwitchCtx:
    """Everything one switch attempt threads from precondition to handoff."""

    task: TaskRecord
    run: RunRecord
    delivery: DeliveryRecord
    target: DeploymentRecord
    target_state: str
    from_slot: str
    layout: "deploy.DeployLayout"
    actor_kind: str
    actor_agent_id: str | None
    op_id: str = ""


@dataclass(slots=True)
class _ReleaseSwitchCtx:
    """The release-line mirror of ``_SwitchCtx`` (release-line-deploy.md
    §3.1): a board-level switch/rollback attempt has no per-run delivery, so
    it threads a board and a release op instead of a delivery."""

    board: BoardRecord
    target: DeploymentRecord
    target_state: str
    from_slot: str
    layout: "deploy.DeployLayout"
    actor_kind: str
    actor_agent_id: str | None
    # The release row being promoted to `live` on success — `None` only for a
    # rollback whose target predates release-line-deploy (§3.1 point 3).
    target_release_id: str | None
    # The release row this op is durably attached to (`release_deployment_ops.
    # release_id`) — the staged candidate for a switch, or the CURRENTLY LIVE
    # release for a rollback (the release being abandoned; release-line-
    # deploy.md §3.1 point 3's rollback-op-attaches-to-the-live-release choice).
    op_release_id: str = ""
    op_id: str = ""




async def _github_create_pr(
    token: str,
    owner: str,
    repo: str,
    *,
    title: str,
    body: str,
    head: str,
    base: str,
    draft: bool,
) -> dict[str, Any]:
    """POST a pull request. Raises on a non-2xx so the op is recorded failed."""
    headers = {**_GH_HEADERS, "Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=float(settings.task_delivery_op_timeout_seconds)) as client:
        resp = await client.post(
            f"{_GH_API}/repos/{owner}/{repo}/pulls",
            headers=headers,
            json={"title": title, "body": body, "head": head, "base": base, "draft": draft},
        )
    if resp.status_code >= 300:
        raise ws.WorkspaceError(
            f"GitHub PR creation failed ({resp.status_code}): {resp.text[:300]}"
        )
    data = resp.json()
    return {
        "number": data.get("number"),
        "url": data.get("html_url"),
        "state": data.get("state"),
    }


async def _github_find_pr(
    token: str, owner: str, repo: str, *, head_owner: str, branch: str
) -> dict[str, Any] | None:
    """Read-only reconciliation: find an existing PR for this head branch (§8, S3)."""
    headers = {**_GH_HEADERS, "Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=float(settings.task_delivery_op_timeout_seconds)) as client:
        resp = await client.get(
            f"{_GH_API}/repos/{owner}/{repo}/pulls",
            headers=headers,
            params={"head": f"{head_owner}:{branch}", "state": "all", "per_page": 1},
        )
    if resp.status_code >= 300:
        raise ws.WorkspaceError(
            f"GitHub PR lookup failed ({resp.status_code}): {resp.text[:200]}"
        )
    items = resp.json()
    if not items:
        return None
    pr = items[0]
    return {"number": pr.get("number"), "url": pr.get("html_url"), "state": pr.get("state")}


class DeliveryCoordinator:
    """Owns delivery op execution. Bound like the dispatcher, off the DB lock."""

    def __init__(self) -> None:
        self.repo: TaskRepository = task_repository
        self.db: Any = None
        self.connectors: ConnectorManager | None = None
        self._notify_terminal: NotifyTerminal | None = None
        # Test seams for the hosting-platform layer — no real network in tests.
        self.create_pr = _github_create_pr
        self.find_pr = _github_find_pr
        # Deploy-switch machinery (docs/plans/local-deploy.md §7). Bound at boot
        # by `bind_deploy`; None means the instance cannot switch (the REST verb
        # refuses as a precondition). Tests set these directly with fakes.
        self.deploy_quiesce: DeployQuiesce | None = None
        self.broadcast_restarting: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self.request_shutdown: Callable[[], None] | None = None
        self.spawn_switcher: Callable[..., int] = switcher.spawn_switcher
        # Host the switcher health-checks the new server on — always loopback
        # (§7.3 step 4). Poll cadence for the drain/idle census waits.
        self._switch_host = "127.0.0.1"
        self._drain_poll_interval = 0.5
        # Bound only in a live server.  Tests and offline delivery tooling keep
        # their established behavior without a process-wide admission gate.
        self._admission_gate: DeployAdmissionGate | None = None

    def bind(
        self,
        *,
        db: Any,
        connectors: ConnectorManager | None = None,
        notify_terminal: NotifyTerminal | None = None,
        repo: TaskRepository | None = None,
    ) -> None:
        self.db = db
        self.connectors = connectors or ConnectorManager(db)
        self._notify_terminal = notify_terminal
        if repo is not None:
            self.repo = repo

    def bind_deploy(
        self,
        *,
        quiesce: DeployQuiesce,
        broadcast_restarting: Callable[[dict[str, Any]], Awaitable[None]],
        request_shutdown: Callable[[], None],
        spawn_switcher: Callable[..., int] | None = None,
        admission_gate: DeployAdmissionGate | None = None,
    ) -> None:
        """Wire the deploy-switch effects that only exist inside a live server
        (docs/plans/local-deploy.md §7.2): the quiesce census/drain primitives,
        the `server_restarting` broadcast, and the in-process graceful-shutdown
        trigger. Kept separate from `bind` so the coordinator's git-delivery
        surface has no dependency on a running instance."""
        self.deploy_quiesce = quiesce
        self.broadcast_restarting = broadcast_restarting
        self.request_shutdown = request_shutdown
        self._admission_gate = admission_gate
        if spawn_switcher is not None:
            self.spawn_switcher = spawn_switcher

    # --- helpers ---------------------------------------------------------

    async def _prep(self, run: Any, task: TaskRecord) -> dict[str, Any]:
        """The base/attempt/repository identity captured at prepare time."""
        meta = run.metadata or {}
        prep = meta.get("prepared") or {}
        git = meta.get("git") or {}
        branch = (
            prep.get("branch")
            or git.get("branch")
            or f"owlery/task-{run.task_id}-run-{run.attempt_no}"
        )
        repository = prep.get("repository")
        if not repository:
            # Legacy/fallback: resolve the source repo's git toplevel.
            board = await self.repo.get_board(task.board_id)
            source = task.working_dir_override or board.working_dir
            rc, top, _ = await ws._git("rev-parse", "--show-toplevel", cwd=source)
            repository = top if rc == 0 and top else str(Path(source).resolve())
        return {
            "repository": repository,
            "branch": branch,
            "base_ref": prep.get("base_ref"),
            "base_head": prep.get("base_head"),
        }

    async def _load(self, task_id: str, run_id: str) -> tuple[TaskRecord, Any, Any]:
        task = await self.repo.get_task(task_id)
        run = await self.repo.get_run(run_id)
        if run.task_id != task_id:
            raise TaskConflictError("run does not belong to task")
        return task, run, await self.repo.get_board(task.board_id)

    async def _delivery_for(self, run_id: str) -> DeliveryRecord:
        delivery = await self.repo.get_delivery_by_run(run_id)
        if delivery is None:
            raise TaskNotFoundError("no delivery for this run; accept it first")
        return delivery

    async def _commit_message(self, task: TaskRecord, run: Any) -> str:
        agent_name = "an Owlery Agent"
        if run.agent_id and self.db is not None:
            agent = await self.db.get_agent(run.agent_id)
            if agent:
                agent_name = agent.get("name") or agent_name
        summary = (run.summary or "").strip() or "Task Board worker changes."
        return (
            f"{task.title}\n\n"
            f"{summary}\n\n"
            f"Delivered by {agent_name} (Task Board attempt #{run.attempt_no}).\n\n"
            f"Owlery-Task: {task.id}\nOwlery-Run: {run.id}\n"
        )

    async def _fire_terminal(self, task: TaskRecord, delivery: DeliveryRecord) -> None:
        if (
            self._notify_terminal is not None
            and delivery.status in {"delivered", "conflicted", "blocked", "failed"}
        ):
            await self._notify_terminal(task, delivery)

    @asynccontextmanager
    async def _admit_delivery_op(self):
        """Atomically claim a new running delivery operation, if live-bound.

        The gate protects only the durable ``plan -> running`` transition.  On
        release, the operation is already visible to the deploy quiesce census,
        so its (possibly slow) git/network work may continue safely while a
        switch closes admission and observes that running row.
        """
        try:
            if self._admission_gate is None:
                yield
            else:
                async with self._admission_gate.admit():
                    yield
        except DeployAdmissionClosedError as exc:
            # Delivery endpoints already map TaskConflictError to a stable 409.
            raise TaskConflictError("deploy admission is closed") from exc

    # --- accept / baseline ----------------------------------------------

    async def accept(
        self,
        task_id: str,
        run_id: str,
        *,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
        base_ref: str | None = None,
    ) -> DeliveryRecord:
        task, run, board = await self._load(task_id, run_id)
        if run.workspace_mode != "git_worktree" or run.state != "completed":
            raise TaskConflictError("delivery requires a completed git_worktree run")
        prep = await self._prep(run, task)
        delivery = await self.repo.create_delivery(
            run_id,
            repository=prep["repository"],
            attempt_branch=prep["branch"],
            base_ref=prep["base_ref"],
            base_head=prep["base_head"],
            retention=board.git_delivery_retention,
            actor_kind=actor_kind,
            actor_agent_id=actor_agent_id,
        )
        # Nit 2: never rewind an in-flight or non-failed terminal delivery.
        if delivery.status in {"preparing", "delivering", "delivered", "conflicted"}:
            return delivery
        if delivery.status == "blocked":
            if not (delivery.reason_kind == "base_ambiguous" and base_ref):
                return delivery  # a non-base block needs an explicit op, not accept
            derived_head = await self._verify_base(delivery, base_ref)
            delivery = await self.repo.resolve_base(
                delivery.id,
                base_ref=base_ref,
                base_head=derived_head,
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
            )
        await self.repo.start_accept(
            delivery.id, actor_kind=actor_kind, actor_agent_id=actor_agent_id
        )
        result = await self._capture_baseline(
            task, run, board, delivery.id, actor_kind, actor_agent_id
        )
        await self._fire_terminal(task, result)
        return result

    async def _verify_base(self, delivery: DeliveryRecord, base_ref: str) -> str:
        """Verify an operator-named base for a legacy run; return the base_head."""
        repo = delivery.repository
        if not await ws.rev_exists(repo, base_ref):
            raise TaskConflictError(f"named base branch not found: {base_ref}")
        rc, base_tip, _ = await ws._git("rev-parse", base_ref, cwd=repo)
        if rc:
            raise TaskConflictError(f"cannot resolve base branch: {base_ref}")
        if delivery.base_head:
            if not await ws.is_ancestor(repo, delivery.base_head, base_ref):
                raise TaskConflictError(
                    "named base does not contain the run's recorded base commit"
                )
            return delivery.base_head
        # Legacy run without a captured base commit: the base must share history
        # with the attempt branch; the merge-base becomes base_head. Never guessed.
        rc, merge_base, _ = await ws._git(
            "merge-base", base_ref, delivery.attempt_branch, cwd=repo
        )
        if rc or not merge_base:
            raise TaskConflictError(
                "named base shares no history with the attempt branch"
            )
        return merge_base

    async def _capture_baseline(
        self, task, run, board, delivery_id, actor_kind, actor_agent_id
    ) -> DeliveryRecord:
        delivery = await self.repo.get_delivery(delivery_id)
        repo = delivery.repository
        base_head = delivery.base_head
        if base_head is None:
            return await self.repo.record_baseline(
                delivery_id,
                status="blocked",
                reason_kind="base_ambiguous",
                reason_detail="run has no captured base commit; name a base branch",
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
            )
        if not Path(run.workspace_path).is_dir():
            if delivery.pushed_ref:
                return await self.repo.record_baseline(
                    delivery_id,
                    status="ready",
                    remote_name=board.git_delivery_remote,
                    actor_kind=actor_kind,
                    actor_agent_id=actor_agent_id,
                )
            return await self.repo.record_baseline(
                delivery_id,
                status="failed",
                reason_kind="workspace_gone_no_effect",
                reason_detail="worktree removed and nothing was pushed",
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
            )
        state = await ws.inspect_git_workspace(run.workspace_path)
        attempt_head = state["head"]
        dirty = bool(state["porcelain"])
        commits_ahead = await ws.count_commits_ahead(repo, base_head, attempt_head)
        diffstat = await ws.compute_diffstat(repo, base_head, attempt_head)
        remote = board.git_delivery_remote
        return await self.repo.record_baseline(
            delivery_id,
            status="ready",
            base_ref=delivery.base_ref or None,
            attempt_head=attempt_head,
            dirty=dirty,
            commits_ahead=commits_ahead,
            diffstat=diffstat,
            remote_name=remote,
            remote_url=await ws.remote_url(repo, remote),
            actor_kind=actor_kind,
            actor_agent_id=actor_agent_id,
        )

    # --- goal ops --------------------------------------------------------

    async def deliver_op(
        self,
        task_id: str,
        run_id: str,
        *,
        kind: str,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
        confirmations: dict[str, bool] | None = None,
        connector_installation_id: str | None = None,
        merge_strategy: str | None = None,
        draft: bool | None = None,
    ) -> DeliveryRecord:
        confirmations = confirmations or {}
        task, run, board = await self._load(task_id, run_id)
        delivery = await self._delivery_for(run_id)
        if delivery.status not in _GOAL_START_STATES:
            raise TaskConflictError(
                "delivery is not in a state that accepts an operation",
                current=delivery,
            )
        if delivery.base_head is None:
            raise TaskConflictError("delivery has no baseline; accept it first")

        if kind == "commit":
            return await self._op_commit(task, run, board, delivery, actor_kind, actor_agent_id)
        if kind == "push":
            return await self._op_push(
                task, run, board, delivery, confirmations, actor_kind, actor_agent_id
            )
        if kind == "pull_request":
            return await self._op_pr(
                task, run, board, delivery, connector_installation_id, draft,
                actor_kind, actor_agent_id,
            )
        if kind == "merge":
            return await self._op_merge(
                task, run, board, delivery, merge_strategy, actor_kind, actor_agent_id
            )
        raise TaskValidationError(f"unknown delivery op kind: {kind}")

    async def _begin(self, delivery, kind, *, request, actor_kind, actor_agent_id):
        """Plan + start one goal op, returning its id. Retries get a fresh key."""
        async with self._admit_delivery_op():
            ops = await self.repo.list_delivery_ops(delivery.id)
            n = sum(1 for o in ops if o.kind == kind)
            suffix = "" if n == 0 else f":retry:{n}"
            source_key = f"task:{delivery.task_id}:run:{delivery.run_id}:delivery:{kind}{suffix}"
            op = await self.repo.plan_op(
                delivery.id,
                kind=kind,
                source_key=source_key,
                request=request,
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
            )
            await self.repo.start_op(
                delivery.id, op.id, advance_delivering=True, allowed_statuses=_GOAL_START_STATES
            )
            return op.id

    async def _op_commit(self, task, run, board, delivery, actor_kind, actor_agent_id):
        if delivery.status != "ready":
            raise TaskConflictError("commit is only available from a ready delivery")
        if not delivery.dirty:
            raise TaskConflictError("nothing to commit; the worktree is clean")
        op_id = await self._begin(
            delivery, "commit", request={}, actor_kind=actor_kind, actor_agent_id=actor_agent_id
        )
        repo = delivery.repository
        try:
            message = await self._commit_message(task, run)
            author = f"{board.git_delivery_author_name} {task.id[:8]}"
            await ws.commit_all(
                run.workspace_path,
                author_name=author,
                author_email=board.git_delivery_author_email,
                message=message,
            )
            attempt_head = await ws.resolve_head(run.workspace_path)
            commits_ahead = await ws.count_commits_ahead(repo, delivery.base_head, attempt_head)
            diffstat = await ws.compute_diffstat(repo, delivery.base_head, attempt_head)
            final, _ = await self.repo.finish_op(
                delivery.id, op_id, state="succeeded", delivery_status="ready",
                delivery_fields={
                    "attempt_head": attempt_head, "commits_ahead": commits_ahead,
                    "diffstat": diffstat, "dirty": False,
                },
                result={"head": attempt_head}, actor_kind=actor_kind, actor_agent_id=actor_agent_id,
            )
        except ws.WorkspaceError as exc:
            final = await self._fail(delivery.id, op_id, "op_failed", str(exc), actor_kind, actor_agent_id)
        return await self._published(task, final)

    async def _op_push(self, task, run, board, delivery, confirmations, actor_kind, actor_agent_id):
        if (delivery.commits_ahead or 0) < 1:
            raise TaskConflictError("nothing_to_deliver: no commits ahead of base")
        repo = delivery.repository
        remote = board.git_delivery_remote
        url = await ws.remote_url(repo, remote)
        if url is None:
            op_id = await self._begin(delivery, "push", request={"remote": remote},
                                      actor_kind=actor_kind, actor_agent_id=actor_agent_id)
            final = await self._fail(delivery.id, op_id, "no_remote",
                                     f"repository has no remote named {remote!r}", actor_kind, actor_agent_id)
            return await self._published(task, final)
        # Destructive guard: refuse to overwrite a non-ancestor remote ref (§13).
        force = bool(confirmations.get("allow_force_push"))
        remote_tip = await ws.remote_branch_tip(repo, remote, delivery.attempt_branch)
        if remote_tip and not await ws.is_ancestor(repo, remote_tip, delivery.attempt_head or "HEAD"):
            if not force:
                raise DeliveryConfirmationRequired(
                    "push would overwrite a diverged remote branch",
                    confirmation="allow_force_push", action="push", current=delivery,
                )
        op_id = await self._begin(
            delivery, "push", request={"remote": remote, "force": force},
            actor_kind=actor_kind, actor_agent_id=actor_agent_id,
        )
        try:
            res = await ws.push_branch(repo, remote, delivery.attempt_branch, force_with_lease=force)
            final, _ = await self.repo.finish_op(
                delivery.id, op_id, state="succeeded", delivery_status="delivered",
                delivery_fields={"pushed_ref": res["pushed_ref"], "remote_name": remote,
                                 "remote_url": url},
                result={"remote_sha": res.get("remote_sha"), "remote": remote},
                actor_kind=actor_kind, actor_agent_id=actor_agent_id,
            )
        except ws.WorkspaceError as exc:
            reason = "push_auth_failed" if "auth" in str(exc).lower() or "denied" in str(exc).lower() else "op_failed"
            final = await self._fail(delivery.id, op_id, reason, str(exc), actor_kind, actor_agent_id)
        return await self._published(task, final)

    async def _op_pr(self, task, run, board, delivery, installation_id, draft, actor_kind, actor_agent_id):
        if not delivery.pushed_ref:
            raise TaskConflictError("a successful push is required before opening a PR")
        base = delivery.base_ref
        if not base:
            raise TaskConflictError("delivery has no base branch for a PR")
        gh = ws.parse_github_remote(delivery.remote_url)
        if gh is None:
            op_id = await self._begin(delivery, "pull_request", request={},
                                      actor_kind=actor_kind, actor_agent_id=actor_agent_id)
            final = await self._fail(delivery.id, op_id, "no_connector",
                                     "remote is not a GitHub repository", actor_kind, actor_agent_id)
            return await self._published(task, final)
        owner, gh_repo = gh
        resolved = await self._resolve_connector(run.agent_id, installation_id)
        if resolved.get("error"):
            op_id = await self._begin(delivery, "pull_request", request={},
                                      actor_kind=actor_kind, actor_agent_id=actor_agent_id)
            final = await self._fail(delivery.id, op_id, resolved["error"],
                                     resolved["detail"], actor_kind, actor_agent_id)
            return await self._published(task, final)
        is_draft = board.git_delivery_default_draft_pr if draft is None else bool(draft)
        op_id = await self._begin(
            delivery, "pull_request",
            request={"installation_id": resolved["installation_id"], "draft": is_draft, "base": base},
            actor_kind=actor_kind, actor_agent_id=actor_agent_id,
        )
        try:
            token = (await self.connectors.get_access_token(resolved["installation_id"]))["access_token"]
            pr = await self.create_pr(
                token, owner, gh_repo,
                title=f"{task.title} (Task Board attempt #{run.attempt_no})",
                body=(run.summary or "Task Board worker changes.")
                + f"\n\n---\nOwlery task `{task.id}` run `{run.id}`.",
                head=delivery.attempt_branch, base=base, draft=is_draft,
            )
            final, _ = await self.repo.finish_op(
                delivery.id, op_id, state="succeeded", delivery_status="delivered",
                delivery_fields={"pr_number": pr["number"], "pr_url": pr["url"],
                                 "pr_state": pr["state"]},
                result=pr, actor_kind=actor_kind, actor_agent_id=actor_agent_id,
            )
        except ws.WorkspaceError as exc:
            final = await self._fail(delivery.id, op_id, "op_failed", str(exc), actor_kind, actor_agent_id)
        return await self._published(task, final)

    async def _op_merge(self, task, run, board, delivery, strategy, actor_kind, actor_agent_id):
        base = delivery.base_ref
        if not base:
            raise TaskConflictError("delivery has no base branch to merge into")
        strategy = strategy or (
            board.git_delivery_default_merge if board.git_delivery_default_merge != "none"
            else "fast_forward_only"
        )
        repo = delivery.repository
        # The base repo must be clean and on the base branch; never overridable.
        if not await ws.repo_is_clean(repo):
            raise TaskConflictError("base_not_clean: source repo has uncommitted changes")
        if await ws.current_branch(repo) != base:
            raise TaskConflictError(f"base_moved: source repo is not on {base}")
        op_id = await self._begin(
            delivery, "merge", request={"strategy": strategy},
            actor_kind=actor_kind, actor_agent_id=actor_agent_id,
        )
        try:
            res = await ws.merge_branch(repo, base, delivery.attempt_branch, strategy=strategy)
            if res.get("conflicted"):
                final, _ = await self.repo.finish_op(
                    delivery.id, op_id, state="failed", delivery_status="conflicted",
                    reason_kind="conflict", reason_detail=res.get("detail") or "merge could not fast-forward",
                    delivery_fields={"merge_strategy": strategy},
                    result={"conflicted": True}, actor_kind=actor_kind, actor_agent_id=actor_agent_id,
                )
            else:
                final, _ = await self.repo.finish_op(
                    delivery.id, op_id, state="succeeded", delivery_status="delivered",
                    delivery_fields={"merge_strategy": strategy},
                    result={"head": res.get("head")}, actor_kind=actor_kind, actor_agent_id=actor_agent_id,
                )
        except ws.WorkspaceError as exc:
            final = await self._fail(delivery.id, op_id, "op_failed", str(exc), actor_kind, actor_agent_id)
        return await self._published(task, final)

    # --- deploy_rollback (docs/plans/local-deploy.md §7.1/§7.2) -----

    async def deploy_rollback(
        self,
        deployment_id: str,
        *,
        confirm_rollback: bool,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
        server_root: Path | None = None,
    ) -> DeliveryRecord:
        """Switch the live process back to its most recent superseded slot.

        This is deliberately a normal, fresh ``deploy_switch`` operation: it
        follows the exact handoff, quiesce, snapshot, and health-verification
        path as a forward release.  The one extra guard is typed confirmation,
        because it intentionally discards the currently running version.
        """
        live = await self.repo.get_deployment(deployment_id)
        if live.state != "live":
            raise TaskConflictError("only the current live deployment can be rolled back")
        target = await self.repo.get_rollback_target(live.id)
        if target is None or target.delivery_id is None:
            raise TaskConflictError("no superseded deployment is available for rollback")
        delivery = await self.repo.get_delivery(target.delivery_id)
        if not confirm_rollback:
            raise DeliveryConfirmationRequired(
                "rollback replaces the running local version; confirmation is required",
                confirmation="confirm_rollback",
                action="rollback",
                current=delivery,
            )
        task, run, board = await self._load(delivery.task_id, delivery.run_id)
        if not board.allow_local_deploy:
            raise TaskConflictError(
                "this board may not deploy the local instance "
                "(enable allow_local_deploy on the board first)", current=delivery,
            )
        check = deploy.deploy_precheck(settings, server_root=server_root)
        if not check.ok:
            raise TaskConflictError(check.message, current=delivery)
        if delivery.status not in _SWITCH_START_STATES:
            raise TaskConflictError("delivery is not in a state that accepts a deploy", current=delivery)
        layout = deploy.DeployLayout.at(settings.resolved_deploy_root)
        from_slot = layout.current_slot()
        if from_slot != live.slot:
            raise TaskConflictError("current deploy slot no longer matches the live deployment", current=delivery)
        if target.slot == from_slot:
            raise TaskConflictError("rollback target is already current", current=delivery)
        if (
            self.deploy_quiesce is None
            or self.broadcast_restarting is None
            or self.request_shutdown is None
        ):
            raise TaskConflictError("deploy switch is unavailable on this instance", current=delivery)

        ctx = _SwitchCtx(
            task=task,
            run=run,
            delivery=delivery,
            target=target,
            target_state="superseded",
            from_slot=from_slot,
            layout=layout,
            actor_kind=actor_kind,
            actor_agent_id=actor_agent_id,
        )
        op_id = await self._plan_and_start_switch_op(delivery, actor_kind, actor_agent_id)
        ctx.op_id = op_id
        try:
            busy = await self.deploy_quiesce.census(exclude_op_id=op_id)
            if busy:
                raise DeploySwitchAbort("not_idle", busy=busy)
            result = await self._perform_switch_handoff(ctx)
        except DeploySwitchAbort as exc:
            final = await self._fail_switch(
                delivery.id, op_id, exc, actor_kind, actor_agent_id
            )
            return await self._published(task, final)
        return await self._published(task, result)

    async def _plan_switch_op(self, delivery, actor_kind, actor_agent_id):
        ops = await self.repo.list_delivery_ops(delivery.id)
        n = sum(1 for o in ops if o.kind == "deploy_switch")
        suffix = "" if n == 0 else f":retry:{n}"
        source_key = (
            f"task:{delivery.task_id}:run:{delivery.run_id}:delivery:deploy_switch{suffix}"
        )
        return await self.repo.plan_op(
            delivery.id, kind="deploy_switch", source_key=source_key,
            request={"drain": False}, actor_kind=actor_kind, actor_agent_id=actor_agent_id,
        )

    async def _plan_and_start_switch_op(self, delivery, actor_kind, actor_agent_id):
        op = await self._plan_switch_op(delivery, actor_kind, actor_agent_id)
        # advance_delivering=False: a deploy is orthogonal to the git-delivery
        # status (its lifecycle lives in `deployments`, §6). A successful switch
        # keeps the delivery settled; a failed one blocks it — so the status is
        # never moved to `delivering`, which would also be unrecoverable at boot.
        await self.repo.start_op(
            delivery.id, op.id, advance_delivering=False,
            allowed_statuses=_SWITCH_START_STATES,
        )
        return op.id

    async def _drain_wait(self, op_id: str) -> list[BusySource]:
        """Poll the census until idle or `deploy_quiesce_timeout_seconds` elapses
        (§7.1 drain). Returns the still-busy list on timeout (empty if it drained)."""
        deadline = time.monotonic() + float(settings.deploy_quiesce_timeout_seconds)
        while time.monotonic() < deadline:
            busy = await self.deploy_quiesce.census(exclude_op_id=op_id)
            if not busy:
                return []
            await asyncio.sleep(self._drain_poll_interval)
        return await self.deploy_quiesce.census(exclude_op_id=op_id)

    async def _perform_switch_handoff(self, ctx: _SwitchCtx) -> DeliveryRecord:
        """Close work admission, then execute the irreversible handoff.

        Closing is deliberately adjacent to the final census in the helper: a
        successful spawn keeps it closed until this process exits, while every
        exception before that spawn reopens it before the caller settles the
        aborted op.
        """
        await self.deploy_quiesce.close_admission()
        spawned = False

        def _mark_spawned() -> None:
            nonlocal spawned
            spawned = True

        try:
            return await self._perform_switch_handoff_after_admission(
                ctx, on_spawned=_mark_spawned
            )
        except BaseException:
            # `spawn_switcher` returning is the point of no return.  A later
            # broadcast/shutdown/read error cannot safely reopen the old process:
            # the detached switcher may already flip or restart it.  Conversely
            # cancellation before spawn is still an abort and must release work.
            if not spawned:
                await self.deploy_quiesce.open_admission()
            raise

    async def _perform_switch_handoff_after_admission(
        self, ctx: _SwitchCtx, *, on_spawned: Callable[[], None]
    ) -> DeliveryRecord:
        """§7.2 handoff, five steps, each committed before the next: snapshot →
        journal `handoff` → deployment `switching` + op journal_ref → spawn the
        detached switcher → broadcast `server_restarting` + graceful shutdown.

        Ordering is the at-most-once contract: the snapshot and the journal
        `handoff` line exist before the flip is possible, so a crash anywhere here
        boot-reconciles from the journal (§8) — never a silent double-switch."""
        # Re-verify idle immediately before the point of no return (§7.2). The
        # census could have gone busy between the gate and here.
        busy = await self.deploy_quiesce.census(exclude_op_id=ctx.op_id)
        if busy:
            raise DeploySwitchAbort("not_idle", busy=busy)

        layout = ctx.layout
        # 1. Checkpoint + snapshot the DB (§7.2 step 1, §7.4).
        snapshot_path = await self._snapshot_db(layout, ctx.op_id)

        # 2. Journal the handoff contract BEFORE any DB flip (§7.2 step 2). Every
        # field the detached switcher needs to flip, restart, health-check, and
        # roll back — resolved once, here, from the live process it replaces.
        old_sha = await self._current_sha(layout, ctx.from_slot)
        handoff_detail = {
            "from_slot": ctx.from_slot,
            "to_slot": ctx.target.slot,
            "old_sha": old_sha,
            "new_sha": ctx.target.sha,
            "old_pid": os.getpid(),
            "host": self._switch_host,
            "port": int(settings.port),
            "db_path": self.db.path,
            "snapshot_path": snapshot_path,
            "serve_argv": ["serve"],
            "serve_env": {},
        }
        journal = switcher.Journal(str(layout.journal_path))
        await asyncio.to_thread(
            journal.append, ctx.op_id, switcher.STEP_HANDOFF, handoff_detail
        )

        # 3. The handoff CAS: take the deployment `switching` (global lock) and
        # pin the op's journal_ref, both durable before we spawn (§7.2 step 3).
        try:
            await self.repo.begin_deployment_switching(
                deployment_id=ctx.target.id, op_id=ctx.op_id,
                expected_state=ctx.target_state,
            )
        except DeployLockedError as exc:
            raise DeploySwitchAbort("deploy_locked", message=str(exc)) from exc
        await self.repo.record_switch_journal_ref(
            ctx.op_id, journal_ref=str(layout.journal_path),
            detail={"from_slot": ctx.from_slot, "to_slot": ctx.target.slot,
                    "new_sha": ctx.target.sha},
        )

        # 4. Spawn the switcher fully detached from the OLD slot's trusted code
        # (§7.2 step 4). Past this line the switch is committed — no more aborts.
        await asyncio.to_thread(
            self.spawn_switcher,
            root=str(layout.root),
            from_slot=ctx.from_slot,
            op_id=ctx.op_id,
            switch_timeout=float(settings.deploy_switch_timeout_seconds),
            health_timeout=float(settings.deploy_health_timeout_seconds),
        )
        on_spawned()

        # 5. Broadcast the restart banner, then initiate the normal graceful
        # shutdown (§7.2 step 5). The switcher waits for this process to exit.
        await self.broadcast_restarting({
            "type": "server_restarting",
            "op_id": ctx.op_id,
            "from_slot": ctx.from_slot,
            "to_slot": ctx.target.slot,
            "sha": ctx.target.sha,
        })
        self.request_shutdown()
        return await self.repo.get_delivery(ctx.delivery.id)

    async def _snapshot_db(self, layout: "deploy.DeployLayout", op_id: str) -> str:
        """Checkpoint the WAL (TRUNCATE) and copy the DB to `snapshots/<op_id>.db`
        (§7.2 step 1, §7.4). A busy checkpoint means a reader blocked the truncate
        so a bare copy would silently drop committed frames — that is the WAL
        silent-busy data-loss trap, so we fail closed rather than snapshot lossy."""
        layout.snapshots_path.mkdir(exist_ok=True)
        snapshot = layout.snapshots_path / f"{op_id}.db"
        busy, _, _ = await self.db.wal_checkpoint_truncate()
        if busy != 0:
            raise DeploySwitchAbort(
                "snapshot_failed",
                message="wal checkpoint busy; cannot take a clean DB snapshot",
            )
        await asyncio.to_thread(shutil.copyfile, self.db.path, str(snapshot))
        self._prune_snapshots(layout, keep_id=op_id)
        return str(snapshot)

    def _prune_snapshots(self, layout: "deploy.DeployLayout", *, keep_id: str) -> None:
        """Retain `deploy_keep_snapshots` snapshots, deleting the oldest — but
        never the one just created (§7.4: a snapshot is never deleted out from
        under a rollback that may still need it)."""
        keep = max(1, int(settings.deploy_keep_snapshots))
        snaps = sorted(
            layout.snapshots_path.glob("*.db"), key=lambda p: p.stat().st_mtime
        )
        excess = len(snaps) - keep
        if excess <= 0:
            return
        for path in [p for p in snaps if p.stem != keep_id][:excess]:
            try:
                path.unlink()
            except OSError:
                pass

    async def _current_sha(self, layout: "deploy.DeployLayout", from_slot: str) -> str:
        """The build sha the OLD server reports at /health — the switcher's
        rollback health target (§7.3 step 6). The live deployment row is
        authoritative; before the first switch there is none, so fall back to the
        current slot's git HEAD (empty string if neither resolves)."""
        live = await self.repo.get_live_deployment()
        if live is not None:
            return live.sha
        rc, out, _ = await ws._git(
            "rev-parse", "HEAD", cwd=str(layout.slot_path(from_slot))
        )
        return out.strip() if rc == 0 and out else ""

    async def _fail_switch(
        self, delivery_id, op_id, exc: DeploySwitchAbort, actor_kind, actor_agent_id
    ) -> DeliveryRecord:
        """Fail a switch op that aborted pre-commit, mapping the abort reason to a
        durable delivery reason (§7.1/§12). Never touches the deployment: any
        `switching` transition is reverted by its own abort path, and a pre-flip
        abort never moved the staged row."""
        reason_map = {
            "not_idle": "not_idle",
            "deploy_locked": "deploy_locked",
            "snapshot_failed": "op_failed",
        }
        reason_kind = reason_map.get(exc.reason, "op_failed")
        if exc.reason == "not_idle" and exc.busy:
            census = "; ".join(f"{b.kind}:{b.detail}" for b in exc.busy)
            detail = f"not_idle: instance is busy — {census}"
            result: dict[str, Any] | None = {"busy": [b.to_dict() for b in exc.busy]}
        else:
            detail = exc.message or exc.reason
            result = None
        final, _ = await self.repo.finish_op(
            delivery_id, op_id, state="failed", error=detail,
            delivery_status="blocked", reason_kind=reason_kind, reason_detail=detail,
            result=result, actor_kind=actor_kind, actor_agent_id=actor_agent_id,
        )
        return final

    # --- teardown --------------------------------------------------------

    async def teardown(
        self,
        task_id: str,
        run_id: str,
        *,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
        retention: str | None = None,
        confirmations: dict[str, bool] | None = None,
    ) -> DeliveryRecord:
        confirmations = confirmations or {}
        task, run, board = await self._load(task_id, run_id)
        delivery = await self._delivery_for(run_id)
        if delivery.status not in _TEARDOWN_START_STATES:
            raise TaskConflictError("delivery is busy; cannot tear down now", current=delivery)
        policy = retention or board.git_delivery_retention
        if policy not in DELIVERY_RETENTIONS:
            raise TaskValidationError("invalid Git delivery retention")
        # Claim before the first durable teardown write — even `keep` changes
        # retention durably, and may not sneak through after a switch has closed
        # new-work admission.  If this starts worktree removal, its running op
        # is visible to the final census before the gate is released.
        worktree = run.workspace_path
        wt_op: str | None = None
        async with self._admit_delivery_op():
            delivery = await self.repo.record_delivery_retention(
                delivery.id,
                policy,
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
            )
            # Live dirty check (S1) — never trust the stale complete-time snapshot.
            force_dirty = bool(confirmations.get("force_discard_dirty"))
            if policy != "keep" and Path(worktree).is_dir():
                live_dirty = not await ws.repo_is_clean(worktree)
                if live_dirty and not force_dirty:
                    raise DeliveryConfirmationRequired(
                        "worktree has uncommitted changes at teardown time",
                        confirmation="force_discard_dirty", action="teardown", current=delivery,
                    )
                wt_op = await self._begin_teardown_op(
                    delivery, "worktree_remove", {"force": force_dirty}, actor_kind, actor_agent_id
                )
        repo = delivery.repository
        if wt_op is not None:
            try:
                await ws.remove_git_worktree(repo, worktree, force=force_dirty)
                await self.repo.finish_op(delivery.id, wt_op, state="succeeded",
                                          result={"removed": worktree},
                                          delivery_fields={"retention": policy},
                                          actor_kind=actor_kind,
                                          actor_agent_id=actor_agent_id)
            except ws.WorkspaceError as exc:
                await self.repo.finish_op(delivery.id, wt_op, state="failed", error=str(exc),
                                          actor_kind=actor_kind, actor_agent_id=actor_agent_id)
        # Branch retention.
        if policy == "remove_all":
            pushed = bool(delivery.pushed_ref)
            force_del = bool(confirmations.get("force_delete_unmerged"))
            if not pushed and not force_del:
                raise DeliveryConfirmationRequired(
                    "deleting an unpushed branch discards unmerged work",
                    confirmation="force_delete_unmerged", action="teardown", current=delivery,
                )
            br_op = await self._begin_teardown_op(delivery, "branch_delete",
                                                  {"force": force_del or not pushed}, actor_kind, actor_agent_id)
            try:
                await ws.delete_branch(repo, delivery.attempt_branch, force=force_del or not pushed)
                await self.repo.finish_op(delivery.id, br_op, state="succeeded",
                                          result={"deleted": delivery.attempt_branch},
                                          actor_kind=actor_kind, actor_agent_id=actor_agent_id)
            except ws.WorkspaceError as exc:
                await self.repo.finish_op(delivery.id, br_op, state="failed", error=str(exc),
                                          actor_kind=actor_kind, actor_agent_id=actor_agent_id)
        final = await self.repo.get_delivery(delivery.id)
        return await self._published(task, final)

    async def _begin_teardown_op(self, delivery, kind, request, actor_kind, actor_agent_id):
        async with self._admit_delivery_op():
            ops = await self.repo.list_delivery_ops(delivery.id)
            n = sum(1 for o in ops if o.kind == kind)
            suffix = "" if n == 0 else f":retry:{n}"
            source_key = f"task:{delivery.task_id}:run:{delivery.run_id}:delivery:{kind}{suffix}"
            op = await self.repo.plan_op(delivery.id, kind=kind, source_key=source_key,
                                         request=request, actor_kind=actor_kind, actor_agent_id=actor_agent_id)
            await self.repo.start_op(delivery.id, op.id, advance_delivering=False,
                                     allowed_statuses=_TEARDOWN_START_STATES)
            return op.id

    # --- connector resolution (B3) --------------------------------------

    async def _resolve_connector(self, agent_id, explicit_id):
        """Resolve the GitHub installation for the RUN's agent (§8, B3)."""
        if self.connectors is None or self.db is None or not agent_id:
            return {"error": "no_connector", "detail": "no agent connector context"}
        ids = await self.connectors.get_agent_connector_ids(agent_id)
        live: list[str] = []
        for iid in ids:
            inst = await self.connectors.get_installation(iid)
            if inst and inst.get("kind") == "github" and not inst.get("needs_reconnect"):
                live.append(iid)
        if explicit_id is not None:
            if explicit_id not in live:
                return {"error": "no_connector",
                        "detail": "selected connector is not a live GitHub install for this run's Agent"}
            return {"installation_id": explicit_id}
        if not live:
            return {"error": "no_connector", "detail": "the run's Agent has no live GitHub connector"}
        if len(live) > 1:
            return {"error": "ambiguous_connector",
                    "detail": "the run's Agent has multiple GitHub connectors; select one"}
        return {"installation_id": live[0]}

    async def reconcile_interrupted_pr(self, delivery: DeliveryRecord) -> DeliveryRecord:
        """Off-critical-path read reconcile for an interrupted PR op (§16, S3)."""
        gh = ws.parse_github_remote(delivery.remote_url)
        if gh is None or delivery.pr_number is not None:
            return delivery
        ops = await self.repo.list_delivery_ops(delivery.id)
        pr_ops = [o for o in ops if o.kind == "pull_request" and o.state == "interrupted"]
        if not pr_ops:
            return delivery
        inst = pr_ops[-1].request.get("installation_id")
        if not inst:
            return delivery
        owner, repo = gh
        try:
            token = (await self.connectors.get_access_token(inst))["access_token"]
            found = await self.find_pr(token, owner, repo, head_owner=owner, branch=delivery.attempt_branch)
        except Exception:
            logger.exception("PR reconcile read failed for delivery %s", delivery.id)
            return delivery
        if not found:
            return delivery
        # A PR already exists — record it without creating anything (never a re-POST).
        return await self.repo.record_pr_reconcile(
            delivery.id, pr_number=found["number"], pr_url=found["url"], pr_state=found["state"],
        )

    # --- release-line deploy (docs/plans/release-line-deploy.md §3) ------
    #
    # Board-level equivalents of `deploy_stage`/`deploy_switch`/`deploy_rollback`
    # above: the source is the board's protected release branch, resolved at
    # its configured remote, instead of a per-run delivery's attempt branch.
    # Every mechanism below — the global deploy lock (now doubled: a release-
    # level lock alongside the existing slot-level one), `stage_slot`, the
    # snapshot/journal/switcher handoff, quiesce, probation — is reused
    # byte-identical from the per-run path; only what may be staged/switched
    # and which table records the op differ.

    async def release_stage(
        self,
        board_id: str,
        *,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
        server_root: Path | None = None,
        stage_runner: deploy.StageRunner | None = None,
    ) -> tuple[ReleaseDeploymentRecord, ReleaseDeploymentOpRecord]:
        """Resolve the board's release ref at its remote, plan a release
        candidate, and stage it into the idle slot — one verb (§3.1 point 1).

        Preconditions (board opt-in, instance fail-closed guard, a resolvable
        release ref) refuse WITHOUT planning a release or an op — the same
        shape as `deploy_stage`'s preconditions. From the plan onward
        everything is durable: a bad stage step settles `failed` and releases
        both the release-level and slot-level locks."""
        board = await self.repo.get_board(board_id)
        if not board.allow_local_deploy:
            raise TaskConflictError(
                "this board may not deploy the local instance "
                "(enable allow_local_deploy on the board first)"
            )
        check = deploy.deploy_precheck(settings, server_root=server_root)
        if not check.ok:
            raise TaskConflictError(check.message)

        remote = board.git_delivery_remote
        remote_url = await ws.remote_url(board.working_dir, remote)
        if remote_url is None:
            raise TaskConflictError(
                f"remote_source_missing: configured remote {remote!r} is unavailable"
            )
        try:
            sha = await ws.remote_release_ref_tip(
                board.working_dir, remote_url, board.deploy_release_ref
            )
        except ws.WorkspaceError as exc:
            raise TaskConflictError(f"release ref could not be resolved: {exc}") from exc

        async with self._admit_delivery_op():
            release = await self.repo.plan_release_deployment(
                board_id=board.id,
                source_ref=board.deploy_release_ref,
                sha=sha,
                source_repo=remote_url,
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
            )
            op = await self.repo.plan_release_op(
                release.id, kind="stage", request={"sha": sha, "source_remote": remote_url},
                actor_kind=actor_kind, actor_agent_id=actor_agent_id,
            )
            await self.repo.start_release_op(release.id, op.id)

        layout = deploy.DeployLayout.at(settings.resolved_deploy_root)
        slot = layout.idle_slot()
        try:
            release, deployment = await self.repo.begin_release_staging(
                release.id, slot=slot, sha=sha, source_repo=remote_url,
            )
        except (DeployLockedError, TaskConflictError) as exc:
            # `DeployLockedError` is the expected contention path (another
            # deploy holds the slot- or release-level lock). A bare
            # `TaskConflictError` here means the release CAS lost against
            # `state='planned'` some other way; either way the op must settle
            # rather than stay `running` forever — never leave a durable op
            # dangling on a lock loss (release-line-deploy.md §3.2).
            op = await self.repo.finish_release_op(
                release.id, op.id, state="failed", error=str(exc),
            )
            # The lock loss happened before either row entered `staging`
            # (`begin_release_staging` is one transaction) — no born-together
            # deployment row exists to settle alongside it. The release row
            # itself may no longer be `planned` (e.g. concurrently
            # superseded), so this settle is best-effort and swallowed if it
            # loses its own CAS — the op is already durably failed above.
            try:
                release = await self.repo.fail_planned_release(release.id, error=str(exc))
            except TaskConflictError:
                release = await self.repo.get_release_deployment(release.id)
            return release, op

        runner = stage_runner or deploy._default_stage_runner
        try:
            result = await asyncio.to_thread(
                deploy.stage_slot,
                layout, slot, repo_path=remote_url, sha=sha,
                timeout=settings.deploy_stage_timeout_seconds, runner=runner,
            )
        except Exception as exc:  # pragma: no cover - defensive; steps return rc
            await self.repo.mark_release_failed(
                release.id, deployment_id=deployment.id,
                error=f"stage pipeline crashed: {exc}",
            )
            op = await self.repo.finish_release_op(
                release.id, op.id, state="failed",
                error=f"stage pipeline crashed: {exc}",
            )
            release = await self.repo.get_release_deployment(release.id)
            return release, op

        if not result.ok:
            detail = f"stage step {result.failed_step!r} failed:\n{result.output}".strip()
            await self.repo.mark_release_failed(
                release.id, deployment_id=deployment.id, error=detail,
            )
            op = await self.repo.finish_release_op(
                release.id, op.id, state="failed", error=detail,
            )
            release = await self.repo.get_release_deployment(release.id)
            return release, op

        release, _ = await self.repo.mark_release_staged(
            release.id, deployment_id=deployment.id,
        )
        op = await self.repo.finish_release_op(
            release.id, op.id, state="succeeded",
            result={"staged_sha": result.sha, "staged_slot": result.slot,
                    "deployment_id": deployment.id},
        )
        return release, op

    async def release_switch(
        self,
        board_id: str,
        *,
        drain: bool = False,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
        server_root: Path | None = None,
    ) -> tuple[ReleaseDeploymentRecord, ReleaseDeploymentOpRecord]:
        """Flip the board's staged release live — the release-line mirror of
        `deploy_switch` (§3.1 point 2). Human-only, enforced by the REST layer
        exactly like `deploy_switch`; this coordinator method itself has no
        actor-kind gate, matching `deploy_switch`'s own split of that
        responsibility to the router.

        `switch_when_idle` (the per-run switch's in-memory-hold mode,
        local-deploy.md §7.1) is deliberately NOT offered here: the task book
        (release-line-deploy.md §3.1 point 2) only names `drain` for the
        release verb's preconditions. `drain` covers the operational need
        (wait for idle, then switch); the unbounded in-memory hold is scoped
        out of v1 for the board-level surface, not an oversight."""
        board = await self.repo.get_board(board_id)
        if not board.allow_local_deploy:
            raise TaskConflictError(
                "this board may not deploy the local instance "
                "(enable allow_local_deploy on the board first)"
            )
        check = deploy.deploy_precheck(settings, server_root=server_root)
        if not check.ok:
            raise TaskConflictError(check.message)

        staged = await self.repo.get_staged_release(board.id)
        if staged is None:
            raise TaskConflictError("nothing_staged: run release_stage before switching")
        layout = deploy.DeployLayout.at(settings.resolved_deploy_root)
        from_slot = layout.current_slot()
        if from_slot is None:
            raise TaskConflictError("deploy layout has no current slot; run `owlery deploy init`")
        deployment = await self.repo.get_deployment(staged.deployment_id) if staged.deployment_id else None
        if deployment is None:
            raise TaskConflictError("staged release has no deployment row to switch to")
        if deployment.slot == from_slot:
            raise TaskConflictError("the staged slot is already current; nothing to switch")
        if (
            self.deploy_quiesce is None
            or self.broadcast_restarting is None
            or self.request_shutdown is None
        ):
            raise TaskConflictError("deploy switch is unavailable on this instance")

        op = await self.repo.plan_release_op(
            staged.id, kind="switch", request={"drain": drain},
            actor_kind=actor_kind, actor_agent_id=actor_agent_id,
        )
        await self.repo.start_release_op(staged.id, op.id)

        ctx = _ReleaseSwitchCtx(
            board=board, target=deployment, target_state="staged",
            from_slot=from_slot, layout=layout,
            actor_kind=actor_kind, actor_agent_id=actor_agent_id,
            target_release_id=staged.id, op_release_id=staged.id, op_id=op.id,
        )

        drained = False
        try:
            busy = await self.deploy_quiesce.census(exclude_op_id=op.id)
            if busy and drain:
                await self.deploy_quiesce.pause_for_drain()
                drained = True
                busy = await self._drain_wait(op.id)
            if busy:
                raise DeploySwitchAbort("not_idle", busy=busy)
            result = await self._perform_release_switch_handoff(ctx)
        except DeploySwitchAbort as exc:
            if drained:
                await self.deploy_quiesce.resume_from_drain()
            return await self._fail_release_switch(ctx, exc)
        return result

    async def release_rollback(
        self,
        board_id: str,
        *,
        confirm_rollback: bool,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
        server_root: Path | None = None,
    ) -> tuple[ReleaseDeploymentRecord | None, ReleaseDeploymentOpRecord]:
        """Switch the board's live release back to its most recent superseded
        slot — the release-line mirror of `deploy_rollback` (§3.1 point 3).
        Slot-level, so this works even when the current live deployment
        predates release-line-deploy (a `deployments` row with no
        `release_id`) — provenance does not matter to a slot flip."""
        board = await self.repo.get_board(board_id)
        live = await self.repo.get_live_deployment()
        if live is None:
            raise TaskConflictError("no live deployment to roll back")
        target = await self.repo.get_rollback_target(live.id)
        if target is None:
            raise TaskConflictError("no superseded deployment is available for rollback")
        if not confirm_rollback:
            raise DeliveryConfirmationRequired(
                "rollback replaces the running local version; confirmation is required",
                confirmation="confirm_rollback", action="rollback",
            )
        if not board.allow_local_deploy:
            raise TaskConflictError(
                "this board may not deploy the local instance "
                "(enable allow_local_deploy on the board first)"
            )
        check = deploy.deploy_precheck(settings, server_root=server_root)
        if not check.ok:
            raise TaskConflictError(check.message)
        layout = deploy.DeployLayout.at(settings.resolved_deploy_root)
        from_slot = layout.current_slot()
        if from_slot != live.slot:
            raise TaskConflictError("current deploy slot no longer matches the live deployment")
        if target.slot == from_slot:
            raise TaskConflictError("rollback target is already current")
        if (
            self.deploy_quiesce is None
            or self.broadcast_restarting is None
            or self.request_shutdown is None
        ):
            raise TaskConflictError("deploy switch is unavailable on this instance")

        # The rollback op is durably attached to the CURRENTLY LIVE release —
        # the one being abandoned — since that is the release row guaranteed
        # to exist (a `release_deployments_one_active` lock target). The
        # rollback TARGET's release (if any) is threaded separately as
        # `target_release_id`, which the finalizers promote to `live`.
        live_release = await self.repo.get_release_for_deployment(live.id)
        target_release = await self.repo.get_release_for_deployment(target.id)
        if live_release is None:
            raise TaskConflictError(
                "the live deployment predates release-line-deploy and has no "
                "release row to attach a rollback op to"
            )
        # `deployments`/`live` are instance-wide singletons, but this endpoint
        # is board-scoped: refuse to let board B's rollback call operate on
        # board A's live release just because B also opted into local deploy.
        if live_release.board_id != board.id:
            raise TaskConflictError(
                f"the live release belongs to a different board "
                f"({live_release.board_id!r}); roll it back from that board"
            )
        # `get_rollback_target` is slot-level and board-blind (only two slots
        # exist, so "newest superseded on the other slot" can belong to a
        # DIFFERENT board's release line — e.g. board B switched live after
        # board A, superseding A's slot; a later board-A rollback would then
        # target B's release). A `None` target release (a genuine pre-release-
        # era deployments row, release_id NULL) is exempt — that is the
        # documented §3.1 point 3 compatibility case, not a cross-board leak.
        if target_release is not None and target_release.board_id != board.id:
            raise TaskConflictError(
                f"the rollback target belongs to a different board "
                f"({target_release.board_id!r}); no eligible rollback target "
                "for this board"
            )

        op = await self.repo.plan_release_op(
            live_release.id, kind="rollback", request={},
            actor_kind=actor_kind, actor_agent_id=actor_agent_id,
        )
        await self.repo.start_release_op(live_release.id, op.id)

        ctx = _ReleaseSwitchCtx(
            board=board, target=target, target_state="superseded",
            from_slot=from_slot, layout=layout,
            actor_kind=actor_kind, actor_agent_id=actor_agent_id,
            target_release_id=target_release.id if target_release else None,
            op_release_id=live_release.id, op_id=op.id,
        )
        try:
            busy = await self.deploy_quiesce.census(exclude_op_id=op.id)
            if busy:
                raise DeploySwitchAbort("not_idle", busy=busy)
            result = await self._perform_release_switch_handoff(ctx)
        except DeploySwitchAbort as exc:
            return await self._fail_release_switch(ctx, exc)
        return result

    async def _perform_release_switch_handoff(
        self, ctx: _ReleaseSwitchCtx
    ) -> tuple[ReleaseDeploymentRecord | None, ReleaseDeploymentOpRecord]:
        """Close work admission, then execute the irreversible handoff — the
        release-line mirror of `_perform_switch_handoff`."""
        await self.deploy_quiesce.close_admission()
        spawned = False

        def _mark_spawned() -> None:
            nonlocal spawned
            spawned = True

        try:
            return await self._perform_release_switch_handoff_after_admission(
                ctx, on_spawned=_mark_spawned
            )
        except BaseException:
            if not spawned:
                await self.deploy_quiesce.open_admission()
            raise

    async def _perform_release_switch_handoff_after_admission(
        self, ctx: _ReleaseSwitchCtx, *, on_spawned: Callable[[], None]
    ) -> tuple[ReleaseDeploymentRecord | None, ReleaseDeploymentOpRecord]:
        """§7.2-equivalent handoff for a release switch/rollback: snapshot ->
        journal `handoff` -> release+deployment `switching` + op journal_ref ->
        spawn the detached switcher -> broadcast `server_restarting` +
        graceful shutdown. Byte-identical sequencing to
        `_perform_switch_handoff_after_admission`; only the DB calls at step 3
        target the release tables instead of a delivery op."""
        busy = await self.deploy_quiesce.census(exclude_op_id=ctx.op_id)
        if busy:
            raise DeploySwitchAbort("not_idle", busy=busy)

        layout = ctx.layout
        snapshot_path = await self._snapshot_db(layout, ctx.op_id)

        old_sha = await self._current_sha(layout, ctx.from_slot)
        handoff_detail = {
            "from_slot": ctx.from_slot,
            "to_slot": ctx.target.slot,
            "old_sha": old_sha,
            "new_sha": ctx.target.sha,
            "old_pid": os.getpid(),
            "host": self._switch_host,
            "port": int(settings.port),
            "db_path": self.db.path,
            "snapshot_path": snapshot_path,
            "serve_argv": ["serve"],
            "serve_env": {},
        }
        journal = switcher.Journal(str(layout.journal_path))
        await asyncio.to_thread(
            journal.append, ctx.op_id, switcher.STEP_HANDOFF, handoff_detail
        )

        try:
            # `expected_release_state` is only meaningful when `target_release_id`
            # is set (`begin_release_switching` skips the release-row CAS
            # entirely when it is None — a pre-release-era rollback target).
            await self.repo.begin_release_switching(
                release_id=ctx.target_release_id,
                deployment_id=ctx.target.id,
                op_id=ctx.op_id,
                expected_release_state=ctx.target_state,
                expected_deployment_state=ctx.target_state,
            )
        except DeployLockedError as exc:
            raise DeploySwitchAbort("deploy_locked", message=str(exc)) from exc
        op = await self.repo.record_release_switch_journal_ref(
            ctx.op_id, journal_ref=str(layout.journal_path),
            detail={"from_slot": ctx.from_slot, "to_slot": ctx.target.slot,
                    "new_sha": ctx.target.sha},
        )

        # Past this line the switch is committed — no more aborts (§7.2 step 4).
        await asyncio.to_thread(
            self.spawn_switcher,
            root=str(layout.root),
            from_slot=ctx.from_slot,
            op_id=ctx.op_id,
            switch_timeout=float(settings.deploy_switch_timeout_seconds),
            health_timeout=float(settings.deploy_health_timeout_seconds),
        )
        on_spawned()

        await self.broadcast_restarting({
            "type": "server_restarting",
            "op_id": ctx.op_id,
            "from_slot": ctx.from_slot,
            "to_slot": ctx.target.slot,
            "sha": ctx.target.sha,
        })
        self.request_shutdown()
        # The op is left `running`: like the per-run switch, its terminal state
        # is settled from the switcher's write-ahead journal, either by boot
        # reconciliation or (the common path) this same process's probation
        # monitor once it observes the journal go terminal (§8).
        release = await self.repo.get_release_deployment(ctx.op_release_id)
        return release, op

    async def _fail_release_switch(
        self, ctx: _ReleaseSwitchCtx, exc: DeploySwitchAbort
    ) -> tuple[ReleaseDeploymentRecord | None, ReleaseDeploymentOpRecord]:
        """Fail a release switch/rollback op that aborted pre-commit (mirrors
        `_fail_switch`). Never touches the deployment/release rows: any
        `switching` transition is reverted by its own abort path, and a
        pre-flip abort never moved the staged rows."""
        detail = (
            f"not_idle: instance is busy — "
            + "; ".join(f"{b.kind}:{b.detail}" for b in (exc.busy or []))
            if exc.reason == "not_idle" and exc.busy
            else (exc.message or exc.reason)
        )
        op = await self.repo.finish_release_op(
            ctx.op_release_id, ctx.op_id, state="failed", error=detail,
        )
        release = await self.repo.get_release_deployment(ctx.op_release_id)
        return release, op

    # --- shared tails ----------------------------------------------------

    async def _fail(self, delivery_id, op_id, reason_kind, detail, actor_kind, actor_agent_id):
        final, _ = await self.repo.finish_op(
            delivery_id, op_id, state="failed", error=detail,
            delivery_status="blocked", reason_kind=reason_kind, reason_detail=detail,
            actor_kind=actor_kind, actor_agent_id=actor_agent_id,
        )
        return final

    async def _published(self, task: TaskRecord, delivery: DeliveryRecord) -> DeliveryRecord:
        await self._fire_terminal(task, delivery)
        return delivery


delivery_coordinator = DeliveryCoordinator()
