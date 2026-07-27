"""Task worker protocol and assignment prompt rendering."""

from __future__ import annotations

from typing import Any, Mapping


TASK_WORKER_SYSTEM_PROMPT = """\
== Owlery Task Board worker protocol ==

You are working one durable Task Board attempt. The board, task, run, Agent,
and workspace identity in OWLERY_TASK_* are trusted controller state.

Rules:
1. Call `mcp__tasks__show()` before acting and use it as the source of truth.
2. Work only inside the supplied run workspace.
3. Record durable intermediate findings with `mcp__tasks__comment()`.
4. Use `mcp__tasks__heartbeat()` during long model-driven work. Waiting for an
   Owlery background task, delegation, research job, approval, or queued
   injection is tracked by the controller and is not a failure.
5. End successful work with `mcp__tasks__complete(...)`; end work that needs
   human input/capability or has a known failure with `mcp__tasks__block(...)`.
   Prose alone never changes board state.
6. Do not retry interrupted external work automatically. Its side effects may
   already have occurred; inspect durable evidence and ask for a new attempt.
7. Child tasks and dependency links must stay on this board. Repository limits
   on tree depth, per-run fan-out, and open task count are authoritative.
"""


def _value(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(key, default)
    return getattr(record, key, default)


def render_assignment_prompt(
    *, task: Any, board: Any, run: Any, workspace: str
) -> str:
    """Render the intentionally small first-turn assignment."""
    title = str(_value(task, "title", "Untitled task"))
    task_id = str(_value(task, "id", ""))
    board_name = str(_value(board, "name", _value(board, "title", "Task Board")))
    run_id = str(_value(run, "id", ""))
    attempt = _value(run, "attempt_no", 1)
    return (
        f"Work Task Board item **{title}**.\n\n"
        f"Board: {board_name}\n"
        f"Task id: {task_id}\n"
        f"Run id: {run_id}\n"
        f"Attempt: {attempt}\n"
        f"Workspace: {workspace}\n\n"
        "Call `mcp__tasks__show()` now for the full durable context, then do "
        "the work. Finish through `complete` or `block`; do not merely report "
        "a terminal result in prose."
    )
