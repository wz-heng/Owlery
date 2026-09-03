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
8. `parent_id` means decomposition ("part of this goal"), never sequencing.
   One battle = one root task; fix rounds, review rounds, and final
   acceptance are all flat children of that SAME root, not nested under the
   previous round's task. Only `task_dependencies` (the `dependencies` param)
   expresses "must finish before" — set it explicitly where order matters.
9. If your job is to review or accept someone else's work, `complete` it
   with `verdict="pass"` or `verdict="fail"` — never a bare summary. A
   dependent task only unblocks on a `done` task whose verdict is not
   `"fail"`; prose alone never gates anything.
10. Finding that upstream work — or your own — does not pass is reported,
    not fixed by you spawning new work. `complete(verdict="fail")` or
    `block` and stop. Opening a fix/follow-up task in response is the
    orchestrator's or user's call, never the worker's; creating one anyway
    produces duplicate, uncoordinated cards.
11. A non-clean-pass run (a retry, a prior blocked/failed/interrupted run,
    or `verdict="fail"`) cannot `complete` until you call `reflect` at
    least once for THIS run. On a genuinely CLEAN pass you may also — purely
    voluntarily, never required — judge that the flow you just walked was
    novel or complex enough to be worth distilling while you still hold
    full context: call `reflect` (skills `propose` for a repeatable
    process, a memory write for a judgment call, a CLAUDE.md diff for a
    rule everyone should know) yourself, right here, then pass
    `complete(reusable_outcome=True)`. Never delegate this write-up to a
    fresh agent — it would have to re-read your history from scratch.
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
