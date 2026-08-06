"""Agent-to-agent delegation manager (agent-collaboration.md).

The shape mirrors `server/bg_tasks.py`: an asynchronous, fire-and-forget
operation on the parent agent's behalf, whose terminal result is delivered
back as a crash-safe `session_injections` user turn. That parent injection is
what gives the caller a fresh turn to react to the child's reply.

For agent collaboration, the public delegation is a normal child `Session`
with ``origin='delegation'`` and ``parent_session_id`` set. Its id remains the
continuation handle. Each initial/follow-up execution inside that session is an
append-only `delegation_runs` row with an internal `run_id`.

What this module owns:

  - A hot in-memory registry of the latest round, backed by durable rows.
  - The broadcast subscriber that watches the child's event stream and
    captures the events that matter for delivery (the same filter
    bridges use for quiet mode: assistant_text + result + error).
  - The cycle and depth guards that walk the parent chain.
  - The agent-name lookup (case-insensitive, ambiguity-rejecting).
  - The injection formatter: ``[agent-reply|agent-error:<name>
    delegation=<id>]`` plus a durable, deduplicated delivery intent.

What this module does NOT own:

  - The ``ask_agent`` MCP server (Phase 2). The server is a thin
    stdio shim that POSTs to the FastAPI routes which call into here.
  - The caller-aware ``ask`` server (Phase 3) — that's a one-line
    change in the existing question handler.
  - Frontend rendering (Phase 4).

Phase 1 deliberately limits itself to: a real child Session is created
under the target agent, the child runs its first turn, and the reply is
injected back into the parent. The parent agent has no tool with which
to invoke this yet — only the REST API does.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .harness import join_text_blocks
from .deploy_admission import DeployAdmissionClosedError
from .model_routing import ModelBackendError, validate_model_for_backend

if TYPE_CHECKING:
    from .database import Database
    from .session_manager import SessionManager

logger = logging.getLogger(__name__)


# Maximum agent-delegation hops allowed in a single caller chain
# (agent-collaboration.md §5.9). The user is hop 0; each delegation
# (origin='delegation' session) is one hop. Allows root → A → B → C
# (three delegated agents under the human) but rejects deeper. A small
# constant on purpose — agent fan-out is meant to be shallow.
DEPTH_CAP = 3


class DelegationError(Exception):
    """Surface-level error for the REST layer. Carries an HTTP status so
    the routes can translate uniformly (404 for resolution failures,
    409 for guard violations)."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class DelegationRunState:
    """Live record of one delegation. Lifetime: from
    ``start_delegation`` to ``state != "running"`` plus a retention
    window so ``list_agent_tasks`` can show recently-finished items.
    """

    # `delegation_id` remains the public child-session continuation handle;
    # `run_id` identifies this particular round inside that session.
    run_id: str
    delegation_id: str
    round_no: int
    start_seq: int
    parent_session_id: str | None
    target_agent_id: str
    target_agent_name: str
    request: str
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    state: str = "running"  # running|completed|failed|cancelled|interrupted
    captured_text: list[str] = field(default_factory=list)
    finished_at: str | None = None
    error: str | None = None
    # Defence-in-depth: terminal injection must fire at most once per
    # record. The state-flip-before-interrupt dance in
    # `cancel_delegation` already prevents the obvious race, but a
    # `result` and an `error` event can arrive close together from a
    # crashing child; this flag forces a single emission no matter
    # how the producers interleave.
    _terminal_injected: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        """API-shape for ``GET /sessions/{sid}/delegations`` and the
        ``ask_agent`` tool's return value. Hides ``captured_text``
        (that goes into the parent's transcript, not the JSON API)."""
        return {
            "run_id": self.run_id,
            "delegation_id": self.delegation_id,
            "sub_session_id": self.delegation_id,
            "round_no": self.round_no,
            "parent_session_id": self.parent_session_id,
            "target_agent_id": self.target_agent_id,
            "target_agent_name": self.target_agent_name,
            "request": self.request,
            "state": self.state,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


class DelegationManager:
    """Coordinator and hot cache for durable delegation rounds.

    Wired in by ``main.py`` lifespan after ``SessionManager.initialize``:
    we register a broadcast listener so the per-session WS events the
    SessionManager already produces become our event source. The database is
    authoritative across restarts; ``_records`` only avoids repeated reads for
    currently active rounds. No extra subprocess, no extra polling.
    """

    BROADCAST_KEY = "delegation_manager"

    def __init__(self) -> None:
        # Keyed by delegation_id (== child session id). Records stay
        # around after their terminal state so list_agent_tasks can
        # render recently-finished entries; a future LRU/retention
        # policy can trim this, but at single-user scale a flat dict
        # is fine.
        self._records: dict[str, DelegationRunState] = {}
        self.session_mgr: "SessionManager | None" = None
        self.db: "Database | None" = None

    # ------------------------------------------------------------ wiring

    def bind(self, session_mgr: "SessionManager", db: "Database") -> None:
        """Subscribe to the session manager's broadcast bus. Idempotent
        on repeat calls (last writer wins on the key)."""
        self.session_mgr = session_mgr
        self.db = db
        session_mgr.on_broadcast(self.BROADCAST_KEY, self._on_broadcast)

    def shutdown(self) -> None:
        if self.session_mgr is not None:
            self.session_mgr.remove_broadcast(self.BROADCAST_KEY)

    @staticmethod
    def _record_from_row(row: dict[str, Any]) -> DelegationRunState:
        return DelegationRunState(
            run_id=row["run_id"],
            delegation_id=row["delegation_id"],
            round_no=int(row["round_no"]),
            start_seq=int(row["start_seq"]),
            parent_session_id=row["parent_session_id"],
            target_agent_id=row["target_agent_id"],
            target_agent_name=row["target_agent_name"],
            request=row["request"],
            created_at=row["created_at"],
            state=row["state"],
            finished_at=row.get("finished_at"),
            error=row.get("error"),
        )

    async def _load_latest_record(
        self, delegation_id: str, *, refresh: bool = False
    ) -> DelegationRunState | None:
        rec = None if refresh else self._records.get(delegation_id)
        if rec is not None:
            return rec
        if self.db is None:
            return None
        row = await self.db.get_latest_delegation_run(delegation_id)
        if row is None:
            return None
        rec = self._record_from_row(row)
        self._records[delegation_id] = rec
        return rec

    async def recover_interrupted(self) -> int:
        """Make restart-orphaned delegation rounds truthful and notify callers.

        No delegated model process survives the Owlery process, but external
        side effects may already have happened.  Therefore a running round
        becomes `interrupted` (outcome unknown), never `failed` and never an
        automatic retry.

        Recovery is deliberately phased while session-injection dispatch is
        paused by main.py:

        1. terminalise every running/legacy round without archiving anything;
        2. create every missing terminal outbox source, deepest-first;
        3. materialise pending events aimed at delegation parents directly into
           their transcripts without reviving a model turn;
        4. only then archive the recovered delegation sessions.

        Keeping archive as the final phase prevents Octo→Vera→Pete recovery
        from deleting Vera before Pete's terminal event is durably recorded.
        """
        if self.session_mgr is None or self.db is None:
            return 0
        if not self.session_mgr.session_injection_dispatch_paused:
            raise RuntimeError(
                "delegation restart recovery requires paused injection dispatch"
            )
        now = datetime.now(timezone.utc).isoformat()
        recovered = 0
        live_delegations = {
            session.id: session
            for session in self.session_mgr.sessions.values()
            if session.origin == "delegation"
        }

        # Phase 1a: terminalise all durable running rounds. Do not inject or
        # archive in this loop; every parent in the restart tree must remain
        # available until descendant delivery intents are materialised.
        for row in await self.db.list_running_delegation_runs():
            changed = await self.db.finish_delegation_run(
                row["run_id"],
                state="interrupted",
                error="server restarted; work may have partially completed",
                finished_at=now,
            )
            if not changed:
                continue
            row["state"] = "interrupted"
            row["error"] = "server restarted; work may have partially completed"
            row["finished_at"] = now
            rec = self._record_from_row(row)
            self._records[rec.delegation_id] = rec
            await self._cancel_park_for_recovery(rec.delegation_id)
            recovered += 1

        # Phase 1b: compatibility rescue for an upgrade while an old-version
        # delegation was live. Historical archived sessions remain untouched;
        # only live sessions with no ledger receive an interrupted round.
        for session in live_delegations.values():
            if await self.db.delegation_session_has_runs(session.id):
                continue
            run_id = uuid.uuid4().hex[:12]
            request = session.delegation_request or "(legacy delegation request unavailable)"
            await self.db.create_delegation_run(
                run_id=run_id,
                delegation_id=session.id,
                round_no=1,
                request=request,
                start_seq=-1,
                created_at=session.created_at,
                state="interrupted",
                error="server restarted; legacy work may have partially completed",
                finished_at=now,
            )
            row = await self.db.get_latest_delegation_run(session.id)
            if row is None:
                continue
            rec = self._record_from_row(row)
            self._records[rec.delegation_id] = rec
            await self._cancel_park_for_recovery(rec.delegation_id)
            recovered += 1

        def session_depth(session_id: str) -> int:
            """Depth inside the live delegation-session tree."""
            value = 0
            session = live_delegations.get(session_id)
            parent_id = session.parent_session_id if session else None
            seen: set[str] = set()
            while parent_id in live_delegations and parent_id not in seen:
                seen.add(parent_id)
                value += 1
                parent_id = live_delegations[parent_id].parent_session_id
            return value

        # Phase 2: completion and delivery intent are separate facts. Every
        # post-ledger terminal round with a live parent must have one source.
        # Deepest-first is both easier to audit and preserves child-before-
        # ancestor transcript order; archive still waits for phase 4.
        missing = await self.db.list_terminal_delegation_runs_missing_delivery()
        missing.sort(
            key=lambda row: (
                session_depth(row["delegation_id"]),
                row.get("finished_at") or "",
                row["run_id"],
            ),
            reverse=True,
        )
        for row in missing:
            rec = self._record_from_row(row)
            latest = self._records.get(rec.delegation_id)
            if latest is None or rec.round_no >= latest.round_no:
                self._records[rec.delegation_id] = rec
            await self._inject_terminal(rec, archive=False)

        # Phase 3: these parents are themselves interrupted and will be
        # archived. Preserve child/bg/research events in their transcript but
        # never restart a model turn whose outcome we have declared unknown.
        materialized = (
            await self.session_mgr.materialize_pending_injections_for_sessions(
                set(live_delegations)
            )
        )

        # Phase 4: all descendant delivery receipts now exist below the model
        # layer, so the whole recovered tree can be archived safely.
        for session_id in sorted(
            live_delegations,
            key=session_depth,
            reverse=True,
        ):
            await self.session_mgr.auto_archive_scheduled_session(session_id)

        if recovered:
            logger.info("delegation recovery: interrupted %d round(s)", recovered)
        if missing:
            logger.info(
                "delegation recovery: repaired %d terminal delivery intent(s)",
                len(missing),
            )
        if materialized:
            logger.info(
                "delegation recovery: materialized %d nested transcript event(s)",
                materialized,
            )
        return recovered

    async def _cancel_park_for_recovery(self, session_id: str) -> None:
        if self.session_mgr is None or self.session_mgr._parked_turns is None:
            return
        try:
            await self.session_mgr._parked_turns.cancel(session_id)
        except Exception:
            logger.exception(
                "delegation recovery: failed to cancel parked child %s",
                session_id,
            )

    # --------------------------------------------------------- public API

    async def start_delegation(
        self,
        *,
        parent_session_id: str,
        agent_name: str,
        request: str,
        files: list[str] | None = None,
        model: str | None = None,
    ) -> DelegationRunState:
        """Create a child session under the named target agent and kick
        off its first turn. Returns immediately with the record — the
        reply arrives later via injection into the parent session
        (agent-collaboration.md §5.1)."""
        if self.session_mgr is None or self.db is None:
            raise DelegationError(
                "DelegationManager not bound", status_code=500
            )
        if not request or not request.strip():
            raise DelegationError(
                "request must be a non-empty string", status_code=400
            )

        parent = self.session_mgr.get_session(parent_session_id)
        if parent is None:
            raise DelegationError(
                f"Parent session {parent_session_id!r} not found",
                status_code=404,
            )

        target = await self._resolve_target_agent(agent_name)
        if target is None:
            raise DelegationError(
                f"No agent named {agent_name!r}", status_code=404
            )
        if parent.agent_id and target["id"] == parent.agent_id:
            raise DelegationError(
                "Cannot delegate to yourself — pick a different agent",
                status_code=409,
            )

        await self._check_chain(parent, target_agent_id=target["id"])

        # Parent name is informational only (used in the child's first
        # message and in the injection prefix). A missing parent agent
        # falls back to a generic placeholder rather than 500ing.
        parent_agent = (
            await self.db.get_agent(parent.agent_id) if parent.agent_id else None
        )
        parent_name = (parent_agent or {}).get("name") or "another agent"

        child_backend = (target.get("backend") or "claude-code")
        # Cross-family guard: reject a model the child's backend can't run
        # (budget-model-routing.md §4.3) BEFORE spawning the child session.
        try:
            validate_model_for_backend(child_backend, model)
        except ModelBackendError as exc:
            raise DelegationError(str(exc), status_code=422) from exc

        async def create_and_start() -> DelegationRunState:
            child = await self.session_mgr.create_session(
                agent_id=target["id"],
                name=f"{target['name']} ← {parent_name}",
                working_dir=parent.working_dir,
                origin="delegation",
                backend=child_backend,
                parent_session_id=parent.id,
                delegation_request=request,
                model=model,
            )
            run_id = uuid.uuid4().hex[:12]
            start_seq = await self.db.max_message_seq(child.id)
            created_at = datetime.now(timezone.utc).isoformat()
            await self.db.create_delegation_run(
                run_id=run_id,
                delegation_id=child.id,
                round_no=1,
                request=request,
                start_seq=start_seq,
                created_at=created_at,
            )
            rec = DelegationRunState(
                run_id=run_id,
                delegation_id=child.id,
                round_no=1,
                start_seq=start_seq,
                parent_session_id=parent.id,
                target_agent_id=target["id"],
                target_agent_name=target["name"],
                request=request,
                created_at=created_at,
            )
            # This record is the delegation portion of the final census.  It
            # is registered before the admitted initial turn can broadcast.
            self._records[child.id] = rec
            composed = self._compose_initial_prompt(
                parent_name=parent_name,
                parent_session_id=parent.id,
                request=request,
                files=files or [],
                working_dir=parent.working_dir,
            )
            try:
                await self.session_mgr.start_message(child.id, composed)
            except Exception as exc:
                logger.exception("Failed to start child session %s for delegation", child.id)
                rec.state = "failed"
                rec.error = f"failed to start child session: {exc}"
                rec.finished_at = datetime.now(timezone.utc).isoformat()
                await self.db.finish_delegation_run(
                    rec.run_id,
                    state=rec.state,
                    error=rec.error,
                    finished_at=rec.finished_at,
                )
                await self._inject_terminal(rec)
                raise DelegationError(
                    f"failed to start delegation: {exc}", status_code=500
                ) from exc
            return rec

        try:
            async with self.session_mgr.deploy_admission_gate.admit():
                return await create_and_start()
        except DeployAdmissionClosedError as exc:
            raise DelegationError("deploy admission is closed", status_code=409) from exc

    async def cancel_delegation(
        self, delegation_id: str, *, reason: str | None = None
    ) -> DelegationRunState:
        """Stop a running delegation. Idempotent — cancelling a finished
        delegation is a no-op that returns the existing record.

        Also CASCADE-CANCELS any running descendants whose
        ``parent_session_id`` chain leads to this delegation. Without
        the cascade, an Octo-cancels-Vera while Vera-asked-Pete
        scenario would leak Pete: Pete keeps burning tokens and his
        eventual reply lands on a missing/cancelled parent and is
        silently dropped. Cascade unwinds the chain top-down so the
        terminal injections to each parent stay meaningful.
        """
        if self.session_mgr is None or self.db is None:
            raise DelegationError(
                "DelegationManager not bound", status_code=500
            )
        rec = await self._load_latest_record(delegation_id)
        if rec is None:
            raise DelegationError(
                f"No delegation {delegation_id!r}", status_code=404
            )
        if rec.state != "running":
            return rec

        # CRITICAL: transition the record state BEFORE calling
        # `interrupt()`. The interrupt broadcasts an `error` event
        # that our own `_on_broadcast` would otherwise turn into a
        # spurious `[agent-error reason=child error]` injection,
        # which would race with the `[agent-error reason=cancelled]`
        # we inject below — the parent would see two terminal turns
        # for one cancellation. The `_on_broadcast` running-check
        # guards against that as long as we flip state first.
        rec.state = "cancelled"
        rec.error = reason or "cancelled by caller"
        rec.finished_at = datetime.now(timezone.utc).isoformat()
        changed = await self.db.finish_delegation_run(
            rec.run_id,
            state=rec.state,
            error=rec.error,
            finished_at=rec.finished_at,
        )
        if not changed:
            # Another terminal transition won the compare-and-set. Refresh
            # from durable truth; returning the locally-mutated cancelled
            # object would lie about the actual outcome.
            latest = await self._load_latest_record(delegation_id, refresh=True)
            return latest or rec
        try:
            await self.session_mgr.interrupt(delegation_id)
        except Exception:
            logger.exception(
                "interrupt(%s) raised during cancellation", delegation_id
            )
        # Cascade BEFORE injecting our own terminal turn into the
        # parent: child terminal injections target our own session
        # id (which is still alive — auto-archive runs after the
        # parent inject), so cancelling them now keeps the chain's
        # invariant that a parent always learns its child's outcome.
        await self._cascade_cancel_descendants(
            delegation_id, root_reason=rec.error or "cancelled by caller"
        )
        await self._inject_terminal(rec)
        return rec

    async def follow_up_delegation(
        self,
        *,
        parent_session_id: str,
        delegation_id: str,
        request: str,
    ) -> DelegationRunState:
        """Continue a prior delegation with a new request in the SAME
        child session — so the target agent keeps her in-session
        transcript and can build on what she did last round.

        Use this for review/iteration loops where context continuity
        matters (Octo asks Vera "review again — I addressed your
        finding 3"). Use plain `start_delegation` (the `ask` tool)
        for fresh work or parallel fan-out to the same target —
        sharing a session would serialise concurrent work and mix
        unrelated contexts.

        Validation:
        - delegation must exist and belong to this parent
        - delegation must be in a terminal state (not ``running``);
          there's no sound semantic for "follow up while the previous
          round is still working" — the parent should wait for the
          terminal turn first
        - the child session must exist (live OR archived); if
          archived, we unarchive it. A hard-deleted child can't be
          followed up — start a fresh delegation instead

        Each follow-up appends a new run row. The public delegation/session id
        and agent identity stay stable; run_id, round_no, request, timestamps,
        and terminal state belong to that one round and never overwrite prior
        audit history.
        """
        if self.session_mgr is None or self.db is None:
            raise DelegationError(
                "DelegationManager not bound", status_code=500
            )
        if not request or not request.strip():
            raise DelegationError(
                "request must be a non-empty string", status_code=400
            )
        prior = await self._load_latest_record(delegation_id)
        if prior is None or prior.parent_session_id != parent_session_id:
            raise DelegationError(
                f"No delegation {delegation_id!r} owned by this session",
                status_code=404,
            )
        if prior.state == "running":
            raise DelegationError(
                f"Delegation {delegation_id!r} is still running; "
                f"wait for its reply before following up",
                status_code=409,
            )

        async def create_and_start() -> DelegationRunState:
            child = self.session_mgr.get_session(prior.delegation_id)
            if child is None:
                # An archived child is resumed as part of the same admission
                # as its new durable round; a closed deploy must not revive it.
                try:
                    await self.session_mgr.unarchive_session(prior.delegation_id)
                except ValueError as exc:
                    raise DelegationError(
                        f"Child session {prior.delegation_id!r} is gone and "
                        "can't be reused; start a fresh delegation",
                        status_code=409,
                    ) from exc
            start_seq = await self.db.max_message_seq(prior.delegation_id)
            created_at = datetime.now(timezone.utc).isoformat()
            run_id = uuid.uuid4().hex[:12]
            await self.db.create_delegation_run(
                run_id=run_id,
                delegation_id=prior.delegation_id,
                round_no=prior.round_no + 1,
                request=request,
                start_seq=start_seq,
                created_at=created_at,
            )
            rec = DelegationRunState(
                run_id=run_id,
                delegation_id=prior.delegation_id,
                round_no=prior.round_no + 1,
                start_seq=start_seq,
                parent_session_id=prior.parent_session_id,
                target_agent_id=prior.target_agent_id,
                target_agent_name=prior.target_agent_name,
                request=request,
                created_at=created_at,
            )
            self._records[rec.delegation_id] = rec
            parent_live = self.session_mgr.get_session(parent_session_id)
            parent_agent = (
                await self.db.get_agent(parent_live.agent_id)
                if parent_live and parent_live.agent_id
                else None
            )
            parent_name = (parent_agent or {}).get("name") or "another agent"
            composed = (
                f"Agent **{parent_name}** has a follow-up for you in the "
                f"same line of work — your previous reply is above in "
                f"this transcript. Their new request follows.\n\n"
                f"---\n{request.strip()}"
            )
            try:
                await self.session_mgr.start_message(rec.delegation_id, composed)
            except Exception as exc:
                logger.exception("Failed to start follow-up turn on child %s", rec.delegation_id)
                rec.state = "failed"
                rec.error = f"failed to start follow-up: {exc}"
                rec.finished_at = datetime.now(timezone.utc).isoformat()
                await self.db.finish_delegation_run(
                    rec.run_id,
                    state=rec.state,
                    error=rec.error,
                    finished_at=rec.finished_at,
                )
                await self._inject_terminal(rec)
                raise DelegationError(
                    f"failed to start follow-up: {exc}", status_code=500
                ) from exc
            return rec

        try:
            async with self.session_mgr.deploy_admission_gate.admit():
                return await create_and_start()
        except DeployAdmissionClosedError as exc:
            raise DelegationError("deploy admission is closed", status_code=409) from exc

    async def _cascade_cancel_descendants(
        self, parent_delegation_id: str, *, root_reason: str
    ) -> None:
        """Cancel every running delegation that descends from this one
        via ``parent_session_id``. Walks breadth-first; each
        descendant is cancelled with a reason that names the
        originating root cancel so the parent's `[agent-error]`
        injection is informative."""
        # Snapshot the running records up front; we'll mutate state
        # as we walk.
        running = [
            r
            for r in self._records.values()
            if r.state == "running"
            and r.delegation_id != parent_delegation_id
        ]
        # Build adjacency: parent_session_id → child delegation_ids.
        children_of: dict[str, list[DelegationRunState]] = {}
        for r in running:
            children_of.setdefault(r.parent_session_id, []).append(r)
        # BFS from the cancelled delegation downward.
        frontier: list[str] = [parent_delegation_id]
        while frontier:
            next_frontier: list[str] = []
            for sid in frontier:
                for child_rec in children_of.get(sid, []):
                    if child_rec.state != "running":
                        continue
                    cancel_reason = (
                        f"parent delegation cancelled "
                        f"({root_reason})"
                    )
                    try:
                        # Recurse via the public cancel path so each
                        # descendant gets the same state-flip-first,
                        # interrupt, terminal-inject treatment — and
                        # in turn cascades to its own children.
                        await self.cancel_delegation(
                            child_rec.delegation_id,
                            reason=cancel_reason,
                        )
                    except Exception:
                        logger.exception(
                            "cascade-cancel of %s failed",
                            child_rec.delegation_id,
                        )
                    next_frontier.append(child_rec.delegation_id)
            frontier = next_frontier

    async def list_delegations(
        self, parent_session_id: str, *, limit: int = 25
    ) -> list[DelegationRunState]:
        """Latest durable round for each child delegation, newest first."""
        if self.db is None:
            return []
        rows = await self.db.list_latest_delegation_runs_for_parent(
            parent_session_id, limit=limit
        )
        result: list[DelegationRunState] = []
        for row in rows:
            live = self._records.get(row["delegation_id"])
            if live is not None and live.run_id == row["run_id"]:
                result.append(live)
            else:
                result.append(self._record_from_row(row))
        return result

    def get_delegation(self, delegation_id: str) -> DelegationRunState | None:
        return self._records.get(delegation_id)

    async def get_delegation_record(
        self, delegation_id: str
    ) -> DelegationRunState | None:
        return await self._load_latest_record(delegation_id)

    async def list_delegation_rounds(
        self, *, parent_session_id: str, delegation_id: str
    ) -> list[DelegationRunState]:
        """Complete append-only execution history for one child session."""
        if self.db is None:
            return []
        latest = await self.db.get_latest_delegation_run(delegation_id)
        if latest is None or latest["parent_session_id"] != parent_session_id:
            raise DelegationError("Delegation not found", status_code=404)
        return [
            self._record_from_row(row)
            for row in await self.db.list_delegation_runs(delegation_id)
        ]

    def has_active_delegation_for_parent(self, parent_session_id: str) -> bool:
        """True if `parent_session_id` has any still-running delegation
        (session-rewind.md §5.4). The fork live-work check uses this to
        refuse forking a parent that's mid-delegation — a fork is only allowed
        against a quiescent parent."""
        return any(
            r.parent_session_id == parent_session_id and r.state == "running"
            for r in self._records.values()
        )

    # ----------------------------------------------------------- internals

    async def _resolve_target_agent(
        self, name: str
    ) -> dict[str, Any] | None:
        """Case-insensitive name lookup over non-archived agents.

        Multiple matches → DelegationError(409). One match → the row.
        Zero matches → None (caller turns this into a 404)."""
        assert self.db is not None  # bound at lifespan
        wanted = (name or "").strip().lower()
        if not wanted:
            return None
        agents = await self.db.load_agents()
        matches = [a for a in agents if (a.get("name") or "").lower() == wanted]
        if not matches:
            return None
        if len(matches) > 1:
            names = ", ".join(repr(a.get("name")) for a in matches)
            raise DelegationError(
                f"Ambiguous agent name {name!r} (matches: {names})",
                status_code=409,
            )
        return matches[0]

    async def _check_chain(self, parent, *, target_agent_id: str) -> None:
        """Walk the parent chain upward from the given parent session.

        Enforces two rules (agent-collaboration.md §5.9):
        - Cycle: the target agent must not already appear in the chain.
        - Depth: the new delegation (counted as +1) plus all existing
          ``origin='delegation'`` hops in the chain must not exceed
          ``DEPTH_CAP``.

        Self-delegation (target == parent.agent_id) is rejected by the
        caller; this method still catches it transitively via the cycle
        check, but the dedicated error message at the call site is
        clearer for the user.

        Fail-closed semantics: a corrupted parent_session_id chain
        (loop in the session-id pointers, or a chain longer than the
        safety cap, or a non-null ``parent_session_id`` whose target
        the DB also can't find) is rejected as a 409 rather than
        silently treated as a valid (short) chain. The cycle guard is
        what stands between "Vera asks Octo" and an infinite
        delegation tower — it must never be skipped.

        Archived ancestors are allowed: when ``session_mgr.get_session``
        returns None for a non-null ancestor id, we fall back to the
        DB (``include_archived=True``) so that a legitimately archived
        delegation parent still contributes its agent_id / origin to
        the walk. This matters after the round-2 fix that archives
        delegation children once their terminal turn is delivered:
        without the DB fallback, an unarchived child whose parent
        remains archived would over-reject every subsequent
        ``ask_agent``.
        """
        assert self.session_mgr is not None
        chain_agent_ids: set[str] = set()
        visited_session_ids: set[str] = set()
        existing_hops = 0
        # Lazy DB cache: load all rows (live + archived) at most once
        # per call, keyed by id. Most chains are short; on a fan-out
        # this saves O(n) round-trips.
        archived_rows_by_id: dict[str, dict[str, Any]] | None = None

        async def _load_archived_index() -> dict[str, dict[str, Any]]:
            assert self.db is not None
            rows = await self.db.load_sessions(include_archived=True)
            return {r["id"]: r for r in rows}

        _SAFETY_CAP = 64  # see for/else clause below
        sid: str | None = parent.id
        for _ in range(_SAFETY_CAP):
            if sid is None:
                break
            if sid in visited_session_ids:
                # Real session-id cycle (e.g. A.parent=B, B.parent=A)
                # — not a transitive agent cycle, but a corrupted
                # pointer chain. Either way this is unsafe; reject.
                raise DelegationError(
                    "Caller chain has a session-id cycle "
                    "(corrupted parent_session_id pointers)",
                    status_code=409,
                )
            visited_session_ids.add(sid)
            session = self.session_mgr.get_session(sid)
            if session is not None:
                agent_id = session.agent_id
                origin = session.origin
                next_sid: str | None = session.parent_session_id
            else:
                # Memory miss — consult the DB (live + archived) for
                # the row before failing closed. An archived
                # delegation parent is legitimate state after the
                # auto-archive fix.
                if archived_rows_by_id is None:
                    archived_rows_by_id = await _load_archived_index()
                row = archived_rows_by_id.get(sid)
                if row is None:
                    raise DelegationError(
                        "Caller chain references a session that "
                        "exists in neither memory nor the database; "
                        "refuse rather than guess",
                        status_code=409,
                    )
                agent_id = row.get("agent_id")
                origin = row.get("origin") or "user"
                next_sid = row.get("parent_session_id")
            if agent_id:
                chain_agent_ids.add(agent_id)
            if origin == "delegation":
                existing_hops += 1
            sid = next_sid
        else:
            # for/else: loop exhausted the safety cap without
            # encountering a None terminator. That's a chain longer
            # than _SAFETY_CAP, which is unreasonable in practice —
            # treat as corruption and fail closed.
            raise DelegationError(
                f"Caller chain exceeds {_SAFETY_CAP} hops; refuse "
                f"as a fail-closed guard against pointer corruption",
                status_code=409,
            )

        if target_agent_id in chain_agent_ids:
            raise DelegationError(
                "Cycle rejected: target agent already in the caller chain",
                status_code=409,
            )
        # +1 because the new delegation we're about to create is the
        # next hop. The cap is "no more than N delegation hops in any
        # one caller chain" (root user is not counted; only delegations).
        if existing_hops + 1 > DEPTH_CAP:
            raise DelegationError(
                f"Delegation depth would exceed {DEPTH_CAP} hops",
                status_code=409,
            )

    def _compose_initial_prompt(
        self,
        *,
        parent_name: str,
        parent_session_id: str,
        request: str,
        files: list[str],
        working_dir: str,
    ) -> str:
        """The child's first user message. Names the caller, ships the
        request verbatim, and optionally lists files the parent flagged
        as relevant.

        File paths are resolved against ``working_dir`` (plan §5.7):
        absolute paths pass through unchanged; relative ones get joined
        to the working dir. We also check existence and clearly flag
        missing entries — better the child sees ``(not found)`` than a
        misleading absolute path that doesn't exist on disk.

        We do NOT include any of the parent's transcript — that's a
        deliberate scope/privacy boundary (plan §2).

        IMPORTANT — single-turn delivery contract: the child's entire
        response (all assistant_text blocks) is concatenated and injected
        into the parent's session as ONE turn when this turn ends.  There
        is no mechanism for subsequent turns on the child session to reach
        the parent.  The delegation protocol note below tells the child
        agent about this so it completes all work in this turn rather than
        sending a preliminary "I'll start" reply and deferring to a second
        turn that will never be delivered."""
        from pathlib import Path

        lines = [
            f"You were asked by agent **{parent_name}** "
            f"(session `{parent_session_id}`) to handle a task on their behalf.",
            "",
            "**Delegation protocol — read before responding:**",
            "- This is a **single-turn delegation**. When your turn ends, "
            "everything you have written is delivered to the caller as one "
            "reply. There is no follow-up turn.",
            "- **Do the work now, in this turn.** Do not send a preliminary "
            '"I\'ll start reviewing…" message and stop — that preamble would '
            "become the entire reply the caller receives.",
            "- Use your tools (read files, run commands, etc.) to complete the "
            "task fully before writing your response. Do **not** use "
            "`mcp__bg__run` for the core work — background tasks run after your "
            "turn ends and their results will not reach the caller.",
            "- End with a clear, self-contained summary of your findings or "
            "output so the caller can act on it without needing to ask again.",
            "",
            f"The request from **{parent_name}** follows.",
            "",
            "---",
            request.strip(),
        ]
        if files:
            lines.append("")
            lines.append(
                "The caller flagged these files as relevant "
                f"(paths resolved against `{working_dir}`):"
            )
            base = Path(working_dir)
            for raw in files:
                resolved = (
                    Path(raw) if Path(raw).is_absolute() else base / raw
                )
                try:
                    resolved = resolved.resolve()
                except OSError:
                    # Path resolution failed (e.g. permission error
                    # walking a symlink); surface the raw form so the
                    # child still sees what the parent meant.
                    lines.append(f"- {raw}  (could not resolve)")
                    continue
                if resolved.exists():
                    lines.append(f"- {resolved}")
                else:
                    lines.append(f"- {resolved}  (not found)")
        return "\n".join(lines)

    # ----------------------------------------------- broadcast → injection

    async def _on_broadcast(self, msg: dict[str, Any]) -> None:
        """Filter the session-manager broadcast bus to delegation
        children we're tracking. Mirrors the bridge quiet-mode filter:
        capture assistant_text, finalise on result, route error through
        the failure injection path, and route `question_request` up to
        the parent so the parent's model gets a turn to answer it
        (agent-collaboration.md §5.4 — the caller chain rule)."""
        sid = msg.get("session_id")
        if not sid:
            return
        rec = self._records.get(sid)
        if rec is None or rec.state != "running":
            return

        kind = msg.get("type")
        if kind == "assistant_text":
            text = msg.get("content")
            if isinstance(text, str) and text:
                rec.captured_text.append(text)
            return
        if kind == "question_request":
            await self._inject_question(rec, msg)
            return
        if kind == "result":
            if msg.get("is_error"):
                rec.state = "failed"
                rec.error = "child session reported an error result"
            else:
                rec.state = "completed"
            rec.finished_at = datetime.now(timezone.utc).isoformat()
            assert self.db is not None
            changed = await self.db.finish_delegation_run(
                rec.run_id,
                state=rec.state,
                error=rec.error,
                finished_at=rec.finished_at,
            )
            if not changed:
                return
            await self._inject_terminal(rec)
            return
        if kind == "error":
            # A parked child is PENDING, not failed (limit-auto-resume.md §4).
            # The usage-limit markers ride the `error` event shape, but the turn
            # is coming back by itself when the window resets — finalising here
            # would inject a bogus `[agent-error]` into the parent AND leave the
            # resumed child answering a delegation nobody is waiting on any more.
            # Stay `running` and keep waiting; the resumed turn ends the
            # delegation for real, via `result` or a genuine error.
            if msg.get("code") == "limit_paused":
                return
            rec.state = "failed"
            rec.error = str(msg.get("message") or "child session error")
            rec.finished_at = datetime.now(timezone.utc).isoformat()
            assert self.db is not None
            changed = await self.db.finish_delegation_run(
                rec.run_id,
                state=rec.state,
                error=rec.error,
                finished_at=rec.finished_at,
            )
            if not changed:
                return
            await self._inject_terminal(rec)
            return

    async def _inject_question(
        self, rec: DelegationRunState, msg: dict[str, Any]
    ) -> None:
        """Bubble a child's `question_request` up to the parent session
        as an injected `[agent-question:…]` turn. The pending question
        itself stays on the child's session (the existing UI path can
        still answer it manually); the parent's
        `answer_agent_question(delegation_id, choice)` tool drains that
        same queue on success — first to drain wins.
        """
        assert self.session_mgr is not None
        question_id = msg.get("question_id") or ""
        questions = msg.get("questions") or []
        body = self._render_question_body(questions)
        prompt = (
            f"[agent-question:{rec.target_agent_name} "
            f"delegation={rec.delegation_id} "
            f"question_id={question_id}]\n{body}\n\n"
            f"Decide: answer them by calling "
            f"`mcp__ask_agent__answer(delegation_id="
            f"\"{rec.delegation_id}\", choice=…)`; or, if you don't "
            f"know, use your own `mcp__ask__user` to ask the user "
            f"and forward their answer; or cancel via "
            f"`mcp__ask_agent__cancel`."
        )
        try:
            await self.session_mgr.enqueue_session_injection(
                source_key=(
                    f"delegation:{rec.run_id}:question:{question_id or 'unknown'}"
                ),
                session_id=rec.parent_session_id,
                prompt=prompt,
            )
        except Exception:
            logger.exception(
                "Failed to inject delegation %s question into parent %s",
                rec.delegation_id,
                rec.parent_session_id,
            )

    @staticmethod
    def _render_question_body(questions: list[dict[str, Any]]) -> str:
        """Render the AskUserQuestion payload into a human-readable
        block the parent's model can reason over. We render every
        question in the batch, but answering currently applies the
        parent's `choice` to the FIRST question only — see
        `answer_pending_question`."""
        if not questions:
            return "(no question text — child sent an empty payload)"
        lines: list[str] = []
        for i, q in enumerate(questions, start=1):
            qtext = q.get("question") or "(no question text)"
            header = q.get("header")
            multi = bool(q.get("multiSelect"))
            options = q.get("options") or []
            lines.append(f"Question {i}: {qtext}")
            if header:
                lines.append(f"  (header: {header})")
            for opt in options:
                label = (opt or {}).get("label") or "?"
                desc = (opt or {}).get("description")
                if desc:
                    lines.append(f"  - {label}: {desc}")
                else:
                    lines.append(f"  - {label}")
            mode = "multi-select" if multi else "single-choice"
            lines.append(f"  ({mode}; pass the chosen label as `choice`.)")
        return "\n".join(lines)

    async def answer_pending_question(
        self, delegation_id: str, choice: str
    ) -> dict[str, Any]:
        """Drain the oldest pending question on the delegation's child
        session by feeding it the parent's chosen label.

        Multiple-question batches are rare (the ask MCP tool accepts
        1-4 questions per call; the common shape is 1). When there
        are >1 we apply `choice` to the first question and leave the
        rest with empty `selected` — same defaulting the human UI
        does when the user skips an option."""
        if self.session_mgr is None:
            raise DelegationError(
                "DelegationManager not bound", status_code=500
            )
        rec = self._records.get(delegation_id)
        if rec is None:
            raise DelegationError(
                f"No delegation {delegation_id!r}", status_code=404
            )
        if rec.state != "running":
            raise DelegationError(
                f"Delegation {delegation_id!r} is {rec.state} — no "
                f"pending question to answer",
                status_code=409,
            )
        child = self.session_mgr.get_session(rec.delegation_id)
        if child is None:
            raise DelegationError(
                "Child session no longer alive", status_code=404
            )
        if not child._pending_questions:
            raise DelegationError(
                "No pending question on the child session",
                status_code=409,
            )
        question_id, pending = next(iter(child._pending_questions.items()))
        choice = (choice or "").strip()
        if not choice:
            raise DelegationError(
                "`choice` must be a non-empty string", status_code=400
            )
        answers = [{"selected": [choice], "text": None}]
        # Pad multi-question batches with empty selections; same shape
        # the frontend submits when a user clicks-through without
        # answering some entries.
        for _ in pending.questions[1:]:
            answers.append({"selected": [], "text": None})
        ok = await self.session_mgr.answer_question(
            rec.delegation_id, question_id, answers
        )
        if not ok:
            # Race: the human UI answered between our get and our set.
            raise DelegationError(
                "Question already answered by another path",
                status_code=409,
            )
        return {
            "delegation_id": delegation_id,
            "question_id": question_id,
            "choice": choice,
            "ok": True,
        }

    async def _inject_terminal(
        self, rec: DelegationRunState, *, archive: bool = True
    ) -> dict[str, Any] | None:
        """Persist and schedule the terminal turn into the parent session.

        The marker prefix is structured text so the
        parent's model can disambiguate when multiple delegations are
        live concurrently, and so the frontend can detect and render
        it as a special card once Phase 4 lands.

        Idempotent. The unique outbox source key is authoritative; the
        ``_terminal_injected`` flag additionally ensures that
        even if two terminal-producing events race (a `result` from
        the child + a `cancel_delegation` from the parent, or a
        `result` and an `error` from a crashing child), exactly one
        ``[agent-…]`` turn lands in the parent session.
        """
        assert self.session_mgr is not None
        if rec._terminal_injected:
            return None
        assert self.db is not None
        injection: dict[str, Any] | None = None
        text_blocks = await self.db.load_delegation_output(
            rec.delegation_id, after_seq=rec.start_seq
        )
        # Real SessionManager ordering guarantees the DB rows exist before the
        # broadcast reaches us.  Lightweight unit fakes sometimes call the
        # broadcast subscriber directly; retain the live cache as a harmless
        # fallback without making it the recovery source of truth.
        if not text_blocks and rec.captured_text:
            text_blocks = list(rec.captured_text)
        if rec.state == "completed":
            # Each captured entry is one COMPLETE assistant text block, and
            # blocks carry no trailing newline — `"".join` would fuse them
            # into one multi-thousand-character line, which the parent's
            # card then renders as a wall of pre-wrap text. See
            # `join_text_blocks`, which both this and the research leaf use.
            body = join_text_blocks(text_blocks)
            if not body:
                body = "(child session ended without producing any text)"
            prompt = (
                f"[agent-reply:{rec.target_agent_name} "
                f"delegation={rec.delegation_id}]\n{body}"
            )
        else:
            reason = rec.error or rec.state
            partial = join_text_blocks(text_blocks)
            partial_note = (
                "\n\nOutput captured before interruption/failure:\n" + partial
                if partial else ""
            )
            prompt = (
                f"[agent-error:{rec.target_agent_name} "
                f"delegation={rec.delegation_id} reason={reason}]\n"
                f"(child session ended in state {rec.state!r}; do not retry "
                f"automatically because external side effects may already "
                f"have happened.){partial_note}"
            )
        if not rec.parent_session_id:
            logger.info(
                "delegation %s round %s has no parent; terminal delivery skipped",
                rec.delegation_id, rec.round_no,
            )
            rec._terminal_injected = True
        else:
            try:
                injection = await self.session_mgr.enqueue_session_injection(
                    source_key=f"delegation:{rec.run_id}:terminal",
                    session_id=rec.parent_session_id,
                    prompt=prompt,
                )
                rec._terminal_injected = True
            except Exception:
                # The outbox insert may already have committed.  Its source key
                # makes a later recovery/enqueue idempotent, so leave the local
                # guard false unless enqueue returned successfully.
                logger.exception(
                    "Failed to enqueue delegation %s round %s terminal for %s",
                    rec.delegation_id,
                    rec.round_no,
                    rec.parent_session_id,
                )
        # In live operation, archive after the parent intent exists. Restart
        # recovery passes archive=False and performs one tree-wide archive
        # phase only after nested pending events have been materialised.
        if archive:
            try:
                await self.session_mgr.auto_archive_scheduled_session(
                    rec.delegation_id
                )
            except Exception:
                logger.exception(
                    "Failed to auto-archive delegation child %s after "
                    "terminal injection",
                    rec.delegation_id,
                )
        return injection


# Module-level singleton (mirrors the session_manager / bg_tasks
# pattern). Wired in main.py's lifespan with .bind(...).
delegation_manager = DelegationManager()
