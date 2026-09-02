"""Real Codex integration: the skill activation adapter
(experience-consolidation-v2.md §3④/§5 touchstone C).

Proves the real chain — approve materializes a Codex-canonical copy ->
`sync_codex_skills_dir` projects it into a real `$CODEX_HOME/skills` ->
Codex's own native discovery finds it — end to end through Owlery's actual
harness spawn path (`get_harness("codex").create_run(...)`, the same seam
`test_backend_codex_real.py` uses), not a bare shelled-out `codex exec` and
not a browser/Playwright flow. This is Albus's ruling on how touchstone C
("real spawn path, not a faked call" — v1 T-B's old e2e touchstone faked
the `Skill` tool_use instead of proving real discovery) is satisfied without
either asserting on live-model decision-making (untestable, would make the
suite flaky in a way no Owlery code change could ever fix) or bypassing the
exact seam the feature adds (a bare `codex exec` against a hand-placed
directory would prove Codex reads that shape of directory, but not that
Owlery's own sync ever puts one there for a real session).

Needs `codex` installed AND a logged-in ChatGPT subscription
(`~/.codex/auth.json`, or wherever `$CODEX_HOME` points); auto-skips
otherwise via `tests.cli_gate.codex_cli_works()`, exactly like
`test_backend_codex_real.py`.

CAUTION: to get a real, directory-backed Codex credential that resolves for
real (the sync only ever fires for one — session_manager.py deliberately
skips it for host-default auth, to never touch the user's real ambient
`$HOME`/`$CODEX_HOME`), this test READ-ONLY copies the host's real
`auth.json` into a scratch `CODEX_HOME` and spawns one real codex turn
against that copy. It never writes back to the real `$CODEX_HOME`. That one
real request carries a small chance of triggering an OAuth refresh-token
rotation on the copied token, which — rarely — could invalidate the host's
own `codex login` session (same class of risk as any other `_real.py` test
touching a real backend, called out here because this one explicitly
duplicates credential material). If `codex` starts reporting "not logged
in" after running this suite, `codex login` again.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.cli_gate import codex_cli_works
from server.codex_login import codex_home_for
from server.config import settings
from server.database import Database
from server.harness import HarnessEvent, RunConfig, get_harness
from server.harness.events import HarnessCredential
from server.skill_registry import SkillRegistry

pytestmark = pytest.mark.skipif(
    not codex_cli_works(),
    reason="codex CLI not on PATH or no ~/.codex login; skipping real-CLI tests",
)


def _real_codex_home() -> str:
    """Wherever the REAL `codex` binary reads auth from — `codex_cli_works()`
    spawns it with no CODEX_HOME override, so this must match that: the
    ambient `$CODEX_HOME` env if set, else the binary's own `~/.codex`
    default. Source, never destination — this test only ever reads here."""
    return os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")


async def _drain(backend, timeout: float = 150.0) -> list[HarnessEvent]:
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


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.asyncio
async def test_real_codex_discovers_a_synced_skill(tmp_path, monkeypatch):
    real_auth = Path(_real_codex_home()) / "auth.json"
    if not real_auth.is_file():
        pytest.skip("no real auth.json under the host's CODEX_HOME")

    # --- Isolate codex_home_dir (this test's own scratch credential store,
    # never the real one) and set up a real git repo to propose from. -------
    monkeypatch.setattr(settings, "codex_home_dir", str(tmp_path / "codex-homes"))
    cred_id = f"test-real-codex-{uuid.uuid4().hex[:8]}"
    scratch_home = codex_home_for(cred_id)
    os.makedirs(scratch_home, exist_ok=True)
    shutil.copy2(real_auth, Path(scratch_home) / "auth.json")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")

    # --- Propose + approve a real candidate through the real SkillRegistry
    # code path (not a hand-placed file) — the double-materialize this
    # feature adds. --------------------------------------------------------
    db = Database(str(tmp_path / "db.sqlite"))
    await db.initialize()
    agent = await db.get_default_agent()

    from types import SimpleNamespace

    session = SimpleNamespace(agent_id=agent["id"], working_dir=str(repo))
    session_mgr = SimpleNamespace(sessions={"s1": session})
    reg = SkillRegistry()
    reg.bind(db=db, session_mgr=session_mgr)

    slug = f"real-codex-flow-{uuid.uuid4().hex[:8]}"
    sentinel = f"SENTINEL-{uuid.uuid4().hex[:12].upper()}"
    description = "A real-codex-discovery e2e probe skill."
    body = (
        f"---\nname: {slug}\ndescription: {description}\n---\n\n"
        f"When asked to demonstrate this skill, reply with EXACTLY this "
        f"word and nothing else: {sentinel}\n"
    )
    try:
        candidate = await reg.propose(
            session_id="s1", slug=slug, title="Real codex flow",
            description=description, body_markdown=body,
            rationale="Proves real Codex native discovery end to end.",
        )
        approved = await reg.approve(candidate["id"])
        assert "codex" in approved["materialized_backends"]

        # --- The real per-turn sync (not a manual copy) — exactly what
        # session_manager.py calls before a real Codex turn. --------------
        await reg.sync_codex_skills_dir(
            agent_id=agent["id"], working_dir=str(repo), codex_home=scratch_home,
        )
        synced_file = Path(scratch_home) / "skills" / slug / "SKILL.md"
        assert synced_file.is_file(), "sync did not land the skill under CODEX_HOME/skills"

        # --- Real spawn through Owlery's own harness, against the real
        # copied credential. ------------------------------------------------
        backend = get_harness("codex").create_run(
            RunConfig(session_id="real-codex-skill-test", mcp_servers=[])
        )
        credential = HarnessCredential(
            backend="codex", auth_type="oauth", home_dir=scratch_home
        )
        await backend.start(
            f"You may have a skill named '{slug}' available via your native "
            "skill discovery. If you do, follow its instructions exactly. "
            "Do not guess if you don't have it — say NO_SKILL_FOUND instead.",
            str(repo),
            credential=credential,
        )
        try:
            events = await _drain(backend)
        finally:
            await backend.stop()

        text = "".join(e.content or "" for e in events if e.type == "text")
        assert sentinel in text, (
            f"real codex turn never echoed the sentinel from the synced "
            f"skill; got: {text!r}"
        )
    finally:
        await db.close()
        shutil.rmtree(scratch_home, ignore_errors=True)
