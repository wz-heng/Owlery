"""A2A `AgentExecutor` wrapping `AgentBeatsExamManager` — the thinnest
possible shim between the a2a-sdk's request/event-queue contract and the
real Owlery harness. Shape ported from RDI-Foundation's own
`agent-template`/`terminal-bench-green` `src/executor.py` (same TERMINAL_STATES
guard, same "one task per context_id" bookkeeping), adapted to call into
`AgentBeatsExamManager.handle_message` instead of a toy echo agent.
"""

from __future__ import annotations

import logging

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import InvalidRequestError, TaskState, UnsupportedOperationError
from a2a.utils import get_message_text, new_agent_text_message, new_task
from a2a.utils.errors import ServerError

from .manager import AgentBeatsExamManager

logger = logging.getLogger(__name__)

_TERMINAL_STATES = {
    TaskState.completed,
    TaskState.canceled,
    TaskState.failed,
    TaskState.rejected,
}


class OwleryExamExecutor(AgentExecutor):
    """One `handle_message` round per A2A `execute()` call."""

    def __init__(self, manager: AgentBeatsExamManager) -> None:
        self._manager = manager

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        msg = context.message
        if not msg:
            raise ServerError(error=InvalidRequestError(message="Missing message in request"))

        task = context.current_task
        if task and task.status.state in _TERMINAL_STATES:
            raise ServerError(
                error=InvalidRequestError(
                    message=f"Task {task.id} already processed (state: {task.status.state})"
                )
            )
        if not task:
            task = new_task(msg)
            await event_queue.enqueue_event(task)

        context_id = task.context_id
        updater = TaskUpdater(event_queue, task.id, context_id)
        await updater.start_work()
        try:
            reply_text = await self._manager.handle_message(
                context_id, get_message_text(msg)
            )
            await updater.add_artifact(
                parts=[_text_part(reply_text)], name="decision"
            )
            await updater.complete()
        except Exception as e:  # noqa: BLE001 — must reach the A2A caller as a failed task, not an unhandled 500
            logger.exception("AgentBeats exam task %s failed", context_id)
            await updater.failed(
                new_agent_text_message(f"Agent error: {e}", context_id=context_id, task_id=task.id)
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ServerError(error=UnsupportedOperationError())


def _text_part(text: str):
    from a2a.types import Part, TextPart

    return Part(root=TextPart(text=text))
