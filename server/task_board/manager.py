"""Dispatcher, worker protocol, recovery, and notifications for Task Board."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from ..config import settings
from ..connector_manager import ConnectorManager
from .delivery import DeliveryCoordinator, delivery_coordinator
from .models import DeliveryRecord, RunRecord, TaskBoardError, TaskConflictError, TaskRecord
from .prompts import render_assignment_prompt
from .repository import TaskRepository, task_repository
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

    async def _dispatcher_pass(self) -> None:
        await self.repo.reconcile_eligibility()
        await self._refresh_liveness()
        ready = await self.repo.list_tasks(status="ready", limit=1000)
        for task in ready:
            if self._stopping:
                return
            try:
                await self._dispatch_task(task)
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
            session = await self.session_mgr.create_session(
                agent_id=agent["id"],
                name=f"Task: {task.title}",
                working_dir=prepared.path,
                origin="task",
                backend=agent.get("backend") or "claude-code",
                task_id=task.id,
                task_run_id=run.id,
            )
            run = await self.repo.attach_run_session(task.id, run.id, session.id)
            prompt = render_assignment_prompt(
                task=task, board=board, run=run, workspace=prepared.path
            )
            await self.session_mgr.start_message(session.id, prompt)
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
            run = await self.repo.get_run(task.current_run_id)
            task, final = await self.repo.cancel_run(task_id, run.id, reason=reason)
            await self._notify_terminal(task, final)
            if run.session_id and self.session_mgr is not None:
                await self.session_mgr.interrupt(run.session_id)
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
        task = await self.repo.get_task(task_id)
        payload = event.to_dict()
        payload["payload"] = {**payload["payload"], "task": task.to_dict()}
        await self.session_mgr._broadcast(
            {
                "type": "task_event",
                "board_id": task.board_id,
                "task_id": task.id,
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

    @staticmethod
    def _delivery_terminal_source(task_id: str, run_id: str) -> str:
        return f"task:{task_id}:run:{run_id}:delivery:terminal"

    async def _notify_delivery_terminal(
        self, task: TaskRecord, delivery: DeliveryRecord
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
            source_key=self._delivery_terminal_source(task.id, delivery.run_id),
            session_id=task.origin_session_id,
            prompt=prompt,
        )

    async def recover_deliveries(self) -> None:
        """Boot recovery for deliveries (task-git-delivery.md §16). DB-only — no
        hosting-platform network I/O in the injection-paused barrier (S3)."""
        if self.session_mgr is not None and not self.session_mgr.session_injection_dispatch_paused:
            raise RuntimeError("delivery recovery requires paused injection dispatch")
        await self.repo.interrupt_running_delivery_ops(
            reason="server restarted; delivery op outcome unknown"
        )
        await self.repo.reset_preparing_deliveries()
        if self.session_mgr is None or self.db is None:
            return
        # B2: reconstruct the terminal-delivery outbox source for live origins.
        for task, delivery in await self.repo.list_terminal_deliveries():
            if not task.origin_session_id:
                continue
            source_key = self._delivery_terminal_source(task.id, delivery.run_id)
            if await self.db.get_session_injection_by_source(source_key):
                continue
            if not await self.db.session_exists(task.origin_session_id):
                await self.repo.record_delivery_notification_unavailable(
                    delivery.id, reason="origin session was deleted"
                )
                continue
            await self._notify_delivery_terminal(task, delivery)

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
                updated = await self.delivery.reconcile_interrupted_pr(delivery)
                if updated.status != delivery.status:
                    await self._notify_delivery_terminal(task, updated)
                    await self.publish_task_update(task.id)
        except Exception:
            logger.exception("interrupted-PR reconcile pass failed")


task_board_manager = TaskBoardManager()
