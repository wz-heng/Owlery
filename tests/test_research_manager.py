"""ResearchManager + research_jobs persistence (native-deep-research.md §6).

The pipeline itself is faked (monkeypatching `run_research`) so these tests
exercise the manager/DB/lifecycle — start, progress→phase, completion,
report injection, cancel, the web-capability gate, and the boot sweep —
without a real CLI.
"""

from __future__ import annotations

import asyncio

import pytest

from server.agent_manager import AgentManager
from server.database import Database
from server.research import manager as rm_mod
from server.research.manager import ResearchManager
from server.research.orchestrator import ResearchProgress, ResearchReport
from server.session_manager import SessionManager


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
async def mgr(db):
    m = SessionManager()
    await m.initialize(db)
    yield m


@pytest.fixture
async def rm(mgr, db):
    r = ResearchManager()
    r.bind(session_mgr=mgr, db=db)
    yield r


async def _session(mgr, db, *, backend="claude-code"):
    agent = await db.get_default_agent()
    return await mgr.create_session(agent["id"], "S", "/tmp", backend=backend)


def _fake_report(question="q"):
    return ResearchReport(
        question=question, report="# Final report\nThe answer [http://a].",
        findings=[], sources=["http://a"], angles=["x"], claims_examined=3, cost=0.05,
    )


@pytest.mark.asyncio
async def test_research_jobs_crud_and_sweep(mgr, db):
    session = await _session(mgr, db)  # research_jobs.session_id FKs to sessions
    await db.create_research_job("j1", session.id, "what?", "2026-06-13T00:00:00Z")
    row = await db.get_research_job("j1")
    assert row["status"] == "running" and row["injection_status"] == "pending"
    await db.update_research_job("j1", phase="verify", cost=0.1)
    assert (await db.get_research_job("j1"))["phase"] == "verify"
    # boot sweep flips running → interrupted
    n = await db.mark_in_flight_research_jobs_interrupted("2026-06-13T01:00:00Z")
    assert n == 1
    assert (await db.get_research_job("j1"))["status"] == "interrupted"


@pytest.mark.asyncio
async def test_start_completes_and_injects_report(rm, mgr, db, monkeypatch):
    injected: list[tuple[str, str]] = []

    async def fake_start_message(
        session_id, prompt, attachment_ids=None, injection_id=None
    ):
        injected.append((session_id, prompt))
        if injection_id:
            await db.append_message(
                session_id,
                seq=await db.count_messages(session_id),
                role="user",
                type="text",
                content=prompt,
                injection_id=injection_id,
            )

    monkeypatch.setattr(mgr, "start_message", fake_start_message)

    async def fake_run_research(question, *, on_progress=None, **kw):
        if on_progress:
            await on_progress(ResearchProgress(phase="search", detail="…"))
            await on_progress(ResearchProgress(phase="done", detail="done"))
        return _fake_report(question)

    monkeypatch.setattr(rm_mod, "run_research", fake_run_research)

    session = await _session(mgr, db)
    row = await rm.start(session.id, "what is X?")
    rid = row["id"]
    # let the background task finish
    await asyncio.wait_for(rm._tasks[rid], timeout=5)

    final = await db.get_research_job(rid)
    assert final["status"] == "completed" and final["phase"] == "done"
    assert final["injection_status"] == "delivered"
    assert final["cost"] == pytest.approx(0.05)
    assert injected and injected[0][0] == session.id
    assert injected[0][1].startswith(f"[deep-research:{rid}]")
    assert "Final report" in injected[0][1]


@pytest.mark.asyncio
async def test_start_rejects_backend_without_web(rm, mgr, db, monkeypatch):
    session = await _session(mgr, db)

    class _NoWeb:
        class profile:
            web = None
            credential_style = "env_secret"

    monkeypatch.setattr(rm_mod, "get_harness", lambda _b: _NoWeb())
    with pytest.raises(rm_mod.ResearchError) as ei:
        await rm.start(session.id, "q")
    assert ei.value.status_code == 409


