"""The quiesce gate for `deploy_switch` (docs/plans/local-deploy.md §7.1).

A switch is the one operation that must not run while *anything* is doing work:
its side effect is the whole instance restarting, so a running turn, task run,
bg task, research job, delegation child, pending injection, or another delivery
op would be silently torn down. The census below enumerates every such busy
source so the coordinator can refuse (`blocked(not_idle)`) with an exact list —
and the drain primitive can pause the producers that would otherwise keep the
census from ever reaching zero.

There is deliberately **no force override** (§7.1): killing running work to
deploy faster is precisely the harm this feature promises not to do. `drain`
only *pauses new work* and *waits*; it never interrupts what is already running.

Everything is injected so each busy source is unit-testable in isolation and the
pause/resume primitives can be spied without a live server (§14).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from ..deploy_admission import DeployAdmissionGate


@dataclass(frozen=True, slots=True)
class BusySource:
    """One reason the instance is not idle. ``kind`` is a stable machine label
    (for the UI census, §11); ``detail`` identifies the specific work item."""

    kind: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "detail": self.detail}


class _SessionRegistry(Protocol):
    sessions: dict[str, Any]


class DeployQuiesce:
    """Census + drain-pause primitives for the switch gate.

    Constructed once at boot with the live subsystem handles (or fakes in
    tests). ``bridge_manager``/``schedule_runner``/``parked_turn_runner`` are
    created inside the FastAPI lifespan, so they arrive as zero-arg getters that
    resolve them lazily (they may not exist yet when this is built)."""

    def __init__(
        self,
        *,
        session_manager: Any,
        repo: Any,
        db: Any,
        bg_task_manager: Any = None,
        research_manager: Any = None,
        delegation_db: Any = None,
        pause_dispatch: Callable[[], None] | None = None,
        resume_dispatch: Callable[[], None] | None = None,
        bridge_manager_getter: Callable[[], Any] | None = None,
        scheduler_getter: Callable[[], Any] | None = None,
        parked_scheduler_getter: Callable[[], Any] | None = None,
        admission_gate: DeployAdmissionGate | None = None,
    ) -> None:
        self._sm = session_manager
        self._repo = repo
        self._db = db
        self._bg = bg_task_manager
        self._research = research_manager
        # Delegation runs are read from the DB (a delegation child is also a
        # Session, but the durable run row is the authoritative "still running").
        self._delegation_db = delegation_db if delegation_db is not None else db
        self._pause_dispatch = pause_dispatch
        self._resume_dispatch = resume_dispatch
        self._bridge_getter = bridge_manager_getter
        self._scheduler_getter = scheduler_getter
        self._parked_scheduler_getter = parked_scheduler_getter
        self._admission_gate = admission_gate

    async def close_admission(self) -> None:
        """Block new work before deploy's final idle census (§7.2)."""
        if self._admission_gate is not None:
            await self._admission_gate.close()

    async def open_admission(self) -> None:
        """Release new work after a pre-spawn deploy abort."""
        if self._admission_gate is not None:
            await self._admission_gate.open()

    # -- census -----------------------------------------------------------

    async def census(self, *, exclude_op_id: str | None = None) -> list[BusySource]:
        """Enumerate every busy source right now (§7.1). ``exclude_op_id`` is the
        switch op itself — it is `running`, but it is not "other work" that would
        be harmed, so it must not count itself as a reason to refuse."""
        busy: list[BusySource] = []

        # A session turn is active anywhere (its orchestrator loop is live). This
        # is the exact `_active_task` idiom the session manager uses everywhere.
        for sid, session in list(getattr(self._sm, "sessions", {}).items()):
            task = getattr(session, "_active_task", None)
            if task is not None and not task.done():
                busy.append(BusySource("session_turn", str(sid)))

        # Parked-resume pending: a turn persisted while awaiting a usage-limit
        # reset (§7.1 "including parked-resume pending"). It would wake mid-switch.
        for parked in await self._db.list_parked_turns():
            busy.append(BusySource("parked_turn", str(parked.get("session_id"))))

        # A task run is running/claimed on any board.
        for run in await self._repo.list_running_runs():
            busy.append(BusySource("task_run", str(run.id)))

        # Another delivery op is running (the per-delivery one-running index only
        # covers this delivery; the census covers every other delivery).
        for op in await self._repo.list_running_delivery_ops():
            if exclude_op_id is not None and op.id == exclude_op_id:
                continue
            busy.append(BusySource("delivery_op", f"{op.kind}:{op.id}"))

        # A bg task subprocess is live (authoritative within this process).
        running_bg = len(getattr(self._bg, "_running", {}) or {}) if self._bg else 0
        if running_bg:
            busy.append(BusySource("bg_task", f"{running_bg} running"))

        # A research job is running.
        running_research = (
            len(getattr(self._research, "_tasks", {}) or {}) if self._research else 0
        )
        if running_research:
            busy.append(BusySource("research", f"{running_research} running"))

        # A delegation child turn is running.
        for drun in await self._delegation_db.list_running_delegation_runs():
            busy.append(
                BusySource("delegation", str(drun.get("id") or drun.get("session_id")))
            )

        # Pending session injections would be consumed mid-restart (§7.1).
        for inj in await self._db.list_pending_session_injections():
            busy.append(
                BusySource("session_injection", str(inj.get("id") or inj.get("source_key")))
            )

        return busy

    # -- drain pause / resume --------------------------------------------
    #
    # `drain` pauses PRODUCERS only, never consumers of running work (§7.1). Each
    # pause is transient (in-memory / runtime), so a successful switch that
    # restarts the process leaves no durable "paused" state behind — the new
    # server boots with producers enabled. On a drain timeout the coordinator
    # calls `resume` to restore the exact set.

    async def pause_for_drain(self) -> None:
        # Stop injection-sourced turns (the reused boot-barrier primitive).
        self._sm.pause_session_injection_dispatch()
        # Stop the task dispatcher claiming new runs (transient, not persisted).
        if self._pause_dispatch is not None:
            self._pause_dispatch()
        # Stop bridge intake (tears down the inbound socket/worker).
        bridge = self._bridge_getter() if self._bridge_getter else None
        if bridge is not None:
            await bridge.stop_all()
        # Freeze scheduled + parked-turn wake-ups so neither fires a fresh turn
        # mid-drain and re-busies the census.
        for getter in (self._scheduler_getter, self._parked_scheduler_getter):
            sched = getter() if getter else None
            if sched is not None:
                sched.pause()

    async def resume_from_drain(self) -> None:
        for getter in (self._scheduler_getter, self._parked_scheduler_getter):
            sched = getter() if getter else None
            if sched is not None:
                sched.resume()
        bridge = self._bridge_getter() if self._bridge_getter else None
        if bridge is not None:
            await bridge.start_all()
        if self._resume_dispatch is not None:
            self._resume_dispatch()
        # Re-enable injection dispatch last; this also drains any rows that
        # queued while paused (normal resume semantics).
        await self._sm.resume_session_injection_dispatch()


CensusProvider = Callable[..., Awaitable[list[BusySource]]]
