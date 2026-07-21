"""Usage tracking (docs/plans/usage-tracking.md): the turn_usage ledger,
its aggregation queries, the session-manager capture path, the WS
enrichment, the /api/usage routes, and the research usage summing."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from server.database import Database
from server.harness import HarnessEvent, TokenUsage
from server.main import app
from server.routers import usage as usage_mod
from server.session_manager import SessionManager, session_manager

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
    try:
        yield d
    finally:
        await d.close()


async def _add(db: Database, **overrides):
    row = {
        "created_at": "2026-07-01T10:00:00+00:00",
        "session_id": "s1",
        "agent_id": "a1",
        "backend": "claude-code",
        "cost": 0.01,
        "input_tokens": 10,
        "cache_read_tokens": 100,
        "cache_creation_tokens": 5,
        "output_tokens": 20,
        "reasoning_tokens": 0,
    }
    row.update(overrides)
    await db.add_turn_usage(**row)


# --------------------------------------------------------------------------- #
# database: add + summarize


@pytest.mark.asyncio
async def test_add_and_group_by_agent(db):
    await _add(db)
    await _add(db, agent_id="a2", cost=None, backend="codex", input_tokens=1000)
    await _add(db, agent_id="a2", cost=None, backend="codex", output_tokens=7)

    out = await db.summarize_usage(group_by="agent")
    assert out["group_by"] == "agent"
    rows = {r["key"]: r for r in out["rows"]}
    assert rows["a1"]["turns"] == 1
    assert rows["a1"]["cost"] == 0.01
    assert rows["a1"]["total_tokens"] == 10 + 100 + 5 + 20
    # SUM over all-NULL costs → None, not 0 (codex reports no USD).
    assert rows["a2"]["turns"] == 2
    assert rows["a2"]["cost"] is None
    assert rows["a2"]["input_tokens"] == 1010
    # id-keyed groupings are ordered by total tokens, biggest spender first
    assert out["rows"][0]["key"] == "a2"
    assert out["totals"]["turns"] == 3
    assert out["totals"]["cost"] == 0.01


@pytest.mark.asyncio
async def test_group_by_session_day_backend(db):
    await _add(db, created_at="2026-07-01T10:00:00+00:00", session_id="s1")
    await _add(db, created_at="2026-07-02T00:00:00+00:00", session_id="s2",
               backend="codex")

    by_session = await db.summarize_usage(group_by="session")
    assert {r["key"] for r in by_session["rows"]} == {"s1", "s2"}

    by_day = await db.summarize_usage(group_by="day")
    assert [r["key"] for r in by_day["rows"]] == ["2026-07-01", "2026-07-02"]

    by_backend = await db.summarize_usage(group_by="backend")
    assert {r["key"] for r in by_backend["rows"]} == {"claude-code", "codex"}


@pytest.mark.asyncio
async def test_window_and_filters(db):
    await _add(db, created_at="2026-07-01T10:00:00+00:00")
    await _add(db, created_at="2026-07-02T10:00:00+00:00", session_id="s2")
    await _add(db, created_at="2026-07-03T10:00:00+00:00")

    # since inclusive, until exclusive
    out = await db.summarize_usage(
        group_by="day", since="2026-07-02", until="2026-07-03"
    )
    assert [r["key"] for r in out["rows"]] == ["2026-07-02"]

    out = await db.summarize_usage(group_by="agent", session_id="s2")
    assert out["totals"]["turns"] == 1

    out = await db.summarize_usage(group_by="agent", agent_id="nobody")
    assert out["rows"] == []
    assert out["totals"] == {
        "turns": 0, "cost": None, "input_tokens": 0, "cache_read_tokens": 0,
        "cache_creation_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
        "total_tokens": 0,
    }


@pytest.mark.asyncio
async def test_null_agent_and_unknown_group_by(db):
    # agent_id is nullable — the ledger keeps pre-agent / orphan rows visible.
    await _add(db, agent_id=None)
    out = await db.summarize_usage(group_by="agent")
    assert out["rows"][0]["key"] is None

    with pytest.raises(ValueError):
        await db.summarize_usage(group_by="model'; DROP TABLE turn_usage;--")


@pytest.mark.asyncio
async def test_total_tokens_denormalized_and_model_usage_stored(db):
    await _add(db, model="claude-opus-4-7", model_usage={"m": {"costUSD": 1}},
               duration_ms=1234, is_error=True, origin="research")
    cursor = await db._conn.execute(
        "SELECT total_tokens, model, model_usage, duration_ms, is_error, origin "
        "FROM turn_usage"
    )
    row = await cursor.fetchone()
    assert row[0] == 10 + 100 + 5 + 20
    assert row[1] == "claude-opus-4-7"
    assert row[2] == '{"m": {"costUSD": 1}}'
    assert row[3] == 1234 and row[4] == 1 and row[5] == "research"


# --------------------------------------------------------------------------- #
# session manager: capture + WS enrichment


def _result_event(**kw) -> HarnessEvent:
    return HarnessEvent(
        type="result",
        cost=kw.pop("cost", 0.05),
        duration_ms=kw.pop("duration_ms", 900),
        usage=kw.pop("usage", TokenUsage(input_tokens=3, cache_read_tokens=50,
                                         output_tokens=9)),
        **kw,
    )


async def _new_session(mgr):
    """Create a session under the Default Agent (created by migration)."""
    agent = await mgr.db.get_default_agent()
    return await mgr.create_session(agent["id"], "S", "/tmp")


@pytest.mark.asyncio
async def test_record_turn_usage_writes_row(db):
    mgr = SessionManager()
    await mgr.initialize(db)
    session = await _new_session(mgr)

    await mgr._record_turn_usage(session, "claude-opus-4-7", _result_event())

    out = await db.summarize_usage(group_by="session")
    assert out["rows"][0]["key"] == session.id
    assert out["rows"][0]["cost"] == 0.05
    assert out["rows"][0]["total_tokens"] == 3 + 50 + 9
    # the session's owning agent is denormalized onto the row
    by_agent = await db.summarize_usage(group_by="agent")
    assert by_agent["rows"][0]["key"] == session.agent_id


@pytest.mark.asyncio
async def test_record_turn_usage_without_usage_and_never_raises(db, monkeypatch):
    mgr = SessionManager()
    await mgr.initialize(db)
    session = await _new_session(mgr)

    # A backend that reported no tokens still gets a (zeroed) turn row.
    await mgr._record_turn_usage(session, None, _result_event(usage=None, cost=None))
    out = await db.summarize_usage(group_by="session")
    assert out["totals"]["turns"] == 1
    assert out["totals"]["total_tokens"] == 0
    assert out["totals"]["cost"] is None

    # Ledger failure is swallowed (logged), never propagated into the turn.
    async def boom(**kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "add_turn_usage", boom)
    await mgr._record_turn_usage(session, None, _result_event())


def test_ws_result_event_carries_usage():
    ev = _result_event(num_turns=2)
    ws = SessionManager._event_to_ws_message("sid", ev)
    assert ws is not None
    assert ws["cost"] == 0.05
    assert ws["usage"] == {
        "input_tokens": 3, "cache_read_tokens": 50, "cache_creation_tokens": 0,
        "output_tokens": 9, "reasoning_tokens": 0, "total_tokens": 62,
    }

    # absent when the backend reported none — additive, never a null field
    ws = SessionManager._event_to_ws_message("sid", _result_event(usage=None))
    assert ws is not None and "usage" not in ws


# --------------------------------------------------------------------------- #
# routes


@pytest.fixture
async def client(db):
    session_manager.sessions.clear()
    await session_manager.initialize(db)
    usage_mod._db = db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_summary_requires_auth(client):
    resp = await client.get("/api/usage/summary")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_summary_happy_path(client, db):
    await _add(db)
    await _add(db, agent_id="a2", backend="codex", cost=None)

    resp = await client.get("/api/usage/summary", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["group_by"] == "agent"
    assert body["totals"]["turns"] == 2
    assert {r["key"] for r in body["rows"]} == {"a1", "a2"}

    resp = await client.get(
        "/api/usage/summary",
        params={"group_by": "day", "since": "2026-07-01", "until": "2026-07-02"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["rows"][0]["key"] == "2026-07-01"


@pytest.mark.asyncio
async def test_summary_validation(client):
    resp = await client.get(
        "/api/usage/summary", params={"group_by": "nope"}, headers=HEADERS
    )
    assert resp.status_code == 422

    for bad in (
        "not-a-date",
        "2026-07-01T10:00:00",  # naive datetime: zone would be a guess
        "2026-07-01 10:00:00",  # space-separated AND naive — still a guess
    ):
        resp = await client.get(
            "/api/usage/summary", params={"since": bad}, headers=HEADERS
        )
        assert resp.status_code == 422, bad


@pytest.mark.asyncio
async def test_summary_normalizes_timezone_bounds(client, db):
    # Row at 10:00 UTC; the same instant expressed as +08:00 must behave
    # identically to its UTC form (TEXT compare happens post-normalization).
    await _add(db, created_at="2026-07-01T10:00:00+00:00")

    resp = await client.get(
        "/api/usage/summary",
        params={"since": "2026-07-01T18:00:00+08:00"},  # == 10:00 UTC, inclusive
        headers=HEADERS,
    )
    assert resp.json()["totals"]["turns"] == 1

    resp = await client.get(
        "/api/usage/summary",
        params={"since": "2026-07-01T18:00:01+08:00"},  # one second past the row
        headers=HEADERS,
    )
    assert resp.json()["totals"]["turns"] == 0

    # The frontend's toISOString() "Z" suffix normalizes too ("Z" would
    # otherwise compare wrongly against "+00:00" rows as raw TEXT).
    resp = await client.get(
        "/api/usage/summary",
        params={"until": "2026-07-01T10:00:00Z"},  # exclusive at the row's instant
        headers=HEADERS,
    )
    assert resp.json()["totals"]["turns"] == 0


@pytest.mark.asyncio
async def test_summary_empty_db(client):
    resp = await client.get("/api/usage/summary", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == [] and body["totals"]["turns"] == 0


# --------------------------------------------------------------------------- #
# research: merge_usage + orchestrator summing


def test_merge_usage_math():
    from server.research.leaf import merge_usage

    assert merge_usage(None, None) is None
    first = merge_usage(None, TokenUsage(input_tokens=1, output_tokens=2))
    assert first is not None and first.total_tokens == 3
    merged = merge_usage(first, TokenUsage(input_tokens=10, cache_read_tokens=4,
                                           reasoning_tokens=1))
    assert merged is first  # accumulates in place
    assert merged.input_tokens == 11
    assert merged.cache_read_tokens == 4
    assert merged.reasoning_tokens == 1
    assert merge_usage(first, None) is first


@pytest.mark.asyncio
async def test_run_research_sums_leaf_usage_into_report():
    from server.research.leaf import LeafResult
    from server.research.orchestrator import ResearchLimits, run_research

    async def fake_reason(prompt: str) -> LeafResult:
        # reasoning leaves report no usage (run_oneshot boundary)
        if "angle" in prompt.lower() or "decompose" in prompt.lower():
            return LeafResult(text='["only angle"]')
        return LeafResult(text="# Report")

    async def fake_search(prompt: str) -> LeafResult:
        if "refuted" in prompt.lower() or "verify" in prompt.lower():
            return LeafResult(text='{"refuted": false}',
                              usage=TokenUsage(input_tokens=1, output_tokens=1))
        return LeafResult(
            text='[{"claim": "c", "url": "http://a"}]',
            cost=0.02,
            usage=TokenUsage(input_tokens=100, cache_read_tokens=30,
                             output_tokens=10),
        )

    report = await run_research(
        "q?",
        working_dir="/tmp",
        limits=ResearchLimits(max_angles=1, votes_per_claim=1),
        search=fake_search,
        reason=fake_reason,
    )
    assert report.usage is not None
    # one search leaf + one verify vote
    assert report.usage.input_tokens == 101
    assert report.usage.cache_read_tokens == 30
    assert report.usage.output_tokens == 11
