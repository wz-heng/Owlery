"""Budget backend (budget-model-routing.md §3): the `budgets` table + CRUD,
window-scoped spend aggregation, the pre-run gate in the session manager
(soft warning once per window, hard block fails the turn fast and source-
agnostically), and the /api/budgets REST surface."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from server.budgets import (
    BudgetExceededError,
    BudgetStatus,
    budget_statuses,
    budget_window_start,
    classify_budget_statuses,
)
from server.database import Database
from server.main import app
from server.models import SessionStatus
from server.parked_turns import ParkedTurnRunner
from server.routers import budgets as budgets_mod
from server.session_manager import QueuedPrompt, SessionManager, session_manager

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


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# --------------------------------------------------------------------------- #
# budget_window_start — local-calendar boundaries, weeks start Monday
# --------------------------------------------------------------------------- #


def test_window_start_is_utc_and_bounded():
    now = datetime.now(timezone.utc)
    for window, span in (
        ("daily", timedelta(days=1)),
        ("weekly", timedelta(days=7)),
        ("monthly", timedelta(days=31)),
    ):
        start = budget_window_start(window, now)
        assert start.tzinfo == timezone.utc
        assert start <= now
        assert now - start < span


def test_window_start_local_calendar_invariants():
    now = datetime.now(timezone.utc)
    # Rendered back in the server's local zone, every boundary is local
    # midnight; weekly lands on Monday, monthly on the 1st.
    daily = budget_window_start("daily", now).astimezone()
    assert (daily.hour, daily.minute, daily.second, daily.microsecond) == (0, 0, 0, 0)

    weekly = budget_window_start("weekly", now).astimezone()
    assert weekly.weekday() == 0
    assert (weekly.hour, weekly.minute, weekly.second) == (0, 0, 0)

    monthly = budget_window_start("monthly", now).astimezone()
    assert monthly.day == 1
    assert (monthly.hour, monthly.minute, monthly.second) == (0, 0, 0)


def test_window_start_rejects_naive_and_unknown():
    with pytest.raises(ValueError):
        budget_window_start("daily", datetime(2026, 7, 1))  # naive
    with pytest.raises(ValueError):
        budget_window_start("yearly", datetime.now(timezone.utc))


# --------------------------------------------------------------------------- #
# Database CRUD
# --------------------------------------------------------------------------- #


async def test_budget_crud_roundtrip(db):
    b = await db.create_budget(scope="global", window="daily", limit_usd=10.0)
    assert b["scope"] == "global"
    assert b["agent_id"] is None
    assert b["soft_pct"] == 0.8
    assert b["enabled"] is True
    assert b["soft_warned_window"] is None

    got = await db.get_budget(b["id"])
    assert got == b

    rows = await db.list_budgets()
    assert [r["id"] for r in rows] == [b["id"]]

    updated = await db.update_budget(b["id"], limit_usd=25.0, enabled=False)
    assert updated["limit_usd"] == 25.0
    assert updated["enabled"] is False
    # Only enabled budgets show up when filtered.
    assert await db.list_budgets(enabled_only=True) == []

    assert await db.delete_budget(b["id"]) is True
    assert await db.delete_budget(b["id"]) is False
    assert await db.get_budget(b["id"]) is None


async def test_budget_uniqueness_per_scope_window(db):
    import sqlite3

    await db.create_budget(scope="global", window="daily", limit_usd=1.0)
    # A second global daily collides...
    with pytest.raises(sqlite3.IntegrityError):
        await db.create_budget(scope="global", window="daily", limit_usd=2.0)
    # ...but a global weekly is fine.
    await db.create_budget(scope="global", window="weekly", limit_usd=5.0)
    # Per-agent daily co-exists with the global daily.
    await db.create_budget(
        scope="agent", agent_id="a1", window="daily", limit_usd=3.0
    )
    with pytest.raises(sqlite3.IntegrityError):
        await db.create_budget(
            scope="agent", agent_id="a1", window="daily", limit_usd=4.0
        )
    # A different agent's daily is fine.
    await db.create_budget(
        scope="agent", agent_id="a2", window="daily", limit_usd=3.0
    )


async def test_budget_scope_agent_check_constraint(db):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        await db.create_budget(scope="agent", window="daily", limit_usd=1.0)
    with pytest.raises(sqlite3.IntegrityError):
        await db.create_budget(
            scope="global", agent_id="a1", window="daily", limit_usd=1.0
        )


# --------------------------------------------------------------------------- #
# budget_spent_usd — window boundary + agent scope + NULL cost
# --------------------------------------------------------------------------- #


async def _spend(db, *, cost, created_at, agent_id="a1", backend="claude-code"):
    await db.add_turn_usage(
        created_at=created_at,
        session_id="s1",
        agent_id=agent_id,
        backend=backend,
        cost=cost,
    )


async def test_spent_sums_within_window_and_excludes_before(db):
    start = budget_window_start("daily")
    # One turn just inside the window, one a second before it (previous day).
    await _spend(db, cost=0.30, created_at=_iso(start + timedelta(seconds=1)))
    await _spend(db, cost=99.0, created_at=_iso(start - timedelta(seconds=1)))
    spent = await db.budget_spent_usd(window_start=_iso(start))
    assert spent == pytest.approx(0.30)


async def test_spent_counts_boundary_second_despite_microseconds(db):
    """The created_at-format trap: a row stamped at the window-start second but
    carrying microseconds must still count (the `T`/space + fractional widths
    line up under a UTC-isoformat comparison)."""
    start = budget_window_start("daily")
    micro = start.replace(microsecond=123456)
    await _spend(db, cost=0.05, created_at=_iso(micro))
    await _spend(db, cost=0.07, created_at=_iso(start))  # exact boundary, no frac
    spent = await db.budget_spent_usd(window_start=_iso(start))
    assert spent == pytest.approx(0.12)


async def test_spent_null_cost_counts_zero_and_agent_scope(db):
    start = budget_window_start("daily")
    inside = _iso(start + timedelta(seconds=1))
    await _spend(db, cost=0.10, created_at=inside, agent_id="a1")
    await _spend(db, cost=None, created_at=inside, agent_id="a1", backend="codex")
    await _spend(db, cost=0.40, created_at=inside, agent_id="a2")

    # Global (no agent filter) sees everyone; NULL codex cost adds nothing.
    assert await db.budget_spent_usd(window_start=_iso(start)) == pytest.approx(0.50)
    # Agent-scoped sees only that agent.
    assert await db.budget_spent_usd(
        window_start=_iso(start), agent_id="a1"
    ) == pytest.approx(0.10)


async def test_spent_includes_research_origin(db):
    start = budget_window_start("daily")
    inside = _iso(start + timedelta(seconds=1))
    await db.add_turn_usage(
        created_at=inside,
        session_id="s1",
        agent_id="a1",
        backend="claude-code",
        cost=0.20,
        origin="research",
    )
    assert await db.budget_spent_usd(window_start=_iso(start)) == pytest.approx(0.20)


# --------------------------------------------------------------------------- #
# mark_budget_soft_warned — compare-and-set, once per window
# --------------------------------------------------------------------------- #


async def test_mark_soft_warned_is_compare_and_set(db):
    b = await db.create_budget(scope="global", window="daily", limit_usd=1.0)
    key = budget_window_start("daily").isoformat()
    assert await db.mark_budget_soft_warned(b["id"], key) is True
    # Same window → no-op, returns False.
    assert await db.mark_budget_soft_warned(b["id"], key) is False
    # A new window key flips it again.
    next_key = (budget_window_start("daily") + timedelta(days=1)).isoformat()
    assert await db.mark_budget_soft_warned(b["id"], next_key) is True


# --------------------------------------------------------------------------- #
# budget_statuses + classify
# --------------------------------------------------------------------------- #


async def test_budget_statuses_filters_and_scopes_spend(db):
    await db.create_budget(scope="global", window="daily", limit_usd=10.0)
    await db.create_budget(
        scope="agent", agent_id="a1", window="daily", limit_usd=2.0
    )
    await db.create_budget(
        scope="agent", agent_id="a2", window="daily", limit_usd=2.0
    )
    start = budget_window_start("daily")
    inside = _iso(start + timedelta(seconds=1))
    await _spend(db, cost=1.0, created_at=inside, agent_id="a1")
    await _spend(db, cost=4.0, created_at=inside, agent_id="a2")

    # For agent a1: the global budget + a1's budget only (not a2's).
    statuses = await budget_statuses(db, only_agent_id="a1")
    by_scope = {(s.scope, s.agent_id): s for s in statuses}
    assert set(by_scope) == {("global", None), ("agent", "a1")}
    assert by_scope[("global", None)].spent_usd == pytest.approx(5.0)  # 1 + 4
    assert by_scope[("agent", "a1")].spent_usd == pytest.approx(1.0)


def test_classify_picks_tightest_hard_and_collects_soft():
    def st(scope, spent, limit, soft=0.8, agent=None):
        return BudgetStatus(
            budget_id=f"{scope}-{agent}",
            scope=scope,
            agent_id=agent,
            window="daily",
            window_start="2026-07-28T00:00:00+00:00",
            limit_usd=limit,
            soft_pct=soft,
            spent_usd=spent,
        )

    # global 1.2/1.0 (ratio 1.2) vs agent 5/2 (ratio 2.5) → agent is tighter.
    hard, soft = classify_budget_statuses(
        [st("global", 1.2, 1.0), st("agent", 5.0, 2.0, agent="a1")]
    )
    assert hard is not None and hard.scope == "agent"
    assert soft == []

    # No hard; one soft (0.9 >= 0.8*1.0), one clear (0.5 < 0.8).
    hard, soft = classify_budget_statuses(
        [st("global", 0.9, 1.0), st("agent", 0.5, 1.0, agent="a1")]
    )
    assert hard is None
    assert [s.scope for s in soft] == ["global"]


def test_budget_exceeded_error_message_is_structured():
    s = BudgetStatus(
        budget_id="b1",
        scope="global",
        agent_id=None,
        window="daily",
        window_start="2026-07-28T00:00:00+00:00",
        limit_usd=10.0,
        soft_pct=0.8,
        spent_usd=12.5,
    )
    err = BudgetExceededError(s)
    text = str(err)
    assert "global" in text and "daily" in text
    assert "10.00" in text and "12.5" in text


# --------------------------------------------------------------------------- #
# Session-manager gate
# --------------------------------------------------------------------------- #


@pytest.fixture
async def manager(tmp_path):
    mgr = SessionManager()
    database = Database(":memory:")
    await database.initialize()
    await mgr.initialize(database)
    await database.save_session(
        "s1", "s1", str(tmp_path), datetime.now(timezone.utc).isoformat()
    )
    await mgr.initialize(database)  # reload so the session is live in memory
    mgr.set_parked_turn_runner(ParkedTurnRunner(mgr, database))
    mgr.sessions["s1"].agent_id = "a1"
    try:
        yield mgr
    finally:
        await database.close()


def _record_backend(calls):
    async def _run(session, prompt):
        calls.append(prompt)
        return
        yield  # pragma: no cover — makes this an async generator

    return _run


async def test_hard_block_fails_fast_without_running_backend(manager):
    db = manager.db
    session = manager.sessions["s1"]
    await db.create_budget(
        scope="agent", agent_id="a1", window="daily", limit_usd=0.01
    )
    inside = _iso(budget_window_start("daily") + timedelta(seconds=1))
    await _spend(db, cost=0.05, created_at=inside, agent_id="a1")

    calls: list[str] = []
    manager._run_backend = _record_backend(calls)

    events = [e async for e in manager.send_message("s1", "hi")]
    errs = [e for e in events if e["type"] == "error"]
    assert len(errs) == 1
    assert errs[0]["code"] == "budget_exceeded"
    assert errs[0]["budget"]["scope"] == "agent"
    assert errs[0]["budget"]["spent_usd"] == pytest.approx(0.05)
    # The backend never ran, and the session is healthy + unlocked afterward.
    assert calls == []
    assert session.status == SessionStatus.idle
    assert not session._lock.locked()


async def test_hard_block_is_source_agnostic(manager):
    """The gate lives in the one path every source funnels through, so an
    injection-dispatched turn (delegation / schedule / Task Board / bg) is
    blocked exactly like an interactive one."""
    db = manager.db
    await db.create_budget(scope="global", window="daily", limit_usd=0.01)
    inside = _iso(budget_window_start("daily") + timedelta(seconds=1))
    await _spend(db, cost=1.0, created_at=inside, agent_id="a1")

    calls: list[str] = []
    manager._run_backend = _record_backend(calls)

    await manager._consume_message(
        "s1", QueuedPrompt(prompt="do work", attachment_ids=[])
    )
    assert calls == []  # backend never ran
    msgs = await db.load_messages("s1")
    assert any(m["type"] == "error" for m in msgs)


async def test_codex_session_is_not_gated(manager):
    """Budgets govern Claude USD only; a Codex turn contributes no cost and is
    never blocked by a Claude budget (§3.1)."""
    db = manager.db
    session = manager.sessions["s1"]
    session.backend = "codex"
    await db.create_budget(scope="global", window="daily", limit_usd=0.01)
    inside = _iso(budget_window_start("daily") + timedelta(seconds=1))
    await _spend(db, cost=5.0, created_at=inside, agent_id="a1")

    calls: list[str] = []
    manager._run_backend = _record_backend(calls)

    events = [e async for e in manager.send_message("s1", "hi")]
    assert not any(e["type"] == "error" for e in events)
    assert calls == ["hi"]  # backend ran normally


async def test_soft_warning_fires_once_per_window(manager):
    db = manager.db
    await db.create_budget(
        scope="agent", agent_id="a1", window="daily", limit_usd=1.0, soft_pct=0.8
    )
    inside = _iso(budget_window_start("daily") + timedelta(seconds=1))
    await _spend(db, cost=0.85, created_at=inside, agent_id="a1")

    calls: list[str] = []
    manager._run_backend = _record_backend(calls)

    first = [e async for e in manager.send_message("s1", "one")]
    warns = [e for e in first if e["type"] == "budget_warning"]
    assert len(warns) == 1
    assert warns[0]["scope"] == "agent"
    assert warns[0]["spent_usd"] == pytest.approx(0.85)
    assert calls == ["one"]  # soft threshold does NOT block

    # Second turn in the same window: still soft, but no repeat warning.
    second = [e async for e in manager.send_message("s1", "two")]
    assert not any(e["type"] == "budget_warning" for e in second)
    assert calls == ["one", "two"]


async def test_disabled_budget_does_not_gate(manager):
    db = manager.db
    b = await db.create_budget(
        scope="global", window="daily", limit_usd=0.01, enabled=False
    )
    inside = _iso(budget_window_start("daily") + timedelta(seconds=1))
    await _spend(db, cost=5.0, created_at=inside, agent_id="a1")
    assert b["enabled"] is False

    calls: list[str] = []
    manager._run_backend = _record_backend(calls)
    events = [e async for e in manager.send_message("s1", "hi")]
    assert not any(e["type"] == "error" for e in events)
    assert calls == ["hi"]


# --------------------------------------------------------------------------- #
# REST API
# --------------------------------------------------------------------------- #


@pytest.fixture
async def client(db):
    session_manager.sessions.clear()
    await session_manager.initialize(db)
    budgets_mod._db = db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_budgets_require_auth(client):
    assert (await client.get("/api/budgets")).status_code in (401, 403)
    assert (await client.get("/api/budgets/status")).status_code in (401, 403)


async def test_budget_rest_crud_and_status(client, db):
    # Create a global daily budget.
    resp = await client.post(
        "/api/budgets",
        json={"scope": "global", "window": "daily", "limit_usd": 10.0},
        headers=HEADERS,
    )
    assert resp.status_code == 201, resp.text
    bid = resp.json()["id"]
    assert resp.json()["soft_pct"] == 0.8

    # List.
    listed = await client.get("/api/budgets", headers=HEADERS)
    assert [b["id"] for b in listed.json()] == [bid]

    # Status reflects live spend.
    start = budget_window_start("daily")
    await _spend(db, cost=3.0, created_at=_iso(start + timedelta(seconds=1)))
    status = await client.get("/api/budgets/status", headers=HEADERS)
    assert status.status_code == 200
    entry = status.json()[0]
    assert entry["scope"] == "global"
    assert entry["limit_usd"] == 10.0
    assert entry["spent_usd"] == pytest.approx(3.0)

    # Patch.
    patched = await client.patch(
        f"/api/budgets/{bid}", json={"limit_usd": 20.0}, headers=HEADERS
    )
    assert patched.json()["limit_usd"] == 20.0

    # Delete.
    assert (
        await client.delete(f"/api/budgets/{bid}", headers=HEADERS)
    ).status_code == 204
    assert (await client.get("/api/budgets", headers=HEADERS)).json() == []


async def test_budget_rest_validation_and_conflicts(client, db):
    # limit must be > 0.
    bad = await client.post(
        "/api/budgets",
        json={"scope": "global", "window": "daily", "limit_usd": 0},
        headers=HEADERS,
    )
    assert bad.status_code == 422

    # agent scope requires an agent_id...
    missing = await client.post(
        "/api/budgets",
        json={"scope": "agent", "window": "daily", "limit_usd": 1.0},
        headers=HEADERS,
    )
    assert missing.status_code == 422

    # ...and the agent must exist.
    ghost = await client.post(
        "/api/budgets",
        json={
            "scope": "agent",
            "agent_id": "nope",
            "window": "daily",
            "limit_usd": 1.0,
        },
        headers=HEADERS,
    )
    assert ghost.status_code == 404

    # Duplicate (scope, window) → 409.
    await client.post(
        "/api/budgets",
        json={"scope": "global", "window": "daily", "limit_usd": 1.0},
        headers=HEADERS,
    )
    dup = await client.post(
        "/api/budgets",
        json={"scope": "global", "window": "daily", "limit_usd": 2.0},
        headers=HEADERS,
    )
    assert dup.status_code == 409

    # Patch / delete of a missing id → 404.
    assert (
        await client.patch(
            "/api/budgets/missing", json={"limit_usd": 5.0}, headers=HEADERS
        )
    ).status_code == 404
    assert (
        await client.delete("/api/budgets/missing", headers=HEADERS)
    ).status_code == 404

    # Patching a budget's window onto one another budget already occupies
    # collides on the same uniqueness index as create → 409, not a 500.
    weekly = await client.post(
        "/api/budgets",
        json={"scope": "global", "window": "weekly", "limit_usd": 3.0},
        headers=HEADERS,
    )
    assert weekly.status_code == 201, weekly.text
    # The global daily from the duplicate check above still exists; moving the
    # weekly onto `daily` would make two global dailies.
    collide = await client.patch(
        f"/api/budgets/{weekly.json()['id']}",
        json={"window": "daily"},
        headers=HEADERS,
    )
    assert collide.status_code == 409, collide.text


async def test_budget_rest_agent_scope_happy_path(client, db):
    now = datetime.now(timezone.utc).isoformat()
    await db.save_agent(agent_id="a1", name="Alice", created_at=now, updated_at=now)
    resp = await client.post(
        "/api/budgets",
        json={
            "scope": "agent",
            "agent_id": "a1",
            "window": "monthly",
            "limit_usd": 50.0,
            "soft_pct": 0.5,
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["scope"] == "agent"
    assert body["agent_id"] == "a1"
    assert body["soft_pct"] == 0.5
