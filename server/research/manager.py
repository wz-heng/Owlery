"""ResearchManager — tracks deep-research jobs as async tasks
(native-deep-research.md §6). Mirrors the bg-task / delegation managers:

  - `start(session_id, question)` persists a `research_jobs` row, returns the
    id immediately, and runs the pipeline as a tracked `asyncio.Task`.
  - Progress is broadcast over the session bus + written to the row's `phase`.
  - On success the report is atomically written to a file, then delivered via
    the crash-safe `session_injections` outbox; completion and transcript
    delivery are separate durable facts.
  - `cancel(job_id)` cancels the task — leaves re-raise CancelledError and reap
    their process groups, so nothing orphans.
  - A global semaphore bounds concurrent JOBS (per-job leaf concurrency is
    bounded inside the pipeline); a hard per-job timeout backstops everything.
  - A boot sweep marks restart-orphaned `running` rows interrupted.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..config import settings
from ..harness import get_harness, has_backend
from .orchestrator import ResearchLimits, ResearchProgress, run_research

if TYPE_CHECKING:
    from ..database import Database
    from ..session_manager import SessionManager

logger = logging.getLogger(__name__)


class ResearchError(Exception):
    """Surface-level error with an HTTP status for the REST layer."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _research_dir() -> str:
    d = settings.resolved_research_dir
    os.makedirs(d, exist_ok=True)
    return d


