"""AgentBeatsExamManager — drives the real Owlery harness through the
`terminal-bench-shell-v1` protocol (docs/plans/agentbeats-a2a-adapter.md).

Shape mirrors `server/delegations.py` and `server/bg_tasks.py`: a thin
coordinator bound into `SessionManager`'s broadcast bus at startup, holding
only transient in-memory state (no new DB table — this phase's explicit,
written-down scope, §10 of the plan doc).

One A2A `context_id` == one Terminal Bench 2.0 task trial == one throwaway,
no-memory Owlery agent + session, created lazily on the first message and
hard-deleted after the terminal decision. Memory never has a chance to leak
between tasks because there is no second session to leak *into* — a fresh
agent_id means a fresh (and later deleted) memory dir every time
(agent-collaboration.md §5.10, memory.md).

Each `handle_message` call is exactly one A2A round: decode the incoming
`terminal-bench-shell-v1` JSON, turn it into one Owlery turn, wait for that
turn to conclude (the `result`/`error` broadcast event — the same
authoritative "turn is over" signal `delegations.py` relies on, which also
carries the turn's real dollar cost), and encode whatever decision the model
recorded via the `tbexec` MCP tool (server/mcp_servers/tbexec.py) as the
outgoing protocol JSON.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..agent_manager import AgentManager
    from ..session_manager import SessionManager

logger = logging.getLogger(__name__)

# Per-task budget (docs/plans/agentbeats-phase1.md §"设计要点"4): $3 or 50
# A2A round-trips, whichever comes first. A "turn" in the task card's
# vocabulary is one exec/final decision round, not an Owlery CLI-internal
# tool-call loop.
TASK_COST_BUDGET_USD = 3.0
TASK_ROUND_BUDGET = 50

# Must stay comfortably under the green agent's own per-message A2A client
# timeout (terminal-bench-green's `messenger.py` default is 300s) so we are
# never the one left holding a half-finished round when it gives up first.
ROUND_TIMEOUT_SEC = 280.0

# Green abandons a task without ever telling purple when its own exec call
# times out (confirmed against the real reference agent, not a theoretical
# concern — agentbeats-a2a-adapter.md §3): there is then no further
# `handle_message` call to react to, so nothing would otherwise ever delete
# the throwaway exam agent+session. `_reap_idle` sweeps for exactly that:
# an unfinished record whose last incoming message is older than
# `IDLE_REAP_SEC`. That threshold must clear the largest legitimate gap
# between two `handle_message` calls on the same context — either our own
# `ROUND_TIMEOUT_SEC` (while a round is in flight) or green's own exec-side
# wait (clamped to 300s upstream, plus container teardown/startup
# overhead) — with real margin, so the reaper can never race a round that's
# merely slow, only one that's truly gone. `REAP_POLL_SEC` just paces how
# often the sweep runs; it doesn't itself need to be tight.
IDLE_REAP_SEC = 900.0
REAP_POLL_SEC = 60.0

# Shared between `_start_task` (naming) and `_sweep_stale_exam_state`
# (matching) so they can't drift. The in-memory `_records`/reaper above
# only reach orphans within *this* process's lifetime — a restart between
# "purple handed back a decision" and the idle threshold wipes that state
# but not the DB rows, so a fresh process has no record of them at all.
# The sweep is the other half: on bind(), find every exam agent left over
# from a previous process (name prefix below, plus `origin="agentbeats"`
# on its session as a second, independent signal) and delete it — these
# are one-shot by design (§2.4), so any that still exist at a fresh
# process's startup are leftovers, never a resumable in-progress trial.
_EXAM_AGENT_NAME_PREFIX = "tb-exam-"

# Claude Code's native tools, denied in full for the exam agent. The only
# thing it can act through is the two `tbexec` MCP tools — the task's real
# filesystem lives inside the green agent's sandbox, not on this machine, so
# any native Bash/Write/Edit call here would be acting on the *wrong* box.
# A denylist, not an allowlist: `claude_code.py`'s own comment on the
# research-leaf precedent is explicit that `--allowedTools` semantics are
# ambiguous under `--dangerously-skip-permissions` (which this harness
# always passes) — a denylist is unambiguous.
_EXAM_DENY_TOOLS = (
    "Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
    "Glob", "Grep", "LS", "WebSearch", "WebFetch", "NotebookRead",
    "TodoWrite", "Task", "BashOutput", "KillBash",
)

_EXAM_MCP_SERVERS = ["tbexec"]

_EXAM_AGENT_PERSONA = (
    "You are a general-purpose AI assistant being evaluated on a task in a "
    "sandboxed Linux environment. You have no local shell, filesystem, or "
    "browser access — the only way to act on the task's environment is the "
    "`tbexec` tool:\n"
    "  - `run(command, timeout?)`: run exactly one shell command in the "
    "remote sandbox. Call it at most once per turn, then stop immediately "
    "with a short acknowledgement — do not guess at the output. You will "
    "see the real exit code, stdout, and stderr as your next turn's input.\n"
    "  - `done(output)`: call this once the task is solved, or once you "
    "have genuinely run out of ideas. This ends the exercise.\n"
    "Every turn, call exactly one of `run` or `done` — never both, never "
    "neither, never more than once."
)


class ExamTaskError(Exception):
    """A terminal-bench-shell-v1 protocol or lifecycle error."""


def _decode_protocol_message(text: str) -> dict[str, Any]:
    import json

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        raise ExamTaskError(f"Malformed protocol message (not JSON): {text[:200]!r}") from e
    if not isinstance(payload, dict) or "kind" not in payload:
        raise ExamTaskError(f"Protocol message missing 'kind': {payload!r}")
    return payload


def _encode_protocol_message(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload)


@dataclass
class _ExamRound:
    """State for exactly one in-flight A2A round-trip."""

    decision: dict[str, Any] | None = None
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    last_assistant_text: str = ""
    cost_usd: float | None = None
    is_error: bool = False
    error_message: str | None = None


@dataclass
class ExamTaskRecord:
    """Durable-enough-for-this-process summary of one finished/running task
    trial, for the cost ledger (agentbeats-phase1.md's "十题逐题结果+成本台账")."""

    context_id: str
    agent_id: str
    session_id: str
    round_no: int = 0
    cumulative_cost_usd: float = 0.0
    finished: bool = False
    aborted_reason: str | None = None
    final_output: str | None = None
    last_activity_at: float = field(default_factory=time.monotonic)


class AgentBeatsExamManager:
    """Coordinator bound into `SessionManager`'s broadcast bus at startup."""

    BROADCAST_KEY = "agentbeats_exam_manager"

    def __init__(self) -> None:
        self.session_mgr: "SessionManager | None" = None
        self.agent_mgr: "AgentManager | None" = None
        self._records: dict[str, ExamTaskRecord] = {}  # context_id -> record
        self._session_to_context: dict[str, str] = {}
        self._current_round: dict[str, _ExamRound] = {}  # context_id -> round
        self._reaper_task: asyncio.Task | None = None

    # ------------------------------------------------------------ wiring

    async def bind(self, session_mgr: "SessionManager", agent_mgr: "AgentManager") -> None:
        self.session_mgr = session_mgr
        self.agent_mgr = agent_mgr
        session_mgr.on_broadcast(self.BROADCAST_KEY, self._on_broadcast)
        await self._sweep_stale_exam_state()
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.ensure_future(self._reap_loop())

    async def _sweep_stale_exam_state(self) -> None:
        """Delete every exam agent (+ its session) left over from a
        *previous* process — see the module comment above
        `_EXAM_AGENT_NAME_PREFIX` for why anything matching here is
        necessarily a leftover, never a resumable trial. Runs once per
        `bind()`, after `SessionManager.initialize()` has already loaded
        every persisted session into `session_mgr.sessions` (true for
        `bind()`'s one real caller, `main.py`'s lifespan — it runs
        `initialize()` first).

        Name prefix alone is not a safe deletion key — nothing stops a
        user from naming their own agent `tb-exam-whatever`, and a brand
        new one would have zero sessions, so it'd otherwise sail through
        `delete_agent`'s own "no live sessions" guard untouched. An agent
        only counts as a leftover if it *also* owns a session with
        `origin="agentbeats"` — a value nothing in the public API lets a
        user set (`CreateSessionRequest` has no `origin` field; only this
        manager's own `_start_task` ever passes it) — so `stale_agent_ids`
        is derived from the matched sessions, never from name matching on
        its own.
        """
        agents_by_id = {
            a["id"]: a for a in await self.agent_mgr.list_agents(include_archived=True)
            if a["name"].startswith(_EXAM_AGENT_NAME_PREFIX)
        }
        if not agents_by_id:
            return
        stale_sessions = [
            s for s in self.session_mgr.sessions.values()
            if s.agent_id in agents_by_id and s.origin == "agentbeats"
        ]
        stale_agent_ids = {s.agent_id for s in stale_sessions}
        if not stale_agent_ids:
            return
        for session in stale_sessions:
            try:
                await self.session_mgr.delete_session(session.id)
            except Exception:
                logger.exception(
                    "Failed to delete stale exam session %s on startup sweep", session.id
                )
        for agent_id in stale_agent_ids:
            try:
                await self.agent_mgr.delete_agent(agent_id)
            except Exception:
                logger.exception(
                    "Failed to delete stale exam agent %s on startup sweep", agent_id
                )
        logger.warning(
            "AgentBeats startup sweep: deleted %d leftover exam agent(s) "
            "(%d session(s)) from a previous process",
            len(stale_agent_ids), len(stale_sessions),
        )

    async def shutdown(self) -> None:
        if self.session_mgr is not None:
            self.session_mgr.remove_broadcast(self.BROADCAST_KEY)
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reaper_task = None

    # ------------------------------------------------------------ ledger

    def get_record(self, context_id: str) -> ExamTaskRecord | None:
        return self._records.get(context_id)

    # ------------------------------------------------------------ the A2A entrypoint

    async def handle_message(self, context_id: str, raw_text: str) -> str:
        """One A2A round. `raw_text` is the incoming protocol JSON string
        from the green agent; returns the outgoing protocol JSON string."""
        assert self.session_mgr is not None and self.agent_mgr is not None, (
            "AgentBeatsExamManager.bind() must run before handle_message()"
        )
        payload = _decode_protocol_message(raw_text)
        kind = payload.get("kind")

        record = self._records.get(context_id)
        if record is None:
            if kind != "task":
                raise ExamTaskError(
                    f"First message for a new task must be kind='task', got {kind!r}"
                )
            record = await self._start_task(context_id, payload)
            prompt = str(payload.get("instruction", ""))
        else:
            if record.finished:
                raise ExamTaskError(f"Task {context_id} already finished")
            if kind != "exec_result":
                raise ExamTaskError(f"Expected kind='exec_result', got {kind!r}")
            prompt = _format_exec_result(payload)

        # Refresh before any `await` below so a concurrent `_reap_idle` sweep
        # always sees a live round as freshly active, never idle (see
        # IDLE_REAP_SEC's comment for why that margin holds).
        record.last_activity_at = time.monotonic()

        decision = await self._run_round(record, prompt)
        if decision["kind"] == "final":
            record.finished = True
            record.final_output = decision.get("output")
            await self._teardown(record)
        return _encode_protocol_message(decision)

    # ------------------------------------------------------------ task lifecycle

    async def _start_task(self, context_id: str, payload: dict[str, Any]) -> ExamTaskRecord:
        agent = await self.agent_mgr.create_agent(
            name=f"{_EXAM_AGENT_NAME_PREFIX}{uuid.uuid4().hex[:10]}",
            description=(
                "Throwaway AgentBeats Terminal Bench 2.0 exam agent "
                "(agentbeats-a2a-adapter.md) — no production persona, no "
                "memory carries across tasks, hard-deleted after this trial."
            ),
            system_prompt=_EXAM_AGENT_PERSONA,
            backend="claude-code",
            mcp_servers=list(_EXAM_MCP_SERVERS),
            tool_deny="\n".join(_EXAM_DENY_TOOLS),
        )
        session = await self.session_mgr.create_session(
            agent_id=agent["id"],
            name=f"agentbeats-exam-{context_id[:8]}",
            origin="agentbeats",
        )
        record = ExamTaskRecord(
            context_id=context_id, agent_id=agent["id"], session_id=session.id
        )
        self._records[context_id] = record
        self._session_to_context[session.id] = context_id
        logger.info(
            "AgentBeats exam task %s started: agent=%s session=%s",
            context_id, agent["id"], session.id,
        )
        return record

    async def _teardown(self, record: ExamTaskRecord) -> None:
        self._session_to_context.pop(record.session_id, None)
        self._current_round.pop(record.context_id, None)
        try:
            await self.session_mgr.delete_session(record.session_id)
        except Exception:
            logger.exception("Failed to delete exam session %s", record.session_id)
        try:
            await self.agent_mgr.delete_agent(record.agent_id)
        except Exception:
            logger.exception("Failed to delete exam agent %s", record.agent_id)
        logger.info(
            "AgentBeats exam task %s torn down: rounds=%d cost=$%.4f",
            record.context_id, record.round_no, record.cumulative_cost_usd,
        )

    # ------------------------------------------------------------ idle reaper

    async def _reap_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(REAP_POLL_SEC)
                await self._reap_idle()
        except asyncio.CancelledError:
            pass

    async def _reap_idle(self) -> None:
        """Tear down any unfinished record green has gone silent on. Safe
        against racing a round that's merely slow: `handle_message` stamps
        `last_activity_at` fresh before awaiting anything, and the longest
        a round can legitimately run before either returning or green's
        next call arriving is far under `IDLE_REAP_SEC` (its own docstring
        above works out the margin) — so anything still idle past that is
        abandoned, not busy. Setting `finished = True` before the (async)
        teardown closes the same race the other end of `handle_message`
        already guards with its own `record.finished` check."""
        now = time.monotonic()
        for record in list(self._records.values()):
            if record.finished:
                continue
            if now - record.last_activity_at <= IDLE_REAP_SEC:
                continue
            record.finished = True
            record.aborted_reason = "idle timeout: green agent never responded"
            logger.warning(
                "AgentBeats exam task %s reaped: idle %.0fs with no message from green",
                record.context_id, now - record.last_activity_at,
            )
            await self._teardown(record)

    # ------------------------------------------------------------ one round

    async def _run_round(self, record: ExamTaskRecord, prompt: str) -> dict[str, Any]:
        if (
            record.round_no >= TASK_ROUND_BUDGET
            or record.cumulative_cost_usd >= TASK_COST_BUDGET_USD
        ):
            record.aborted_reason = (
                f"budget exceeded (rounds={record.round_no}, "
                f"cost=${record.cumulative_cost_usd:.4f})"
            )
            return {"kind": "final", "output": f"(aborted: {record.aborted_reason})"}

        record.round_no += 1
        round_ = _ExamRound()
        self._current_round[record.context_id] = round_

        await self.session_mgr.start_message(record.session_id, prompt)

        try:
            await asyncio.wait_for(round_.completed.wait(), timeout=ROUND_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            await self.session_mgr.interrupt(record.session_id)
            return {"kind": "final", "output": "(aborted: turn timed out)"}

        if round_.cost_usd is not None:
            record.cumulative_cost_usd += round_.cost_usd

        if round_.is_error:
            return {
                "kind": "final",
                "output": f"(aborted: harness error: {round_.error_message})",
            }

        if round_.decision is not None:
            kind = round_.decision.get("kind")
            if kind == "exec":
                return {
                    "kind": "exec_request",
                    "command": round_.decision.get("command", ""),
                    "timeout": round_.decision.get("timeout", 60),
                }
            if kind == "finish":
                return {"kind": "final", "output": round_.decision.get("output", "")}
            # Shouldn't happen — the router only ever records "exec"/"finish".
            return {"kind": "final", "output": f"(aborted: bad decision {round_.decision!r})"}

        # The model never called `run`/`done` this turn (didn't follow the
        # protocol). Fall back to its plain assistant text as an implicit
        # "done" rather than silently dropping the task.
        return {
            "kind": "final",
            "output": round_.last_assistant_text or "(no decision recorded)",
        }

    # ------------------------------------------------------------ decision recording (called by the tbexec route)

    def record_decision(self, session_id: str, decision: dict[str, Any]) -> bool:
        """Returns False if a decision was already recorded this round
        (the route maps that to 409) — exactly one of `run`/`done` per turn."""
        context_id = self._session_to_context.get(session_id)
        if context_id is None:
            return False
        round_ = self._current_round.get(context_id)
        if round_ is None or round_.decision is not None:
            return False
        round_.decision = decision
        return True

    # ------------------------------------------------------------ broadcast

    async def _on_broadcast(self, msg: dict[str, Any]) -> None:
        session_id = msg.get("session_id")
        if not session_id:
            return
        context_id = self._session_to_context.get(session_id)
        if context_id is None:
            return
        round_ = self._current_round.get(context_id)
        if round_ is None:
            return

        event_type = msg.get("type")
        if event_type == "assistant_text":
            round_.last_assistant_text += msg.get("content", "")
        elif event_type == "result":
            round_.cost_usd = msg.get("cost")
            round_.is_error = bool(msg.get("is_error"))
            round_.completed.set()
        elif event_type == "error":
            round_.is_error = True
            round_.error_message = msg.get("message", "unknown error")
            round_.completed.set()


def _format_exec_result(payload: dict[str, Any]) -> str:
    exit_code = payload.get("exit_code")
    stdout = payload.get("stdout", "")
    stderr = payload.get("stderr", "")
    return (
        f"exit_code={exit_code}\n"
        f"stdout:\n{stdout}\n"
        f"stderr:\n{stderr}\n\n"
        "Decide your next action: call `run` for another command, or `done` "
        "once finished."
    )


# Process-wide singleton, bound by main.py's lifespan (same pattern as
# `delegation_manager`/`bg_task_manager`).
agentbeats_exam_manager = AgentBeatsExamManager()
