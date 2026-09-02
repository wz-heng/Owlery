"""Real Claude Code integration: skill discovery + use_count attribution
(experience-consolidation.md §3.4/§5, experience-consolidation-v2.md §5
touchstone C follow-up).

Snape's round-3 review of the Codex activation adapter asked for the same
"real spawn, not a faked call" rigor on the Claude side: does Owlery's own
use_count attribution actually match what a real `claude` process reports
for a `Skill` tool_use? A real spawn (2026-09-02) revealed it did NOT — a
plugin-provided Skill invocation is ALWAYS reported as a namespaced
"<plugin-name>:<slug>" value (confirmed with both a single --plugin-dir and
two colliding ones), not the bare slug every existing test (all fake-CLI or
mocked) had assumed. `session_manager.py`'s extraction silently never
matched a DB row for any real Claude session until this was fixed — this
test proves the fix end to end through the real pipeline: approve() lands a
real plugin dir, a real `claude` process discovers and invokes it, and
`record_usage()` (fed the SAME extraction session_manager.py uses) correctly
increments use_count on the right DB row.

Needs `claude` installed AND signed in; auto-skips otherwise via
`tests.cli_gate.claude_cli_works()`, matching test_backend_claude_code_real.py.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.cli_gate import claude_cli_works
from server.database import Database
from server.harness import HarnessEvent, RunConfig, get_harness
from server.skill_registry import SkillRegistry

pytestmark = pytest.mark.skipif(
    not claude_cli_works(),
    reason="claude CLI unavailable or not signed in; skip real-CLI tests",
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


async def _drain(backend, timeout: float = 60.0) -> list[HarnessEvent]:
    events: list[HarnessEvent] = []

    async def collect() -> None:
        async for ev in backend.stream():
            events.append(ev)

    try:
        await asyncio.wait_for(collect(), timeout=timeout)
    except asyncio.TimeoutError:
        raise AssertionError(
            f"stream() didn't terminate within {timeout}s. "
            f"Collected: {[e.type for e in events]}"
        )
    return events


@pytest.mark.asyncio
async def test_real_claude_skill_invocation_is_namespaced_and_attributes_correctly(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")

    db = Database(str(tmp_path / "db.sqlite"))
    await db.initialize()
    agent = await db.get_default_agent()
    session = SimpleNamespace(agent_id=agent["id"], working_dir=str(repo))
    session_mgr = SimpleNamespace(sessions={"s1": session})
    reg = SkillRegistry()
    reg.bind(db=db, session_mgr=session_mgr)

    slug = f"real-claude-flow-{uuid.uuid4().hex[:8]}"
    sentinel = f"SENTINEL-{uuid.uuid4().hex[:12].upper()}"
    description = "A real-Claude-discovery use_count attribution probe."
    body = (
        f"---\nname: {slug}\ndescription: {description}\n---\n\n"
        f"When invoked, reply with EXACTLY this word and nothing else: {sentinel}\n"
    )
    try:
        candidate = await reg.propose(
            session_id="s1", slug=slug, title="Real claude flow",
            description=description, body_markdown=body,
            rationale="Proves real Claude discovery + use_count attribution end to end.",
        )
        approved = await reg.approve(candidate["id"])
        assert approved["use_count"] == 0

        plugin_dirs = await reg.resolve_plugin_dir(
            agent_id=agent["id"], working_dir=str(repo)
        )
        assert len(plugin_dirs) == 1

        backend = get_harness("claude-code").create_run(
            RunConfig(model="haiku", skills_plugin_dirs=plugin_dirs)
        )
        await backend.start(
            f"Use your '{slug}' skill and reply with exactly what it says.",
            str(repo),
        )
        try:
            events = await _drain(backend)
        finally:
            await backend.stop()

        skill_use = next(
            (e for e in events if e.type == "tool_use" and e.tool_name == "Skill"),
            None,
        )
        assert skill_use is not None, (
            f"model never invoked the Skill tool; saw {[e.type for e in events]}"
        )
        raw_value = (skill_use.tool_input or {}).get("skill")
        assert isinstance(raw_value, str)
        # The real, load-bearing finding this test locks in: a real Skill
        # invocation is namespaced, not a bare slug.
        assert ":" in raw_value
        assert raw_value.rsplit(":", 1)[-1] == slug

        text = "".join(e.content or "" for e in events if e.type == "text")
        assert sentinel in text

        # The SAME extraction session_manager.py uses, feeding record_usage
        # for real — proves the fix closes the loop, not just the parsing.
        extracted_slug = raw_value.rsplit(":", 1)[-1]
        repository = await reg.resolve_repository(str(repo))
        await reg.record_usage(
            extracted_slug, agent_id=agent["id"], repository=repository,
            session_id="s1", backend="claude-code",
        )
        after = await reg.get_candidate(candidate["id"])
        assert after["use_count"] == 1
    finally:
        await db.close()
