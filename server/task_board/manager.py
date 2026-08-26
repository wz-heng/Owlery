"""Dispatcher, worker protocol, recovery, and notifications for Task Board."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .. import deploy, switcher
from ..config import settings
from ..connector_manager import ConnectorManager
from ..deploy_admission import DeployAdmissionClosedError
from ..model_routing import ModelBackendError, validate_model_for_backend
from .delivery import DeliveryCoordinator, delivery_coordinator
from .deploy_quiesce import DeployQuiesce
from .models import DeliveryRecord, RunRecord, TaskBoardError, TaskConflictError, TaskRecord
from .prompts import render_assignment_prompt
from .repository import TaskRepository, task_repository
from . import workspaces as ws
from .workspaces import (
    WorkspaceError,
    capture_artifacts,
    delete_artifact_bytes,
    discard_captured_artifacts,
    inspect_git_workspace,
    prepare_workspace,
)

if False:  # pragma: no cover - type-only imports without runtime cycles
    from ..database import Database
    from ..session_manager import SessionManager

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Switcher journal steps that terminate a deploy_switch op — the boot reconciler
# and the probation monitor both key off these (docs/plans/local-deploy.md §8).
_SWITCH_TERMINAL_STEPS = frozenset({
    switcher.STEP_SWITCHED_OK,
    switcher.STEP_ROLLED_BACK,
    switcher.STEP_ROLLBACK_INCOMPLETE,
    switcher.STEP_OLD_WONT_DIE,
    switcher.STEP_SWITCH_ERROR,
})


@dataclass(frozen=True, slots=True)
class DeployProbation:
    """A booting server that flipped but has not yet seen the switcher's verdict
    (docs/plans/local-deploy.md §7.5). Its op is left `running`; the probation
    monitor holds the producers paused until the journal goes terminal or the
    health window elapses, then finalizes the op and releases them."""

    op_id: str
    journal_path: str
    # The original switcher's health window, measured from its durable
    # ``flip_done`` journal line.  A restarted server must consume only the
    # remainder, never grant the unconfirmed switch a fresh full timeout.
    health_deadline: datetime
    # ``task_delivery_ops`` (per-run) or ``release_deployment_ops`` (release-
    # line, release-line-deploy.md §3.3) — which table/finalizers `op_id`
    # belongs to. The two share the same slot-level lock, so at most one
    # probation candidate can exist across both at any boot.
    kind: str = "delivery"


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _record(value: Any) -> Any:
    return value.to_dict() if hasattr(value, "to_dict") else value


class TaskBoardManager:
    BROADCAST_KEY = "task_board_manager"

    def __init__(self) -> None:
        self.repo: TaskRepository = task_repository
        self.session_mgr: "SessionManager | None" = None
        self.db: "Database | None" = None
        self._wake = asyncio.Event()
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._last_tick_at: str | None = None
        self._last_error: str | None = None
        self._idle_checks: dict[str, asyncio.Task[None]] = {}
        self.delivery: DeliveryCoordinator = delivery_coordinator
        # Transient (never persisted) dispatcher pause for a deploy drain
        # (docs/plans/local-deploy.md §7.1). In-memory so a restart clears it —
        # unlike per-board `dispatch_enabled`, which must survive a deploy.
        self._deploy_paused = False
        self._probation_poll_interval = 0.5

    def bind(
        self,
        *,
        session_mgr: "SessionManager",
        db: "Database",
        repo: TaskRepository | None = None,
    ) -> None:
        self.session_mgr = session_mgr
        self.db = db
        if repo is not None:
            self.repo = repo
        self.delivery.bind(
            db=db,
            connectors=ConnectorManager(db),
            notify_terminal=self._notify_delivery_terminal,
            repo=self.repo,
            on_task_touched=self.publish_task_update,
        )
        session_mgr.on_broadcast(self.BROADCAST_KEY, self._on_broadcast)

    async def start(self) -> None:
        if self._dispatcher_task and not self._dispatcher_task.done():
            return
        self._stopping = False
        self._dispatcher_task = asyncio.create_task(
            self._dispatcher_loop(), name="task-board-dispatcher"
        )
        self._wake.set()

    async def shutdown(self) -> None:
        """Stop claims, make active runs truthful, and leave outbox pending."""
        self._stopping = True
        task = self._dispatcher_task
        self._dispatcher_task = None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        checks = list(self._idle_checks.values())
        for check in checks:
            check.cancel()
        if checks:
            await asyncio.gather(*checks, return_exceptions=True)
        self._idle_checks.clear()

        if self.session_mgr is not None:
            for run in await self.repo.list_running_runs():
                # Terminal CAS first: the interrupt broadcast cannot overwrite
                # the durable outcome if it races this shutdown path.
                try:
                    task_row, final_run = await self.repo.interrupt_run(
                        run.task_id,
                        run.id,
                        reason="server shutting down; work may have partially completed",
                    )
                except TaskConflictError:
                    continue
                await self._notify_terminal(task_row, final_run)
                if run.session_id:
                    await self.session_mgr.interrupt(run.session_id)
                    session = self.session_mgr.sessions.get(run.session_id)
                    if session is not None:
                        session._auto_archive_requested = True
        if self.session_mgr is not None:
            self.session_mgr.remove_broadcast(self.BROADCAST_KEY)

    async def recover_phase1(self) -> list[tuple[TaskRecord, RunRecord]]:
        """Interrupt prior-process runs while workers remain available."""
        if self.session_mgr is None or not self.session_mgr.session_injection_dispatch_paused:
            raise RuntimeError("Task recovery requires paused session injection dispatch")
        recovered = await self.repo.interrupt_all_running(
            reason="server restarted; work may have partially completed"
        )
        # A parked task worker must not wake after its run is interrupted.
        parked = getattr(self.session_mgr, "_parked_turns", None)
        if parked is not None:
            for _, run in recovered:
                if run.session_id:
                    await parked.cancel(run.session_id)
        return recovered

    async def recover_phase2(self) -> int:
        """Repair terminal notifications, materialize worker events, archive."""
        if self.session_mgr is None or self.db is None:
            return 0
        if not self.session_mgr.session_injection_dispatch_paused:
            raise RuntimeError("Task recovery requires paused session injection dispatch")

        repaired = 0
        worker_session_ids: set[str] = set()
        for task, run in await self.repo.list_terminal_runs():
            if run.session_id:
                worker_session_ids.add(run.session_id)
            if not task.origin_session_id:
                continue
            source_key = self._terminal_source(task.id, run.id)
            if await self.db.get_session_injection_by_source(source_key):
                continue
            if not await self.db.session_exists(task.origin_session_id):
                await self.repo.record_notification_unavailable(
                    task.id, run.id, reason="origin session was deleted"
                )
                continue
            await self._notify_terminal(task, run)
            repaired += 1

        await self.session_mgr.materialize_pending_injections_for_sessions(
            worker_session_ids
        )
        for session_id in worker_session_ids:
            session = self.session_mgr.sessions.get(session_id)
            if session is not None and session.origin == "task":
                await self.session_mgr.auto_archive_scheduled_session(session_id)
        return repaired

    async def wake_dispatcher(self) -> None:
        self._wake.set()

    async def _dispatcher_loop(self) -> None:
        interval = max(float(settings.task_dispatch_interval_seconds), 0.1)
        while not self._stopping:
            try:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=interval)
                except TimeoutError:
                    pass
                self._wake.clear()
                await self._dispatcher_pass()
                self._last_tick_at = _iso()
                self._last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                logger.exception("Task Board dispatcher pass failed")

    def pause_dispatch_for_deploy(self) -> None:
        """Transiently stop the dispatcher claiming new runs during a deploy drain
        (§7.1). Running runs are untouched — a deploy never kills work."""
        self._deploy_paused = True

    def resume_dispatch_for_deploy(self) -> None:
        self._deploy_paused = False
        self._wake.set()

    async def _dispatcher_pass(self) -> None:
        if self._deploy_paused:
            return
        await self.repo.reconcile_eligibility()
        await self._refresh_liveness()
        ready = await self.repo.list_tasks(status="ready", limit=1000)
        for task in ready:
            if self._stopping:
                return
            try:
                await self._dispatch_task(task)
            except DeployAdmissionClosedError:
                # A deploy closed the final admission gate after this pass
                # enumerated ``ready``.  Leave the task ready for the new
                # process (or a pre-spawn deploy abort) to dispatch later.
                continue
            except TaskBoardError:
                # Expected CAS/capacity races: another task/tick owns it.
                continue
            except Exception:
                logger.exception("Failed to dispatch task %s", task.id)

    async def _dispatch_task(self, task: TaskRecord) -> None:
        assert self.session_mgr is not None and self.db is not None
        board = await self.repo.get_board(task.board_id)
        mode = task.workspace_mode or board.default_workspace_mode
        source = task.working_dir_override or board.working_dir
        run_id = uuid.uuid4().hex[:12]
        prior_runs = await self.repo.list_runs(task.id)
        attempt_no = len(prior_runs) + 1
        if mode == "shared":
            planned_path = str(Path(source).expanduser().resolve(strict=True))
        else:
            planned_path = str(
                Path(settings.resolved_task_workspaces_dir).resolve(strict=False)
                / task.id
                / run_id
            )
        lease = _iso(_now() + timedelta(seconds=settings.task_run_lease_seconds))
        # ``claim_ready`` is the durable point at which a ready task becomes
        # work the deploy census must wait for.  Keep exactly that claim under
        # the shared admission gate: after ``close`` returns, the census sees
        # this run, or no dispatcher run has been claimed.  Workspace setup is
        # intentionally outside the gate -- it can involve slow git I/O, and
        # a claimed run is already visible to the census while it is prepared.
        async with self.session_mgr.deploy_admission_gate.admit():
            run = await self.repo.claim_ready(
                task.id,
                workspace_mode=mode,
                workspace_path=planned_path,
                lease_expires_at=lease,
                run_id=run_id,
            )
        try:
            prepared = await prepare_workspace(
                mode=mode,
                source_dir=source,
                task_id=task.id,
                run_id=run.id,
                attempt_no=run.attempt_no,
            )
            if prepared.metadata:
                # Persist the git base branch/commit so it survives the
                # completion-metadata overwrite and delivery can read it later
                # (task-git-delivery.md §5, B1).
                await self.repo.set_run_metadata(
                    task.id, run.id, {"prepared": prepared.metadata}
                )
            agent = await self.db.get_agent(task.assignee_agent_id) if task.assignee_agent_id else None
            if agent is None:
                raise WorkspaceError("Assigned Agent is unavailable")
            worker_backend = agent.get("backend") or "claude-code"
            # Belt-and-braces re-check: the task's model was validated against
            # its assignee at create/specify time, but a later reassignment can
            # pair it with an incompatible backend. Fail the run with a clear
            # message rather than spawning a doomed worker session
            # (budget-model-routing.md §4.3).
            try:
                validate_model_for_backend(worker_backend, task.model)
            except ModelBackendError as exc:
                raise WorkspaceError(str(exc)) from exc
            session = await self.session_mgr.create_session(
                agent_id=agent["id"],
                name=f"Task: {task.title}",
                working_dir=prepared.path,
                origin="task",
                backend=worker_backend,
                task_id=task.id,
                task_run_id=run.id,
                model=task.model,
            )
            run = await self.repo.attach_run_session(task.id, run.id, session.id)
            prompt = render_assignment_prompt(
                task=task, board=board, run=run, workspace=prepared.path
            )
            await self.session_mgr.start_message(
                session.id, prompt, admission_claimed=True
            )
            await self.publish_task_update(task.id)
        except Exception as exc:
            try:
                final_task, final_run = await self.repo.fail_run(
                    task.id,
                    run.id,
                    error=f"worker setup failed: {exc}",
                    kind="capability" if isinstance(exc, WorkspaceError) else "failure",
                )
            except TaskConflictError:
                raise
            await self._notify_terminal(final_task, final_run)
            await self.publish_task_update(task.id)

    async def _refresh_liveness(self) -> None:
        """Extend pending leases; interrupt only truly abandoned attempts."""
        now = _now()
        for run in await self.repo.list_running_runs():
            pending = await self.worker_has_pending_work(run.session_id)
            expires = None
            try:
                expires = datetime.fromisoformat(run.lease_expires_at) if run.lease_expires_at else None
            except ValueError:
                expires = None
            if pending:
                await self.repo.heartbeat_run(
                    run.task_id,
                    run.id,
                    lease_expires_at=_iso(now + timedelta(seconds=settings.task_run_lease_seconds)),
                    emit_event=False,
                )
            elif expires is not None and expires <= now:
                task, final = await self.repo.interrupt_run(
                    run.task_id, run.id, reason="worker lease expired without tracked work"
                )
                await self._notify_terminal(task, final)
                if run.session_id and self.session_mgr is not None:
                    await self.session_mgr.interrupt(run.session_id)

    async def worker_has_pending_work(self, session_id: str | None) -> bool:
        if not session_id or self.session_mgr is None:
            return False
        session = self.session_mgr.sessions.get(session_id)
        if session is not None:
            if session._pending_queue or session._pending_questions or session._pending_approvals:
                return True
            if session._inner_task and not session._inner_task.done():
                return True
            if session._active_task and not session._active_task.done():
                return True
            if await self.session_mgr.get_pending_park(session_id):
                return True
        if self.db is not None:
            return await self.db.worker_has_persisted_pending_work(session_id)
        return False

    async def _on_broadcast(self, message: dict[str, Any]) -> None:
        session_id = message.get("session_id")
        if not session_id:
            return
        session = self.session_mgr.sessions.get(session_id) if self.session_mgr else None
        if session is None or session.origin != "task" or not session.task_run_id:
            return
        kind = message.get("type")
        if kind in {"assistant_text", "tool_use", "tool_result", "status"}:
            if kind != "status" or message.get("status") == "running":
                try:
                    await self.repo.heartbeat_run(
                        session.task_id,
                        session.task_run_id,
                        lease_expires_at=_iso(
                            _now() + timedelta(seconds=settings.task_run_lease_seconds)
                        ),
                        emit_event=False,
                    )
                except TaskConflictError:
                    pass
        if kind == "status" and message.get("status") == "idle":
            old = self._idle_checks.pop(session_id, None)
            if old:
                old.cancel()
            self._idle_checks[session_id] = asyncio.create_task(
                self._check_idle_protocol(session_id),
                name=f"task-idle-check-{session.task_run_id}",
            )
        elif kind == "error" and message.get("code") != "limit_paused":
            await self._finish_from_session_error(session, message)

    async def _check_idle_protocol(self, session_id: str) -> None:
        try:
            # The idle broadcast occurs inside send_message, before the queue
            # driver clears its own task or starts an already-queued turn.
            # Wait for that driver to drain the whole in-memory queue; this is
            # the exact post-transition boundary the protocol requires.
            initial = self.session_mgr.sessions.get(session_id) if self.session_mgr else None
            driver = initial._active_task if initial is not None else None
            if driver is not None and driver is not asyncio.current_task():
                try:
                    await asyncio.shield(driver)
                except Exception:
                    pass
            session = self.session_mgr.sessions.get(session_id) if self.session_mgr else None
            if session is None or not session.task_id or not session.task_run_id:
                return
            if await self.worker_has_pending_work(session_id):
                return
            try:
                task, run = await self.repo.fail_run(
                    session.task_id,
                    session.task_run_id,
                    error="worker became idle without calling complete or block",
                    kind="protocol",
                )
            except TaskConflictError:
                return
            await self._notify_terminal(task, run)
            session._auto_archive_requested = True
            await self.session_mgr.auto_archive_scheduled_session(session_id)
            await self.publish_task_update(task.id)
        finally:
            self._idle_checks.pop(session_id, None)

    async def _finish_from_session_error(self, session: Any, message: Mapping[str, Any]) -> None:
        try:
            error = str(message.get("message") or "worker session error")
            if "interrupted by user" in error:
                task, run = await self.repo.interrupt_run(
                    session.task_id, session.task_run_id, reason=error
                )
            else:
                task, run = await self.repo.fail_run(
                    session.task_id,
                    session.task_run_id,
                    error=error,
                    kind="failure",
                )
        except TaskConflictError:
            return
        await self._notify_terminal(task, run)
        session._auto_archive_requested = True
        await self.publish_task_update(task.id)

    async def _validate_worker(
        self, task_id: str, run_id: str, session_id: str
    ) -> tuple[TaskRecord, RunRecord]:
        task = await self.repo.get_task(task_id)
        run = await self.repo.get_run(run_id)
        if (
            run.task_id != task_id
            or run.session_id != session_id
            or run.state != "running"
            or task.current_run_id != run_id
        ):
            raise TaskConflictError("worker identity no longer owns this task", current=task)
        return task, run

    async def worker_snapshot(self, task_id: str, run_id: str, session_id: str) -> dict[str, Any]:
        task, run = await self._validate_worker(task_id, run_id, session_id)
        board = await self.repo.get_board(task.board_id)
        dependencies = []
        for link in await self.repo.list_dependencies(task.id):
            dep = await self.repo.get_task(link.depends_on_task_id)
            dependencies.append(dep.to_dict())
        return {
            "board": board.to_dict(),
            "task": task.to_dict(),
            "run": run.to_dict(),
            "dependencies": dependencies,
            "comments": [_record(x) for x in await self.repo.list_comments(task.id)],
            "prior_runs": [_record(x) for x in await self.repo.list_runs(task.id)],
            "artifacts": [_record(x) for x in await self.repo.list_artifacts(task.id)],
        }

    async def heartbeat_worker(
        self, task_id: str, run_id: str, session_id: str, note: str | None = None
    ) -> dict[str, Any]:
        await self._validate_worker(task_id, run_id, session_id)
        run = await self.repo.heartbeat_run(
            task_id,
            run_id,
            lease_expires_at=_iso(_now() + timedelta(seconds=settings.task_run_lease_seconds)),
            note=note,
        )
        await self.publish_task_update(task_id)
        return run.to_dict()

    async def complete_worker(
        self,
        task_id: str,
        run_id: str,
        session_id: str,
        *,
        summary: str,
        metadata: Mapping[str, Any] | None = None,
        artifacts: list[dict[str, str]] | None = None,
        verdict: str | None = None,
    ) -> dict[str, Any]:
        task, run = await self._validate_worker(task_id, run_id, session_id)
        # Preserve prep metadata (the git base branch/commit) under the worker's
        # declared metadata and the terminal git inspection (B1).
        terminal_metadata = dict(run.metadata or {})
        terminal_metadata.update(dict(metadata or {}))
        if run.workspace_mode == "git_worktree":
            terminal_metadata["git"] = await inspect_git_workspace(run.workspace_path)
        captured = await capture_artifacts(
            workspace=run.workspace_path,
            task_id=task_id,
            run_id=run_id,
            artifacts=artifacts or [],
        )
        try:
            task, final = await self.repo.complete_run(
                task_id,
                run_id,
                summary=summary,
                metadata=terminal_metadata,
                artifacts=[asdict(item) for item in captured],
                actor_agent_id=run.agent_id,
                verdict=verdict,
            )
        except Exception:
            await discard_captured_artifacts(captured)
            raise
        await self._notify_terminal(task, final)
        session = self.session_mgr.sessions.get(session_id) if self.session_mgr else None
        if session is not None:
            session._auto_archive_requested = True
        await self.publish_task_update(task_id)
        await self.wake_dispatcher()
        return {"task": task.to_dict(), "run": final.to_dict()}

    async def block_worker(
        self,
        task_id: str,
        run_id: str,
        session_id: str,
        *,
        reason: str,
        kind: str = "input",
        summary: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _, run = await self._validate_worker(task_id, run_id, session_id)
        task, final = await self.repo.block_run(
            task_id,
            run_id,
            reason=reason,
            kind=kind,
            summary=summary,
            metadata=metadata,
            actor_agent_id=run.agent_id,
        )
        await self._notify_terminal(task, final)
        session = self.session_mgr.sessions.get(session_id) if self.session_mgr else None
        if session is not None:
            session._auto_archive_requested = True
        await self.publish_task_update(task_id)
        return {"task": task.to_dict(), "run": final.to_dict()}

    async def create_worker_task(
        self, task_id: str, run_id: str, session_id: str, **create: Any
    ) -> dict[str, Any]:
        task, run = await self._validate_worker(task_id, run_id, session_id)
        child = await self.repo.create_task(
            board_id=task.board_id,
            title=create.pop("title"),
            body=create.pop("body", ""),
            parent_task_id=create.pop("parent_task_id", None) or task.id,
            assignee_agent_id=create.pop("assignee_agent_id", None),
            priority=create.pop("priority", 0),
            dependencies=create.pop("dependencies", None),
            scheduled_at=create.pop("scheduled_at", None),
            idempotency_key=create.pop("idempotency_key", None),
            status=create.pop("status", "triage"),
            model=create.pop("model", None),
            origin_session_id=task.origin_session_id,
            created_by_kind="agent",
            created_by_agent_id=run.agent_id,
            creator_run_id=run_id,
        )
        await self.publish_task_update(child.id)
        await self.wake_dispatcher()
        return child.to_dict()

    async def link_worker_dependency(
        self,
        task_id: str,
        run_id: str,
        session_id: str,
        subject_task_id: str,
        depends_on_task_id: str,
    ) -> dict[str, Any]:
        owner, run = await self._validate_worker(task_id, run_id, session_id)
        subject = await self.repo.get_task(subject_task_id)
        dependency = await self.repo.get_task(depends_on_task_id)
        if subject.board_id != owner.board_id or dependency.board_id != owner.board_id:
            raise TaskConflictError("worker dependencies cannot cross boards")
        link = await self.repo.add_dependency(
            subject_task_id,
            depends_on_task_id,
            created_by_kind="agent",
            created_by_agent_id=run.agent_id,
        )
        await self.publish_task_update(subject_task_id)
        return _record(link)

    async def cancel_task(self, task_id: str, reason: str = "cancelled") -> dict[str, Any]:
        task = await self.repo.get_task(task_id)
        if task.status == "running" and task.current_run_id:
            # cancelled is a terminal status, not a run outcome (task-board-gaps.md
            # §3.4) — a running task cannot jump straight to it, so stop the run
            # first (lands the task in `blocked`), then finish the SAME cancel
            # request through to `cancelled` in this one call. Chaining both here
            # (rather than requiring a second user click once it's blocked) is what
            # keeps a cancel-while-running from re-creating the exact "looks
            # blocked, is actually long since cancelled" card the migration in
            # §3.4 is cleaning up.
            run = await self.repo.get_run(task.current_run_id)
            task, final = await self.repo.cancel_run(task_id, run.id, reason=reason)
            await self._notify_terminal(task, final)
            if run.session_id and self.session_mgr is not None:
                await self.session_mgr.interrupt(run.session_id)
            task = await self.repo.cancel_task(task_id, reason=reason)
        else:
            task = await self.repo.cancel_task(task_id, reason=reason)
        await self.publish_task_update(task_id)
        return task.to_dict()

    async def block_active_task(
        self,
        task_id: str,
        *,
        reason: str,
        kind: str = "input",
        summary: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        task = await self.repo.get_task(task_id)
        if task.status != "running" or not task.current_run_id:
            raise TaskConflictError("only a running task can be blocked", current=task)
        run = await self.repo.get_run(task.current_run_id)
        task, final = await self.repo.block_run(
            task_id,
            run.id,
            reason=reason,
            kind=kind,
            summary=summary,
            metadata=metadata,
        )
        await self._notify_terminal(task, final)
        if run.session_id and self.session_mgr is not None:
            await self.session_mgr.interrupt(run.session_id)
        await self.publish_task_update(task_id)
        return task.to_dict()

    async def delete_artifact(self, task_id: str, artifact_id: str) -> dict[str, Any]:
        artifact = await self.repo.get_artifact(task_id, artifact_id)
        await delete_artifact_bytes(artifact.stored_path)
        tombstone = await self.repo.delete_artifact(task_id, artifact_id)
        await self.publish_task_update(task_id)
        return tombstone.to_dict()

    async def dispatcher_status(self, board_id: str) -> dict[str, Any]:
        board = await self.repo.get_board(board_id)
        running_by_agent: dict[str, int] = {}
        running = 0
        for run in await self.repo.list_running_runs():
            task = await self.repo.get_task(run.task_id)
            if task.board_id != board_id:
                continue
            running += 1
            key = run.agent_id or "unassigned"
            running_by_agent[key] = running_by_agent.get(key, 0) + 1
        return {
            "board_id": board_id,
            "enabled": board.dispatch_enabled,
            "running": running,
            "running_by_agent": running_by_agent,
            "last_tick_at": self._last_tick_at,
            "last_error": self._last_error,
        }

    async def publish_task_update(self, task_id: str) -> None:
        if self.session_mgr is None:
            return
        event = await self.repo.get_latest_task_event(task_id)
        if event is None:
            return
        task = await self.repo.enrich_task(task_id)
        payload = event.to_dict()
        payload["payload"] = {**payload["payload"], "task": task}
        await self.session_mgr._broadcast(
            {
                "type": "task_event",
                "board_id": task["board_id"],
                "task_id": task["id"],
                "event": payload,
            }
        )

    async def publish_board_updates(self, board_id: str) -> None:
        for task in await self.repo.list_tasks(
            board_id=board_id, include_archived=True, limit=1000
        ):
            await self.publish_task_update(task.id)

    async def publish_board_update(self, board_id: str) -> None:
        """Broadcast the board audit event, then any eligibility task changes."""
        if self.session_mgr is not None:
            event = await self.repo.get_latest_board_event(board_id)
            if event is not None:
                await self.session_mgr._broadcast(
                    {
                        "type": "task_event",
                        "board_id": board_id,
                        "task_id": None,
                        "event": event.to_dict(),
                    }
                )
        await self.publish_board_updates(board_id)

    @staticmethod
    def _terminal_source(task_id: str, run_id: str) -> str:
        return f"task:{task_id}:run:{run_id}:terminal"

    async def _notify_terminal(self, task: TaskRecord, run: RunRecord) -> None:
        if not task.origin_session_id or self.session_mgr is None or self.db is None:
            return
        if not await self.db.session_exists(task.origin_session_id):
            await self.repo.record_notification_unavailable(
                task.id, run.id, reason="origin session was deleted"
            )
            return
        label = {
            "completed": "completed",
            "blocked": "blocked",
            "failed": "failed",
            "cancelled": "cancelled",
            "interrupted": "interrupted; outcome may be unknown",
        }.get(run.state, run.state)
        detail = run.summary or run.error or task.blocked_reason or "No summary supplied."
        prompt = (
            f"[task-result task={task.id} run={run.id} state={run.state}]\n"
            f"Task **{task.title}** {label}.\n\n{detail}\n\n"
            "Inspect the Task Board run/session/workspace before deciding on any "
            "follow-up. Never automatically retry interrupted external work."
        )
        await self.session_mgr.enqueue_session_injection(
            source_key=self._terminal_source(task.id, run.id),
            session_id=task.origin_session_id,
            prompt=prompt,
        )

    # --- Git delivery (task-git-delivery.md §16) -------------------------

    async def request_delivery(
        self, task_id: str, run_id: str, session_id: str, *, note: str | None = None
    ) -> dict[str, Any]:
        """Worker-scoped hint: record that the worker wants its run delivered.

        Performs NO external effect — push/PR/merge stay in the trusted server
        path, triggered by a human or orchestrator (task-git-delivery.md §15)."""
        _, run = await self._validate_worker(task_id, run_id, session_id)
        body = "Worker requested Git delivery of this run's branch."
        if note:
            body += f"\n\n{note}"
        await self.repo.add_comment(
            task_id, body, run_id=run_id, author_kind="agent",
            author_agent_id=run.agent_id,
        )
        await self.publish_task_update(task_id)
        return {"requested": True, "task_id": task_id, "run_id": run_id}

    async def deliver_accept(
        self, task_id: str, run_id: str, **kwargs: Any
    ) -> DeliveryRecord:
        delivery = await self.delivery.accept(task_id, run_id, **kwargs)
        await self.publish_task_update(task_id)
        return delivery

    async def deliver_run_op(
        self, task_id: str, run_id: str, **kwargs: Any
    ) -> DeliveryRecord:
        delivery = await self.delivery.deliver_op(task_id, run_id, **kwargs)
        await self.publish_task_update(task_id)
        return delivery

    async def deliver_teardown(
        self, task_id: str, run_id: str, **kwargs: Any
    ) -> DeliveryRecord:
        delivery = await self.delivery.teardown(task_id, run_id, **kwargs)
        await self.publish_task_update(task_id)
        return delivery

    async def list_deployments(self) -> list[Any]:
        """Return the durable local-deploy history for the operator surface."""
        return await self.repo.list_deployments()

    # --- release-line deploy (docs/plans/release-line-deploy.md §3) ------

    async def release_stage(self, board_id: str, **kwargs: Any) -> tuple[Any, Any]:
        """Resolve the board's release ref and stage it into the idle slot."""
        release, op = await self.delivery.release_stage(board_id, **kwargs)
        await self.publish_board_update(board_id)
        return release, op

    async def release_switch(self, board_id: str, **kwargs: Any) -> tuple[Any, Any]:
        """Start the explicitly user-authorized release-line switch handoff."""
        release, op = await self.delivery.release_switch(board_id, **kwargs)
        await self.publish_board_update(board_id)
        return release, op

    async def list_release_deployments(
        self, board_id: str, *, limit: int = 10, offset: int = 0
    ) -> tuple[list[Any], int]:
        """Return a page of the durable release-line history for the board's
        Releases surface (task-board-overhaul.md §3.2), plus the total count
        for "load more" pagination."""
        return await self.repo.list_release_deployments(board_id, limit=limit, offset=offset)

    async def get_current_release_deployments(self, board_id: str) -> tuple[Any, Any]:
        """The board's current live/staged rows, independent of history
        pagination — reuses the same lookups the coordinator uses to plan a
        stage/switch, so the header summary is never a page-window artifact."""
        live = await self.repo.get_live_release(board_id)
        staged = await self.repo.get_staged_release(board_id)
        return live, staged

    async def resolve_release_remote_tip(self, board_id: str) -> str | None:
        """Best-effort current sha of the board's configured release branch at
        its remote — the "remote tip" half of the Releases surface's "remote
        tip vs live sha" display (release-line-deploy.md §3.4). Read-only and
        never raises: a transient network/remote hiccup must not break the
        release list itself, only leave the tip comparison blank, exactly the
        same resolver `release_stage` uses to plan a candidate."""
        board = await self.repo.get_board(board_id)
        if not board.allow_local_deploy:
            return None
        remote_url = await ws.remote_url(board.working_dir, board.git_delivery_remote)
        if remote_url is None:
            return None
        try:
            return await ws.remote_release_ref_tip(
                board.working_dir, remote_url, board.deploy_release_ref
            )
        except ws.WorkspaceError:
            return None

    async def release_rollback(self, board_id: str, **kwargs: Any) -> tuple[Any, Any]:
        """Run a confirmed rollback of the board's live release through the
        ordinary release-switch path."""
        release, op = await self.delivery.release_rollback(board_id, **kwargs)
        await self.publish_board_update(board_id)
        return release, op

    @staticmethod
    def _delivery_terminal_source(
        task_id: str, run_id: str, settle_op_id: str | None = None
    ) -> str:
        base = f"task:{task_id}:run:{run_id}:delivery:terminal"
        return f"{base}:{settle_op_id}" if settle_op_id else base

    async def _notify_delivery_terminal(
        self, task: TaskRecord, delivery: DeliveryRecord, settle_op_id: str | None = None
    ) -> None:
        if not task.origin_session_id or self.session_mgr is None or self.db is None:
            return
        if not await self.db.session_exists(task.origin_session_id):
            await self.repo.record_delivery_notification_unavailable(
                delivery.id, reason="origin session was deleted"
            )
            return
        label = {
            "delivered": "delivered",
            "conflicted": "hit a merge conflict",
            "blocked": "is blocked",
            "failed": "failed",
        }.get(delivery.status, delivery.status)
        detail = delivery.reason_detail or (
            f"PR #{delivery.pr_number}" if delivery.pr_number
            else delivery.pushed_ref or "See the Task Board delivery panel."
        )
        prompt = (
            f"[task-delivery task={task.id} run={delivery.run_id} "
            f"status={delivery.status}]\n"
            f"Git delivery for **{task.title}** {label}.\n\n{detail}\n\n"
            "Inspect the Task Board delivery panel before any follow-up. Never "
            "automatically retry interrupted external Git/PR work."
        )
        await self.session_mgr.enqueue_session_injection(
            source_key=self._delivery_terminal_source(
                task.id, delivery.run_id, settle_op_id
            ),
            session_id=task.origin_session_id,
            prompt=prompt,
        )

    async def recover_deliveries(self) -> DeployProbation | None:
        """Boot recovery for deliveries (task-git-delivery.md §16). DB-only — no
        hosting-platform network I/O in the injection-paused barrier (S3).

        Returns a `DeployProbation` when this boot is a flipped-but-unconfirmed
        deploy candidate (§7.5): the caller must then keep producers paused and
        run `run_deploy_probation` before starting them. A release-line switch
        op flipped and a per-run one flipped can never both be true — the two
        share the same slot-level `deployments_one_active` lock — so at most
        one of the two reconcile calls below can return a probation."""
        if self.session_mgr is not None and not self.session_mgr.session_injection_dispatch_paused:
            raise RuntimeError("delivery recovery requires paused injection dispatch")
        # deploy_switch ops FIRST: a running one is not "unknown", it is journal-
        # reconciled (§8), and must be resolved before the generic interrupt
        # sweep (which deliberately skips deploy_switch) can run. Release-line
        # switch/rollback ops are journal-reconciled the same way and must also
        # resolve before any generic sweep.
        probation = await self.reconcile_deploy_switch_ops()
        release_probation = await self.reconcile_release_switch_ops()
        probation = probation or release_probation
        await self.repo.interrupt_running_delivery_ops(
            reason="server restarted; delivery op outcome unknown"
        )
        await self.repo.interrupt_running_release_stage_ops(
            reason="server restarted; release op outcome unknown"
        )
        # A deploy_stage that died mid-pipeline leaves its `deployments` row
        # `staging`, which holds the global deploy lock; interrupting the op does
        # not release it. Fail those orphans so deploys are not wedged forever
        # (docs/plans/local-deploy.md §5). The staging never touched the running
        # instance, so this is DB-only and safe inside the boot barrier.
        await self.repo.fail_orphan_staging_deployments(
            reason="server restarted; deploy_stage interrupted"
        )
        # Same for a release stage: its release row AND its born-together
        # `deployments` row both leave `staging` together (release-line-
        # deploy.md §3.3).
        await self.repo.fail_orphan_staging_releases(
            reason="server restarted; release stage interrupted"
        )
        await self.repo.reset_preparing_deliveries()
        # Data self-heal (task-board-gaps open-pr-500.md §4): a delivery left
        # `blocked` despite already carrying a `pr_number` is exactly the shape
        # the terminal-notification idempotency-key collision produced — the PR
        # op itself had already succeeded before the notify call crashed the
        # response and a subsequent retry's `_fail` overwrote the status on top
        # of it. Idempotent and unconditional so it also self-heals any future
        # occurrence, not just the two historical rows this ticket names.
        for fixed in await self.repo.reconcile_blocked_deliveries_with_pr():
            logger.info(
                "delivery %s (task %s) self-healed blocked -> delivered "
                "(pr_number=%s already recorded)",
                fixed.id, fixed.task_id, fixed.pr_number,
            )
        if self.session_mgr is None or self.db is None:
            return
        # B2: reconstruct the terminal-delivery outbox source for live origins.
        # The existence check must use the SAME key the live notify path would
        # use (the settle event's own op-scoped key), not the old unscoped key
        # — otherwise a stale unscoped row from a prior event wrongly skips a
        # still-missing op-scoped notification, or a fresh unscoped enqueue
        # duplicates one already sent under its op-scoped key (open-pr-500.md
        # §4 blocker-2). Isolated per-delivery: one bad row must not abort the
        # rest of this boot pass.
        for task, delivery in await self.repo.list_terminal_deliveries():
            if not task.origin_session_id:
                continue
            try:
                settle_op_id = await self.delivery.current_terminal_settle_op_id(delivery.id)
                source_key = self._delivery_terminal_source(
                    task.id, delivery.run_id, settle_op_id
                )
                if await self.db.get_session_injection_by_source(source_key):
                    continue
                if not await self.db.session_exists(task.origin_session_id):
                    await self.repo.record_delivery_notification_unavailable(
                        delivery.id, reason="origin session was deleted"
                    )
                    continue
                await self._notify_delivery_terminal(task, delivery, settle_op_id)
            except Exception:
                logger.exception(
                    "boot delivery-notification reconstruction failed for delivery "
                    "%s (task %s); continuing with the remaining deliveries",
                    delivery.id, task.id,
                )
        return probation

    # --- deploy_switch boot reconciliation + probation (§7.5/§8) ---------

    async def reconcile_deploy_switch_ops(self) -> DeployProbation | None:
        """For every `deploy_switch` op left `running`, read the switcher journal
        and settle it per the §8 table — DB-and-local-file only, no network (S3).

        Fast path: no running switch op → return immediately, reading no journal
        (the common boot's one cheap DB query). At most one op can be a probation
        candidate (the global deploy lock serializes deploys); it is returned and
        left `running` for the probation monitor."""
        ops = await self.repo.list_running_deploy_switch_ops()
        if not ops:
            return None
        root = settings.resolved_deploy_root
        if not root:
            # A running switch op but no deploy_root to find its journal — the
            # config changed out from under it. It cannot be confirmed; interrupt.
            # No `target_slot`/`target_sha` fallback is possible here (unlike the
            # journal-tail branches below): that locator is derived by reading the
            # journal's `handoff` line, and with no `deploy_root` we cannot even
            # find the journal file. If the bound deployment row's `op_id` was
            # also reverted by a snapshot restore (§7.4) in this same window, it
            # is left `staged`/`switching` — a narrower, config-loss-triggered gap
            # than the snapshot-restore-during-a-live-journal case this change
            # fixes; a human already has to intervene to restore `deploy_root`.
            for op in ops:
                await self.repo.finalize_deploy_interrupted(
                    op.id, reason="deploy_root unset at boot; switch state unknown"
                )
            return None
        journal = switcher.Journal(str(Path(root) / deploy.JOURNAL_NAME))
        now = _now()
        health_timeout = float(settings.deploy_health_timeout_seconds)
        probation: DeployProbation | None = None
        for op in ops:
            entries = journal.entries(op.id)
            if await self._apply_switch_terminal(op.id, entries) is not None:
                continue
            tail = entries[-1] if entries else None
            step = tail.get("step") if tail else None
            flipped_at = self._journal_timestamp(tail)
            if (
                step == switcher.STEP_FLIP_DONE
                and flipped_at is not None
                and self._journal_fresh(tail, now, health_timeout)
            ):
                # Flipped, fresh, not yet confirmed → this boot is under probation.
                probation = DeployProbation(
                    op_id=op.id,
                    journal_path=str(journal.path),
                    health_deadline=flipped_at + timedelta(seconds=health_timeout),
                )
            else:
                reason = (
                    "stale flip_done; switcher never confirmed"
                    if step == switcher.STEP_FLIP_DONE
                    else "handoff recorded but switcher never confirmed"
                )
                target_slot, target_sha = self._handoff_target(entries)
                await self.repo.finalize_deploy_interrupted(
                    op.id, reason=reason, journal_excerpt={"tail": entries[-6:]},
                    target_slot=target_slot, target_sha=target_sha,
                )
        return probation

    async def reconcile_release_switch_ops(self) -> DeployProbation | None:
        """For every release-line switch/rollback op left `running`, read the
        SAME switcher journal and settle it per the §8 table — the
        release-line mirror of `reconcile_deploy_switch_ops`
        (release-line-deploy.md §3.3). Both op families share one journal
        file (one deploy_root, one switcher) and the same slot-level lock, so
        this is the identical algorithm keyed to a different DB table."""
        ops = await self.repo.list_running_release_switch_ops()
        if not ops:
            return None
        root = settings.resolved_deploy_root
        if not root:
            for op in ops:
                await self.repo.finalize_release_interrupted(
                    op.id, reason="deploy_root unset at boot; switch state unknown"
                )
            return None
        journal = switcher.Journal(str(Path(root) / deploy.JOURNAL_NAME))
        now = _now()
        health_timeout = float(settings.deploy_health_timeout_seconds)
        probation: DeployProbation | None = None
        for op in ops:
            entries = journal.entries(op.id)
            if await self._apply_switch_terminal(op.id, entries, kind="release") is not None:
                continue
            tail = entries[-1] if entries else None
            step = tail.get("step") if tail else None
            flipped_at = self._journal_timestamp(tail)
            if (
                step == switcher.STEP_FLIP_DONE
                and flipped_at is not None
                and self._journal_fresh(tail, now, health_timeout)
            ):
                probation = DeployProbation(
                    op_id=op.id,
                    journal_path=str(journal.path),
                    health_deadline=flipped_at + timedelta(seconds=health_timeout),
                    kind="release",
                )
            else:
                reason = (
                    "stale flip_done; switcher never confirmed"
                    if step == switcher.STEP_FLIP_DONE
                    else "handoff recorded but switcher never confirmed"
                )
                target_slot, target_sha = self._handoff_target(entries)
                await self.repo.finalize_release_interrupted(
                    op.id, reason=reason, journal_excerpt={"tail": entries[-6:]},
                    target_slot=target_slot, target_sha=target_sha,
                )
        return probation

    async def run_deploy_probation(
        self, probation: DeployProbation, *, on_release: Callable[[], Awaitable[None]]
    ) -> str:
        """Hold under probation (§7.5): poll the switcher journal until it goes
        terminal or `deploy_health_timeout_seconds` elapses, finalize the op, then
        `on_release` (start the deferred producers). A bounded LOCAL poll — no
        network — so it never violates the boot barrier's S3 rule."""
        journal = switcher.Journal(probation.journal_path)
        remaining = max(0.0, (probation.health_deadline - _now()).total_seconds())
        deadline = time.monotonic() + remaining
        outcome = "timeout"
        finalize_interrupted = (
            self.repo.finalize_release_interrupted if probation.kind == "release"
            else self.repo.finalize_deploy_interrupted
        )
        try:
            while True:
                entries = journal.entries(probation.op_id)
                applied = await self._apply_switch_terminal(
                    probation.op_id, entries, kind=probation.kind
                )
                if applied is not None:
                    outcome = applied
                    break
                if time.monotonic() >= deadline:
                    # Still non-terminal after the original health window → now stale.
                    target_slot, target_sha = self._handoff_target(entries)
                    await finalize_interrupted(
                        probation.op_id,
                        reason="probation timed out; switcher never confirmed",
                        target_slot=target_slot, target_sha=target_sha,
                    )
                    break
                await asyncio.sleep(
                    min(self._probation_poll_interval, max(0.0, deadline - time.monotonic()))
                )
        finally:
            await on_release()
        return outcome

    @staticmethod
    def _handoff_target(entries: list[dict[str, Any]]) -> tuple[str | None, str | None]:
        """The `to_slot`/`new_sha` this op's `handoff` journal line targeted, or
        `(None, None)` if there is no handoff line yet — the restricted fallback
        locator for a deployment row a snapshot restore can unbind from its op id
        (§7.4; see `_finalize_switch`'s docstring in repository.py). Read from the
        journal, never the DB, so it works even when the DB row itself is the
        thing that reverted."""
        for entry in entries:
            if entry.get("step") == switcher.STEP_HANDOFF:
                detail = entry.get("detail") or {}
                slot = detail.get("to_slot")
                sha = detail.get("new_sha")
                return (str(slot) if slot else None, str(sha) if sha else None)
        return (None, None)

    async def _apply_switch_terminal(
        self, op_id: str, entries: list[dict[str, Any]], *, kind: str = "delivery"
    ) -> str | None:
        """If the journal tail for `op_id` is terminal, settle the op per §8 and
        return the step name; otherwise return None. Shared by boot reconciliation
        and the probation monitor so both apply the §8 table identically.

        ``kind`` selects the finalizer family: ``"delivery"`` for a per-run
        `deploy_switch` op (`task_delivery_ops`), ``"release"`` for a
        board-level switch/rollback op (`release_deployment_ops`,
        release-line-deploy.md §3.3) — same journal-tail table, different
        durable home for the verdict."""
        tail = entries[-1] if entries else None
        step = tail.get("step") if tail else None
        if step not in _SWITCH_TERMINAL_STEPS:
            return None
        detail = (tail.get("detail") or {}) if tail else {}
        excerpt = {"tail": entries[-6:]}
        target_slot, target_sha = self._handoff_target(entries)
        finalize_switched = (
            self.repo.finalize_release_switched if kind == "release"
            else self.repo.finalize_deploy_switched
        )
        finalize_rolled_back = (
            self.repo.finalize_release_rolled_back if kind == "release"
            else self.repo.finalize_deploy_rolled_back
        )
        finalize_rollback_incomplete = (
            self.repo.finalize_release_rollback_incomplete if kind == "release"
            else self.repo.finalize_deploy_rollback_incomplete
        )
        finalize_old_wont_die = (
            self.repo.finalize_release_old_wont_die if kind == "release"
            else self.repo.finalize_deploy_old_wont_die
        )
        finalize_interrupted = (
            self.repo.finalize_release_interrupted if kind == "release"
            else self.repo.finalize_deploy_interrupted
        )
        if step == switcher.STEP_SWITCHED_OK:
            await finalize_switched(
                op_id, deployed_sha=str(detail.get("sha", "")),
                deployed_slot=str(detail.get("slot", "")), journal_excerpt=excerpt,
            )
        elif step == switcher.STEP_ROLLED_BACK:
            await finalize_rolled_back(
                op_id, reason=str(detail.get("reason", "health_failed")),
                journal_excerpt=excerpt,
                target_slot=target_slot, target_sha=target_sha,
            )
        elif step == switcher.STEP_ROLLBACK_INCOMPLETE:
            await finalize_rollback_incomplete(
                op_id,
                reason=(
                    f"rollback_incomplete at {detail.get('stage', '?')} "
                    f"({detail.get('reason', '?')})"
                ),
                journal_excerpt=excerpt,
                target_slot=target_slot, target_sha=target_sha,
            )
        elif step == switcher.STEP_OLD_WONT_DIE:
            await finalize_old_wont_die(
                op_id, journal_excerpt=excerpt,
                target_slot=target_slot, target_sha=target_sha,
            )
        else:  # STEP_SWITCH_ERROR — a bad/missing handoff; the flip never happened.
            await finalize_interrupted(
                op_id, reason=f"switch_error: {detail.get('reason', '?')}",
                journal_excerpt=excerpt,
                target_slot=target_slot, target_sha=target_sha,
            )
        return step

    @staticmethod
    def _journal_timestamp(tail: Mapping[str, Any] | None) -> datetime | None:
        """Parse a journal timestamp as UTC, or fail closed on malformed input."""
        ts = tail.get("ts") if tail else None
        if not ts:
            return None
        try:
            when = datetime.fromisoformat(str(ts))
        except ValueError:
            return None
        return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when

    @staticmethod
    def _journal_fresh(
        tail: Mapping[str, Any] | None, now: datetime, timeout: float
    ) -> bool:
        """A journal line is fresh if it was written within the health window —
        the boundary between a live probation and a stale, abandoned switch (§8).
        An unparseable/absent timestamp reads as stale (fail-safe: interrupt)."""
        when = TaskBoardManager._journal_timestamp(tail)
        if when is None:
            return False
        age = (now - when).total_seconds()
        return 0 <= age < timeout

    def bind_deploy_switch(
        self,
        *,
        broadcast_restarting: Callable[[dict[str, Any]], Awaitable[None]],
        request_shutdown: Callable[[], None],
        bg_task_manager: Any = None,
        research_manager: Any = None,
        bridge_manager_getter: Callable[[], Any] | None = None,
        scheduler_getter: Callable[[], Any] | None = None,
        parked_scheduler_getter: Callable[[], Any] | None = None,
        spawn_switcher: Callable[..., int] | None = None,
    ) -> None:
        """Assemble the quiesce provider from the live subsystems and wire the
        switch effects into the delivery coordinator (§7). Called from the
        FastAPI lifespan once every producer exists."""
        quiesce = DeployQuiesce(
            session_manager=self.session_mgr,
            repo=self.repo,
            db=self.db,
            bg_task_manager=bg_task_manager,
            research_manager=research_manager,
            pause_dispatch=self.pause_dispatch_for_deploy,
            resume_dispatch=self.resume_dispatch_for_deploy,
            bridge_manager_getter=bridge_manager_getter,
            scheduler_getter=scheduler_getter,
            parked_scheduler_getter=parked_scheduler_getter,
            admission_gate=self.session_mgr.deploy_admission_gate,
        )
        self.delivery.bind_deploy(
            quiesce=quiesce,
            broadcast_restarting=broadcast_restarting,
            request_shutdown=request_shutdown,
            spawn_switcher=spawn_switcher,
            admission_gate=self.session_mgr.deploy_admission_gate,
        )

    async def reconcile_interrupted_prs(self) -> None:
        """Off the boot critical path (S3): bounded read-only reconcile of any
        interrupted PR op, never a re-POST. Never blocks the dispatcher/drain."""
        try:
            for task, delivery in await self.repo.list_terminal_deliveries():
                if not (
                    delivery.status == "blocked"
                    and delivery.reason_kind == "interrupted"
                    and delivery.pr_number is None
                ):
                    continue
                updated, settle_op_id = await self.delivery.reconcile_interrupted_pr(delivery)
                if updated.status != delivery.status:
                    await self._notify_delivery_terminal(task, updated, settle_op_id)
                    await self.publish_task_update(task.id)
        except Exception:
            logger.exception("interrupted-PR reconcile pass failed")


task_board_manager = TaskBoardManager()
