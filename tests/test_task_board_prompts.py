"""task-board-gaps.md §3.2: the worker prompt must carry the "report, don't
self-create a fix card" and "verdict-gate review/acceptance work" discipline
as plain text — there is no tool-level enforcement (§3.2 says so
explicitly: workers keep the ability to create tasks for legitimate
decomposition), so the prompt copy IS the mechanism.
"""

from __future__ import annotations

from server.task_board.prompts import TASK_WORKER_SYSTEM_PROMPT


def test_prompt_forbids_self_created_fix_cards_on_failure():
    lowered = TASK_WORKER_SYSTEM_PROMPT.lower()
    assert "not fixed by you spawning new work" in lowered
    assert "orchestrator's or user's call, never the worker's" in lowered


def test_prompt_requires_verdict_for_review_and_acceptance_work():
    assert 'verdict="pass"' in TASK_WORKER_SYSTEM_PROMPT
    assert 'verdict="fail"' in TASK_WORKER_SYSTEM_PROMPT
    lowered = TASK_WORKER_SYSTEM_PROMPT.lower()
    assert "review" in lowered and "accept" in lowered