@pytest.mark.asyncio
async def test_cancel_marks_cancelled(rm, mgr, db, monkeypatch):
    started = asyncio.Event()

    async def fake_run_research(question, *, on_progress=None, **kw):
        started.set()
        await asyncio.sleep(30)  # block until cancelled
        return _fake_report()

    monkeypatch.setattr(rm_mod, "run_research", fake_run_research)
    monkeypatch.setattr(mgr, "start_message", lambda *a, **k: asyncio.sleep(0))

    session = await _session(mgr, db)
    row = await rm.start(session.id, "q")
    rid = row["id"]
    await asyncio.wait_for(started.wait(), timeout=5)
    # cancel() records the state transition BEFORE interrupting, so the
    # returned row is already cancelled (no stale "running") — Vera review.
    cancelled = await rm.cancel(rid)
    assert cancelled["status"] == "cancelled"
    assert (await db.get_research_job(rid))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_recover_interrupted(rm, mgr, db):
    session = await _session(mgr, db)
    await db.create_research_job("old", session.id, "q", "2026-06-13T00:00:00Z")
    n = await rm.recover_interrupted()
    assert n == 1
    assert (await db.get_research_job("old"))["status"] == "interrupted"


@pytest.mark.asyncio
async def test_recover_completed_pending_report_enqueues_delivery(
    rm, mgr, db, tmp_path, monkeypatch
):
    session = await _session(mgr, db)
    report = tmp_path / "report.md"
    report.write_text("# Durable report\nRecovered.", encoding="utf-8")
    await db.create_research_job("done", session.id, "what?", "2026-06-13T00:00:00Z")
    await db.update_research_job(
        "done",
        status="completed",
        phase="done",
        report_path=str(report),
        completed_at="2026-06-13T01:00:00Z",
    )
    captured: list[tuple[str, str, str | None]] = []

    async def accept(sid, prompt, attachment_ids=None, injection_id=None):
        captured.append((sid, prompt, injection_id))

    monkeypatch.setattr(mgr, "start_message", accept)
    assert await rm.recover_interrupted() == 0
    assert captured and captured[0][0] == session.id
    assert "Durable report" in captured[0][1]
    outbox = await db.get_session_injection_by_source("research:done")
    assert outbox and outbox["status"] == "pending"


@pytest.mark.asyncio
async def test_recover_uses_existing_pending_outbox_without_report_file(
    rm, mgr, db
):
    session = await _session(mgr, db)
    await db.create_research_job("queued", session.id, "what?", "2026-06-13T00:00:00Z")
    await db.update_research_job(
        "queued",
        status="completed",
        phase="done",
        completed_at="2026-06-13T01:00:00Z",
        report_path=None,
    )
    await db.create_session_injection(
        injection_id="inj-queued",
        source_key="research:queued",
        session_id=session.id,
        prompt="durable report payload",
        created_at="2026-06-13T01:00:00Z",
    )
    assert await rm.recover_interrupted() == 0
    row = await db.get_research_job("queued")
    assert row["injection_status"] == "pending"
    assert row["error"] is None


@pytest.mark.asyncio
async def test_recover_repairs_research_compatibility_mirror_from_outbox(
    rm, mgr, db
):
    session = await _session(mgr, db)
    await db.create_research_job("mirrored", session.id, "what?", "2026-06-13T00:00:00Z")
    await db.update_research_job(
        "mirrored",
        status="completed",
        phase="done",
        completed_at="2026-06-13T01:00:00Z",
    )
    await db.create_session_injection(
        injection_id="inj-mirrored",
        source_key="research:mirrored",
        session_id=session.id,
        prompt="durable report payload",
        created_at="2026-06-13T01:00:00Z",
    )
    await db.append_message(
        session.id,
        seq=0,
        role="user",
        type="text",
        content="durable report payload",
        injection_id="inj-mirrored",
    )
    # Reproduce a crash/legacy reader seeing a stale compatibility value.
    await db.update_research_job(
        "mirrored", injection_status="pending", injected_at=None
    )
    assert await rm.recover_interrupted() == 0
    row = await db.get_research_job("mirrored")
    assert row["injection_status"] == "delivered"
    assert row["injected_at"]