class ResearchManager:
    """App-lifetime singleton; bound in main.py's lifespan."""

    def __init__(self) -> None:
        self.session_mgr: "SessionManager | None" = None
        self.db: "Database | None" = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._job_sem: asyncio.Semaphore | None = None
        self._shutting_down = False

    def bind(self, session_mgr: "SessionManager", db: "Database") -> None:
        self.session_mgr = session_mgr
        self.db = db
        self._job_sem = asyncio.Semaphore(max(1, settings.research_max_concurrent_jobs))
        self._shutting_down = False

    async def shutdown(self) -> None:
        """Interrupt live jobs while the DB is still available."""
        self._shutting_down = True
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def recover_interrupted(self) -> int:
        """Boot sweep running jobs and re-enqueue completed reports."""
        if self.db is None or self.session_mgr is None:
            return 0
        n = await self.db.mark_in_flight_research_jobs_interrupted(_now())
        if n:
            logger.info("research: marked %d interrupted job(s) on boot", n)
        # Compatibility rescue for the old two-step delivery path: a process
        # could die after marking completion but before the report became a
        # transcript message. New jobs also use this path after any such crash; a
        # completed job now guarantees a durable report file.
        for row in await self.db.list_pending_research_deliveries():
            existing = await self.db.get_session_injection_by_source(
                f"research:{row['id']}"
            )
            if existing and existing["status"] == "delivered":
                await self.db.update_research_job(
                    row["id"],
                    injection_status="delivered",
                    injected_at=existing["delivered_at"] or _now(),
                )
                continue
            if existing and existing["status"] == "failed":
                await self.db.update_research_job(
                    row["id"],
                    injection_status="failed",
                    error=f"delivery failed: {existing['error'] or 'unknown error'}",
                )
                continue
            if existing and existing["status"] == "pending":
                # The centralized post-recovery drain replays this row later
                # in the boot sequence. Its stored prompt is already the
                # durable payload; do not require or re-render the report.
                continue
            path = row.get("report_path")
            try:
                if not path:
                    raise OSError("completed research has no report_path")
                with open(path, encoding="utf-8") as fh:
                    report_text = fh.read()
                await self._enqueue_report(
                    row["id"], row["session_id"], row["question"], report_text
                )
            except Exception as exc:  # legacy damaged row; surface truthfully
                logger.warning(
                    "research %s: cannot recover pending report: %s",
                    row["id"], exc,
                )
                await self.db.update_research_job(
                    row["id"],
                    injection_status="failed",
                    error=f"delivery recovery failed: {exc}",
                )
        return n

    # ----------------------------------------------------------------- start

    async def start(self, session_id: str, question: str) -> dict[str, Any]:
        """Create + launch a research job for `session_id`. Returns the row."""
        if self.session_mgr is None or self.db is None:
            raise ResearchError("ResearchManager not bound", status_code=500)
        question = (question or "").strip()
        if not question:
            raise ResearchError("question must be a non-empty string", status_code=400)

        session = self.session_mgr.get_session(session_id)
        if session is None:
            raise ResearchError(f"session {session_id} not found", status_code=404)
        if not has_backend(session.backend):
            raise ResearchError(f"unknown backend {session.backend!r}", status_code=400)
        harness = get_harness(session.backend)
        if harness.profile.web is None:
            raise ResearchError(
                f"the {session.backend} backend has no web tools, so deep "
                "research isn't available on it",
                status_code=409,
            )

        job_id = uuid.uuid4().hex[:12]
        await self.db.create_research_job(job_id, session_id, question, _now())
        await self._broadcast({
            "type": "research_started",
            "session_id": session_id,
            "research_id": job_id,
            "question": question,
        })
        task = asyncio.create_task(self._run_job(job_id, session_id, question))
        self._tasks[job_id] = task
        task.add_done_callback(lambda _t, jid=job_id: self._tasks.pop(jid, None))
        return await self.db.get_research_job(job_id)

    # ------------------------------------------------------------------- run

    async def _run_job(self, job_id: str, session_id: str, question: str) -> None:
        assert self.session_mgr is not None and self.db is not None
        assert self._job_sem is not None
        try:
            async with self._job_sem:  # bound concurrent jobs
                session = self.session_mgr.get_session(session_id)
                if session is None:
                    raise ResearchError("session disappeared", status_code=404)
                harness = get_harness(session.backend)
                agent = await self.db.get_agent(session.agent_id) if session.agent_id else None
                model = (agent or {}).get("model")
                credential = await self.session_mgr.resolve_credential_by_id(
                    session.credential_id or (agent or {}).get("credential_id"),
                    style=harness.profile.credential_style,
                    context=f"research {job_id}",
                )

                async def on_progress(p: ResearchProgress) -> None:
                    await self.db.update_research_job(job_id, phase=p.phase)
                    await self._broadcast({
                        "type": "research_progress",
                        "session_id": session_id,
                        "research_id": job_id,
                        "phase": p.phase,
                        "detail": p.detail,
                        "counts": p.counts,
                    })

                # Run leaves in an EMPTY per-job scratch cwd, NOT the session's
                # working_dir — a web/reasoning leaf has no reason to read the
                # user's repo, and this keeps the read-only-sandbox/denylist
                # leaves from touching real files (Vera review).
                scratch = os.path.join(_research_dir(), job_id, "cwd")
                os.makedirs(scratch, exist_ok=True)
                report = await asyncio.wait_for(
                    run_research(
                        question,
                        harness=harness,
                        credential=credential,
                        model=model,
                        working_dir=scratch,
                        limits=ResearchLimits(),
                        on_progress=on_progress,
                    ),
                    timeout=settings.research_job_timeout_seconds,
                )

            # Persist the report file durably before claiming completion.
            report_path = self._write_report(job_id, report.report)
            await self.db.update_research_job(
                job_id, status="completed", phase="done", cost=report.cost,
                completed_at=_now(), report_path=report_path,
            )
            # Consumption ledger (usage-tracking.md §4): one row per completed
            # job — the same best-effort boundary as research_jobs.cost (failed
            # / cancelled jobs lose their partial usage). Never fails the job.
            try:
                usage = report.usage
                await self.db.add_turn_usage(
                    created_at=_now(),
                    session_id=session_id,
                    agent_id=session.agent_id,
                    backend=session.backend,
                    model=model or None,
                    cost=report.cost,
                    input_tokens=usage.input_tokens if usage else 0,
                    cache_read_tokens=usage.cache_read_tokens if usage else 0,
                    cache_creation_tokens=usage.cache_creation_tokens if usage else 0,
                    output_tokens=usage.output_tokens if usage else 0,
                    reasoning_tokens=usage.reasoning_tokens if usage else 0,
                    origin="research",
                )
            except Exception:
                logger.exception("failed to record research usage for job %s", job_id)
            await self._broadcast({
                "type": "research_completed",
                "session_id": session_id,
                "research_id": job_id,
                "sources": report.sources,
                "verified": len(report.findings),
            })
            await self._enqueue_report(
                job_id, session_id, question, report.report
            )
        except asyncio.CancelledError:
            if self._shutting_down:
                await self._finalize_failed(
                    job_id,
                    session_id,
                    "interrupted",
                    "server shut down while research was running",
                )
            else:
                await self._finalize_failed(
                    job_id, session_id, "cancelled", "cancelled by user"
                )
            raise
        except asyncio.TimeoutError:
            await self._finalize_failed(
                job_id, session_id, "failed",
                f"research exceeded {settings.research_job_timeout_seconds}s and was stopped",
            )
        except ResearchError as e:
            await self._finalize_failed(job_id, session_id, "failed", e.message)
        except Exception as e:  # noqa: BLE001
            logger.exception("research job %s crashed", job_id)
            await self._finalize_failed(job_id, session_id, "failed", str(e))

    async def _enqueue_report(
        self, job_id: str, session_id: str, question: str, report_text: str
    ) -> None:
        """Persist the report-delivery intent before scheduling its turn."""
        assert self.db is not None and self.session_mgr is not None
        prompt = (
            f"[deep-research:{job_id}] Research complete for: {question}\n\n"
            f"{report_text}"
        )
        try:
            injection = await self.session_mgr.enqueue_session_injection(
                source_key=f"research:{job_id}",
                session_id=session_id,
                prompt=prompt,
            )
            # The outbox is authoritative. Repair the legacy compatibility
            # mirror when enqueue returns an already-terminal source row (for
            # example after a crash between the trigger ack and mirror write).
            if injection["status"] == "delivered":
                await self.db.update_research_job(
                    job_id,
                    injection_status="delivered",
                    injected_at=injection["delivered_at"] or _now(),
                )
            elif injection["status"] == "failed":
                await self.db.update_research_job(
                    job_id,
                    injection_status="failed",
                    error=f"delivery failed: {injection['error'] or 'unknown error'}",
                )
        except Exception as e:  # noqa: BLE001 — parent may be gone; tolerate
            logger.warning("research %s: report enqueue failed: %s", job_id, e)
            await self.db.update_research_job(
                job_id, injection_status="failed", error=f"delivery failed: {e}"
            )

    async def _finalize_failed(
        self, job_id: str, session_id: str, status: str, error: str
    ) -> None:
        assert self.db is not None
        try:
            # Idempotent: if the job is already terminal (e.g. cancel() recorded
            # it before interrupting), don't overwrite or re-broadcast (Vera
            # review — avoids the cancel double-write/double-event).
            row = await self.db.get_research_job(job_id)
            if row and row["status"] != "running":
                return
            await self.db.update_research_job(
                job_id, status=status, error=error, completed_at=_now()
            )
            await self._broadcast({
                "type": "research_failed",
                "session_id": session_id,
                "research_id": job_id,
                "status": status,
                "error": error,
            })
        except Exception:
            logger.exception("research %s: finalize(%s) failed", job_id, status)

    # ---------------------------------------------------------------- cancel

    async def cancel(self, job_id: str) -> dict[str, Any]:
        if self.db is None:
            raise ResearchError("ResearchManager not bound", status_code=500)
        row = await self.db.get_research_job(job_id)
        if row is None:
            raise ResearchError(f"research job {job_id} not found", status_code=404)
        # Record cancelled + broadcast BEFORE interrupting live work, so the
        # state transition is authoritative and the REST caller/UI never see a
        # stale `running` (Vera review — cancel-as-state-transition). The
        # CancelledError path's _finalize_failed is now idempotent, so it won't
        # double-write. No-op if already terminal.
        if row["status"] == "running":
            await self.db.update_research_job(
                job_id, status="cancelled", error="cancelled by user",
                completed_at=_now(),
            )
            await self._broadcast({
                "type": "research_failed",
                "session_id": row["session_id"],
                "research_id": job_id,
                "status": "cancelled",
                "error": "cancelled by user",
            })
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()  # reaps in-flight leaves via CancelledError
        return await self.db.get_research_job(job_id)

    # --------------------------------------------------------------- helpers

    def _write_report(self, job_id: str, report: str) -> str:
        path = os.path.join(_research_dir(), f"{job_id}.md")
        tmp = f"{path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(report)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            return path
        except Exception as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise ResearchError(
                f"could not persist research report: {exc}", status_code=500
            ) from exc

    async def _broadcast(self, msg: dict[str, Any]) -> None:
        if self.session_mgr is not None:
            try:
                await self.session_mgr._broadcast(msg)
            except Exception:
                logger.exception("research broadcast failed")


# Module-level singleton (mirrors session_manager / bg_tasks / delegations).
research_manager = ResearchManager()
