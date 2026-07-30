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
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .. import deploy
from ..config import settings
from ..connector_manager import ConnectorManager
from . import workspaces as ws
from .models import (
    DELIVERY_RETENTIONS,
    DeliveryConfirmationRequired,
    DeliveryRecord,
    DeployLockedError,
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

# Delivery block reasons that a deploy_stage itself produced — a later green
# stage clears these, unlike a real git block/conflict (local-deploy.md §4).
_DEPLOY_BLOCK_REASONS = frozenset({"stage_failed", "deploy_locked"})

NotifyTerminal = Callable[[TaskRecord, DeliveryRecord], Awaitable[None]]


def _stage_success_status(
    prior_status: str,
    prior_reason_kind: str | None,
    prior_reason_detail: str | None,
    delivery: DeliveryRecord,
) -> tuple[str, str | None, str | None]:
    """The (status, reason_kind, reason_detail) a successful deploy_stage settles
    the delivery to (docs/plans/local-deploy.md §4 — "succeed back to
    ready/delivered"). Staging is transparent to genuine git state but never
    perpetuates its OWN prior failure."""
    if prior_status in {"blocked", "conflicted"} and prior_reason_kind in _DEPLOY_BLOCK_REASONS:
        # Only a prior deploy attempt blocked this delivery; a green stage undoes
        # it. Land delivered if it was ever pushed/PR'd, else ready.
        delivered = bool(delivery.pushed_ref or delivery.pr_number)
        return ("delivered" if delivered else "ready", None, None)
    if prior_status in {"blocked", "conflicted"}:
        # A real git block/conflict — a stage does not resolve it; keep it.
        return (prior_status, prior_reason_kind, prior_reason_detail)
    # A settled ready/delivered — keep it, with no lingering reason.
    return (prior_status, None, None)


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

    # --- deploy_stage (docs/plans/local-deploy.md §4, §5) ----------------

    async def deploy_stage(
        self,
        task_id: str,
        run_id: str,
        *,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
        server_root: Path | None = None,
        stage_runner: deploy.StageRunner | None = None,
    ) -> DeliveryRecord:
        """Stage the delivered sha into the idle deploy slot (local-deploy §5).

        A local, idempotent, re-runnable delivery op under the same
        ``task_delivery_ops`` machinery as ``commit`` (§4). It fetches the exact
        reviewed sha from the board repo by local path, prepares the idle slot
        completely (venv + build + import probe), and records a ``deployments``
        row — all while the running instance is untouched by construction.

        The board opt-in, the instance fail-closed guard, and the git
        prerequisites (§2/§9) are preconditions: they refuse without creating an
        op or mutating the delivery, exactly like ``deliver_op``'s baseline/state
        checks. Only an attempt that actually starts creates an op — which fails
        to ``blocked(stage_failed)`` on a bad step, or ``blocked(deploy_locked)``
        if the global deploy lock is already held (§4, §5, §12)."""
        task, run, board = await self._load(task_id, run_id)

        # 1. Board opt-in (§9). Touching the production instance is an explicit
        #    per-board decision; a board that never opted in is refused outright.
        if not board.allow_local_deploy:
            raise TaskConflictError(
                "this board may not deploy the local instance "
                "(enable allow_local_deploy on the board first)",
                current=await self._delivery_for(run_id),
            )

        # 2. Instance fail-closed guard (§3.1). A config/instance problem, not a
        #    delivery one — refuse without touching this (or any) delivery.
        check = deploy.deploy_precheck(settings, server_root=server_root)
        if not check.ok:
            raise TaskConflictError(check.message, current=await self._delivery_for(run_id))

        delivery = await self._delivery_for(run_id)

        # 3. Git prerequisites (§2) — same shape as deliver_op's preconditions.
        if delivery.status not in _GOAL_START_STATES:
            raise TaskConflictError(
                "delivery is not in a state that accepts a deploy", current=delivery
            )
        if delivery.base_head is None:
            raise TaskConflictError("delivery has no baseline; accept it first", current=delivery)
        if delivery.dirty:
            raise TaskConflictError(
                "worktree is dirty; run the commit op before deploying", current=delivery
            )
        if not delivery.attempt_head:
            raise TaskConflictError("delivery has no committed head to deploy", current=delivery)
        if (delivery.commits_ahead or 0) < 1:
            raise TaskConflictError(
                "nothing_to_deliver: no commits ahead of base", current=delivery
            )

        # deploy_precheck guaranteed a resolvable layout; read the idle slot now
        # so the deployments row and the pipeline agree on a single target.
        layout = deploy.DeployLayout.at(settings.resolved_deploy_root)
        slot = layout.idle_slot()

        prior_status = delivery.status
        prior_reason_kind = delivery.reason_kind
        prior_reason_detail = delivery.reason_detail
        op_id = await self._begin(
            delivery,
            "deploy_stage",
            request={"slot": slot, "sha": delivery.attempt_head},
            actor_kind=actor_kind,
            actor_agent_id=actor_agent_id,
        )

        # 4. Global deploy lock (§4/§12): a durable outcome. Whether the holder
        #    was already there or raced in, begin_deployment_staging's unique
        #    index rejects the insert and the op fails to blocked(deploy_locked)
        #    naming the holder — an explicit new op is required to try again.
        try:
            deployment = await self.repo.begin_deployment_staging(
                delivery_id=delivery.id,
                task_id=task.id,
                op_id=op_id,
                slot=slot,
                sha=delivery.attempt_head,
                source_repo=delivery.repository,
            )
        except DeployLockedError as exc:
            final = await self._fail(
                delivery.id, op_id, "deploy_locked", str(exc), actor_kind, actor_agent_id
            )
            return await self._published(task, final)

        runner = stage_runner or deploy._default_stage_runner
        try:
            result = await asyncio.to_thread(
                deploy.stage_slot,
                layout,
                slot,
                repo_path=delivery.repository,
                sha=delivery.attempt_head,
                timeout=settings.deploy_stage_timeout_seconds,
                runner=runner,
            )
        except Exception as exc:  # pragma: no cover - defensive; steps return rc
            await self.repo.mark_deployment_failed(deployment.id)
            final = await self._fail(
                delivery.id, op_id, "stage_failed",
                f"stage pipeline crashed: {exc}", actor_kind, actor_agent_id,
            )
            return await self._published(task, final)

        if not result.ok:
            await self.repo.mark_deployment_failed(deployment.id)
            detail = f"stage step {result.failed_step!r} failed:\n{result.output}".strip()
            final = await self._fail(
                delivery.id, op_id, "stage_failed", detail, actor_kind, actor_agent_id
            )
            return await self._published(task, final)

        staged = await self.repo.mark_deployment_staged(deployment.id)
        # A successful stage must "succeed back to ready/delivered" (§4). Staging
        # is otherwise transparent to the git-delivery status:
        #   - a delivery blocked by THIS pipeline's own prior failure
        #     (stage_failed / deploy_locked) is cleared — a green restage undoes
        #     it, landing delivered if it was ever pushed, else ready;
        #   - a block/conflict from real git work (a merge conflict, an
        #     interrupted push) is preserved — a stage does not resolve it;
        #   - a settled ready/delivered is kept as-is.
        new_status, new_reason_kind, new_reason_detail = _stage_success_status(
            prior_status, prior_reason_kind, prior_reason_detail, delivery
        )
        final, _ = await self.repo.finish_op(
            delivery.id,
            op_id,
            state="succeeded",
            delivery_status=new_status,
            reason_kind=new_reason_kind,
            reason_detail=new_reason_detail,
            result={
                "staged_sha": result.sha,
                "staged_slot": result.slot,
                "deployment_id": staged.id,
            },
            actor_kind=actor_kind,
            actor_agent_id=actor_agent_id,
        )
        return await self._published(task, final)

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
        delivery = await self.repo.record_delivery_retention(
            delivery.id,
            policy,
            actor_kind=actor_kind,
            actor_agent_id=actor_agent_id,
        )
        repo = delivery.repository
        # Live dirty check (S1) — never trust the stale complete-time snapshot.
        force_dirty = bool(confirmations.get("force_discard_dirty"))
        worktree = run.workspace_path
        if policy != "keep" and Path(worktree).is_dir():
            live_dirty = not await ws.repo_is_clean(worktree)
            if live_dirty and not force_dirty:
                raise DeliveryConfirmationRequired(
                    "worktree has uncommitted changes at teardown time",
                    confirmation="force_discard_dirty", action="teardown", current=delivery,
                )
            wt_op = await self._begin_teardown_op(delivery, "worktree_remove",
                                                  {"force": force_dirty}, actor_kind, actor_agent_id)
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
