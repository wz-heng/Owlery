"""Usage-limit park & auto-resume (limit-auto-resume.md).

The classifier tests replay REAL captured CLI streams (tests/fixtures/, see the
README there) through the real parsers. That matters: both CLIs answer a
server-side throttle and the user's own limit with HTTP 429 and prose that
contains "rate limit" in both cases, so the whole design rests on the claim
that the structured discriminators separate them. These tests are that claim.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from server.database import Database
from server.harness import TurnFailure, get_harness
from server.parked_turns import ParkedTurnRunner

FIXTURES = Path(__file__).parent / "fixtures"


def _replay(backend: str, fixture: str) -> TurnFailure:
    """Feed a captured CLI stream through the REAL parser and build the
    TurnFailure a live failed turn would produce."""
    harness = get_harness(backend)
    parser = harness.profile.new_event_parser()
    error_text = ""
    for line in (FIXTURES / fixture).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        for event in parser.parse(json.loads(line)).events:
            if event.is_error:
                error_text += (event.content or "") + "\n"
                error_text += json.dumps(event.raw or {}) + "\n"
    return TurnFailure(
        error_text=error_text, rate_limit_info=parser.rate_limit_info
    )


# --------------------------------------------------------------- classifier

def test_claude_user_limit_is_classified_as_a_limit():
    """The real 5-hour limit: claude emits a `rate_limit_event` claiming the
    window (`rateLimitType`) with an epoch, and we park on it."""
    hit = get_harness("claude-code").classify_usage_limit(
        _replay("claude-code", "limit_claude_user_5h.jsonl")
    )
    assert hit is not None
    assert hit.kind == "five_hour"
    assert hit.reset_at == 1784038967  # the epoch the CLI reported, verbatim


def test_codex_user_limit_is_classified_as_a_limit():
    hit = get_harness("codex").classify_usage_limit(
        _replay("codex", "limit_codex_user.jsonl")
    )
    assert hit is not None
    assert hit.kind == "usage_limit"


@pytest.mark.parametrize(
    "backend,fixture",
    [
        ("claude-code", "limit_claude_transient_429.jsonl"),
        ("codex", "limit_codex_transient_429.jsonl"),
    ],
)
def test_server_side_429_is_not_a_usage_limit(backend, fixture):
    """THE bug this design exists to avoid: a server-side 429 is a transient
    blip to retry in seconds, not a limit to park on for hours. It carries the
    same status code and prose as the real limit — only the structure differs."""
    assert get_harness(backend).classify_usage_limit(_replay(backend, fixture)) is None


@pytest.mark.parametrize(
    "backend,fixture,expect_limit,expect_transient",
    [
        ("claude-code", "limit_claude_user_5h.jsonl", True, False),
        ("claude-code", "limit_claude_transient_429.jsonl", False, True),
        ("codex", "limit_codex_user.jsonl", True, False),
        ("codex", "limit_codex_transient_429.jsonl", False, False),
    ],
)
def test_dispositions_are_mutually_exclusive(
    backend, fixture, expect_limit, expect_transient
):
    """The four dispositions must never double-claim a turn: a limit is never
    also transient, and neither is ever mistaken for an auth rejection."""
    harness = get_harness(backend)
    failure = _replay(backend, fixture)
    is_limit = harness.classify_usage_limit(failure) is not None
    is_transient = harness.is_transient_error(failure.error_text)
    is_auth = harness.is_auth_error(failure.error_text)

    assert is_limit is expect_limit
    assert is_transient is expect_transient
    assert not is_auth
    assert not (is_limit and is_transient)  # the disjointness that matters


def test_claude_rejected_without_a_window_claim_is_not_a_limit():
    """A `rejected` rate_limit_event with no `rateLimitType` is the server-side
    throttle's shape, not the user's limit — it must fall through to transient."""
    harness = get_harness("claude-code")
    failure = TurnFailure(
        error_text="API Error: Request rejected (429)",
        rate_limit_info={"status": "rejected", "isUsingOverage": False},
    )
    assert harness.classify_usage_limit(failure) is None


def test_claude_approaching_limit_warning_does_not_park():
    """`allowed_warning` means "you're close" — the turn SUCCEEDED. Parking on
    it would strand a session that never failed."""
    harness = get_harness("claude-code")
    failure = TurnFailure(
        error_text="",
        rate_limit_info={"status": "allowed_warning", "rateLimitType": "five_hour"},
    )
    assert harness.classify_usage_limit(failure) is None


def test_claude_limit_without_an_epoch_yields_no_reset():
    """No epoch reported → the hit still classifies (so we park), but with no
    reset time, which sends the caller to probe mode rather than guessing."""
    harness = get_harness("claude-code")
    hit = harness.classify_usage_limit(
        TurnFailure(
            error_text="You've hit your session limit",
            rate_limit_info={"status": "rejected", "rateLimitType": "five_hour"},
        )
    )
    assert hit is not None and hit.reset_at is None


def _write_codex_rollout(tmp_path, *, resets_at: int) -> None:
    """A minimal codex rollout carrying the machine-readable reset epoch in its
    `token_count.rate_limits`, where the epoch actually lives (never on stdout)."""
    sessions = tmp_path / "sessions" / "2026" / "07" / "14"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-2026-07-14T19-24-42-thread-1.jsonl"
    rollout.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "thread-1"}}) + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {
                            "used_percent": 100.0,
                            "window_minutes": 300,
                            "resets_at": resets_at,
                        }
                    },
                },
            }
        )
        + "\n"
    )


def test_codex_classify_is_pure_and_the_lookup_fills_the_epoch(tmp_path):
    """The two-hook split (limit-auto-resume.md §4): `classify_usage_limit` is
    PURE and stream-only — it decides the verdict and carries NO epoch even when
    a rollout with one sits right there on disk (codex puts none on stdout). The
    separate I/O hook `resolve_usage_limit_reset` reads the machine-readable
    epoch from the rollout. Codex renders its reset as a localized string ("try
    again at 10:23 PM") but we read the epoch, never parse the prose back."""
    _write_codex_rollout(tmp_path, resets_at=1784039851)
    harness = get_harness("codex")
    failure = TurnFailure(
        error_text=(
            "You've hit your usage limit. Upgrade to Pro ... or try again "
            "at 10:23 PM."
        ),
        resume_id="thread-1",
        home_dir=str(tmp_path),
    )

    hit = harness.classify_usage_limit(failure)
    assert hit is not None
    # Pure: the classifier did NOT touch the rollout, so the epoch is still
    # unfilled even though it exists on disk.
    assert hit.reset_at is None

    resolved = harness.resolve_usage_limit_reset(hit, failure)
    assert resolved.reset_at == 1784039851  # the I/O lookup read it from the rollout


def test_resolve_is_a_noop_when_the_stream_already_carried_the_epoch():
    """Claude's epoch rides the stream, so `classify_usage_limit` already has it
    and the lookup must not run: claude has no lookup hook, and a hit that
    already carries a reset is returned untouched, never re-judged."""
    harness = get_harness("claude-code")
    hit = harness.classify_usage_limit(
        _replay("claude-code", "limit_claude_user_5h.jsonl")
    )
    assert hit.reset_at == 1784038967
    assert harness.resolve_usage_limit_reset(hit, TurnFailure()) is hit


def test_codex_resolve_degrades_when_the_rollout_is_absent(tmp_path):
    """No rollout on disk → the I/O lookup finds no epoch and the hit keeps its
    missing reset, sending the caller to the probe fallback rather than guessing
    a reset time. A failed lookup degrades; it never breaks the park."""
    harness = get_harness("codex")
    failure = TurnFailure(
        error_text="You've hit your usage limit.",
        resume_id="thread-missing",
        home_dir=str(tmp_path),
    )
    hit = harness.classify_usage_limit(failure)
    assert hit is not None and hit.reset_at is None
    assert harness.resolve_usage_limit_reset(hit, failure).reset_at is None


# ------------------------------------------------------------------ parking

class _FakeSessionManager:
    """Captures what the runner would resume, without spawning anything."""

    def __init__(self) -> None:
        self.resumed: list[dict] = []

    async def resume_parked_turn(self, row: dict) -> None:
        self.resumed.append(row)


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    # parked_turns has an FK to sessions — give it a row to hang off.
    await database.save_session(
        "s1", "s1", str(tmp_path), datetime.now(timezone.utc).isoformat()
    )
    yield database
    await database.close()


async def test_park_persists_and_schedules(db):
    runner = ParkedTurnRunner(_FakeSessionManager(), db)
    reset = datetime.now(timezone.utc) + timedelta(hours=3)
    row = await runner.park(
        "s1",
        resume_mode="prompt",
        payload="do the thing",
        resume_at_turn_start="resume-1",
        limit_kind="five_hour",
        reset_at=reset,
    )
    assert row["resume_mode"] == "prompt"
    stored = await db.get_parked_turn("s1")
    assert stored is not None
    assert stored["payload"] == "do the thing"
    assert stored["limit_kind"] == "five_hour"
    # The wake-up lands after the reset, never before it — racing the window
    # open just earns another 429.
    assert datetime.fromisoformat(stored["wake_at"]) > reset


async def test_park_survives_a_restart(db):
    """The whole point is a multi-hour wait, so a restart must not drop it: the
    DB rows are the truth and boot rebuilds the jobs from them."""
    first = ParkedTurnRunner(_FakeSessionManager(), db)
    await first.park(
        "s1",
        resume_mode="continue",
        payload="continue",
        resume_at_turn_start="resume-1",
        limit_kind="five_hour",
        reset_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )

    # A brand-new process: nothing in memory, only the DB.
    reborn = ParkedTurnRunner(_FakeSessionManager(), db)
    await reborn.initialize()
    assert reborn._scheduler.get_job("parked-turn:s1") is not None


async def test_a_wake_up_missed_while_down_fires_immediately(db):
    """The server was off through the reset. The work must still resume — a
    missed wake-up is due NOW, not dropped."""
    now = datetime.now(timezone.utc)
    await db.upsert_parked_turn(
        {
            "session_id": "s1",
            "resume_mode": "prompt",
            "payload": "p",
            "resume_at_turn_start": None,
            "limit_kind": "five_hour",
            "reset_at": (now - timedelta(hours=5)).isoformat(),
            "wake_at": (now - timedelta(hours=5)).isoformat(),  # long past
            "attempts": 1,
            "probes": 0,
            "created_at": (now - timedelta(hours=6)).isoformat(),
        }
    )
    runner = ParkedTurnRunner(_FakeSessionManager(), db)
    await runner.initialize()
    job = runner._scheduler.get_job("parked-turn:s1")
    assert job is not None
    assert job.trigger.run_date <= now + timedelta(seconds=5)


async def test_sessions_sharing_a_reset_are_staggered(db, tmp_path):
    """Several sessions parked on the SAME reset must not stampede the fresh
    window the instant it opens."""
    for sid in ("s2", "s3"):
        await db.save_session(
            sid, sid, str(tmp_path), datetime.now(timezone.utc).isoformat()
        )
    runner = ParkedTurnRunner(_FakeSessionManager(), db)
    reset = datetime.now(timezone.utc) + timedelta(hours=3)
    wakes = []
    for sid in ("s1", "s2", "s3"):
        row = await runner.park(
            sid,
            resume_mode="prompt",
            payload="p",
            resume_at_turn_start=None,
            limit_kind="five_hour",
            reset_at=reset,  # identical reset for all three
        )
        wakes.append(datetime.fromisoformat(row["wake_at"]))

    wakes.sort()
    for earlier, later in zip(wakes, wakes[1:]):
        assert (later - earlier).total_seconds() >= 55


async def test_wake_up_consumes_the_record_and_resumes(db):
    """The record is consumed BEFORE the turn runs: a resumed turn that limits
    again re-parks itself, and a stale row would rebuild into a duplicate job."""
    mgr = _FakeSessionManager()
    runner = ParkedTurnRunner(mgr, db)
    await runner.park(
        "s1",
        resume_mode="continue",
        payload="continue",
        resume_at_turn_start="resume-1",
        limit_kind="five_hour",
        reset_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    await runner._wake("s1")

    assert await db.get_parked_turn("s1") is None  # consumed
    assert len(mgr.resumed) == 1
    assert mgr.resumed[0]["resume_mode"] == "continue"


async def test_cancel_drops_the_record_and_the_job(db):
    runner = ParkedTurnRunner(_FakeSessionManager(), db)
    await runner.initialize()
    await runner.park(
        "s1",
        resume_mode="prompt",
        payload="p",
        resume_at_turn_start=None,
        limit_kind="five_hour",
        reset_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert await runner.cancel("s1") is True
    assert await db.get_parked_turn("s1") is None
    assert runner._scheduler.get_job("parked-turn:s1") is None
    # A cancelled park is gone — the wake-up must be a no-op, not a resurrection.
    assert await runner.cancel("s1") is False


async def test_wake_up_of_a_cancelled_park_does_nothing(db):
    """The job can fire in the window between the user cancelling and the
    coroutine running. It must not resume a turn the user called off."""
    mgr = _FakeSessionManager()
    runner = ParkedTurnRunner(mgr, db)
    await runner.park(
        "s1",
        resume_mode="prompt",
        payload="p",
        resume_at_turn_start=None,
        limit_kind="five_hour",
        reset_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    await db.delete_parked_turn("s1")  # user cancelled

    await runner._wake("s1")

    assert mgr.resumed == []


async def test_probe_mode_when_no_epoch_is_reported(db):
    """No reset epoch → knock again on a fixed interval instead of guessing."""
    runner = ParkedTurnRunner(_FakeSessionManager(), db)
    before = datetime.now(timezone.utc)
    row = await runner.park(
        "s1",
        resume_mode="prompt",
        payload="p",
        resume_at_turn_start=None,
        limit_kind="five_hour",
        reset_at=None,  # the CLI told us nothing
        probes=1,
    )
    wake = datetime.fromisoformat(row["wake_at"])
    assert timedelta(minutes=29) < (wake - before) < timedelta(minutes=31)
    assert row["reset_at"] is None


# ------------------------------------------- re-park bounding & queue holding

@pytest.fixture
async def manager(tmp_path):
    from server.session_manager import SessionManager

    mgr = SessionManager()
    database = Database(":memory:")
    await database.initialize()
    await mgr.initialize(database)
    await database.save_session(
        "s1", "s1", str(tmp_path), datetime.now(timezone.utc).isoformat()
    )
    await mgr.initialize(database)  # reload so the session is live in memory
    mgr.set_parked_turn_runner(ParkedTurnRunner(mgr, database))
    try:
        yield mgr
    finally:
        await database.close()


class _Hit:
    def __init__(self, reset_at=None, kind="five_hour"):
        self.reset_at = reset_at
        self.kind = kind


async def _park(manager, *, reset_at, produced_output=False):
    return await manager._park_limited_turn(
        manager.sessions["s1"],
        hit=_Hit(reset_at=reset_at),
        prompt="original prompt",
        resume_at_turn_start="resume-0",
        produced_output=produced_output,
    )


async def test_repark_is_bounded(manager):
    """A wake-up that limits again re-parks — but not forever. After the cap we
    surface a terminal error instead of parking into eternity."""
    epoch = int((datetime.now(timezone.utc) + timedelta(hours=3)).timestamp())

    for expected in (1, 2, 3):
        event, parked = await _park(manager, reset_at=epoch)
        assert parked is True, f"park {expected} should have been scheduled"
        assert event["code"] == "limit_paused"

    # 4th consecutive limit failure with no progress → give up, clearly.
    event, parked = await _park(manager, reset_at=epoch)
    assert parked is False
    assert event["code"] == "limit_exhausted"
    # And the park record is gone, so a reboot can't revive it.
    assert await manager._parked_turns.get("s1") is None


async def test_probe_mode_is_bounded(manager):
    """No epoch → probe every 30 min, but bounded well past one 5-hour window."""
    for _ in range(12):
        event, parked = await _park(manager, reset_at=None)
        assert parked is True
    event, parked = await _park(manager, reset_at=None)
    assert parked is False
    assert event["code"] == "limit_exhausted"


async def test_park_records_the_two_resume_modes(manager):
    """The resume mode is the transient-retry recovery, reused verbatim: nothing
    streamed → re-run the original prompt; output streamed → "continue"."""
    epoch = int((datetime.now(timezone.utc) + timedelta(hours=3)).timestamp())

    await _park(manager, reset_at=epoch, produced_output=False)
    row = await manager._parked_turns.get("s1")
    assert row["resume_mode"] == "prompt"
    assert row["payload"] == "original prompt"
    assert row["resume_at_turn_start"] == "resume-0"

    manager.sessions["s1"].claude_session_id = "mid-turn-id"
    await _park(manager, reset_at=epoch, produced_output=True)
    row = await manager._parked_turns.get("s1")
    assert row["resume_mode"] == "continue"
    assert row["payload"] == "continue"


async def test_output_without_a_resume_id_is_not_parked(manager):
    """A turn that streamed output but captured no resume id can't be continued
    safely and must not be re-run (it would duplicate work) — surface as-is."""
    manager.sessions["s1"].claude_session_id = None
    event, parked = await _park(
        manager,
        reset_at=int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        produced_output=True,
    )
    assert parked is False
    assert event is None  # nothing to say — the raw limit error surfaces


async def test_a_parked_session_holds_its_queue(manager):
    """Queued prompts must NOT fire into an exhausted window. They stay queued
    and drain after the resume."""
    from server.session_manager import QueuedPrompt, UsageLimitParked

    session = manager.sessions["s1"]
    session._pending_queue.append(
        QueuedPrompt(prompt="queued one", attachment_ids=[])
    )

    async def _boom(session_id, queued):
        raise UsageLimitParked(session_id)

    manager._consume_message = _boom
    await manager._drive_messages(
        "s1", QueuedPrompt(prompt="first", attachment_ids=[])
    )

    # The drive stopped at the park; the queued prompt is still waiting.
    assert [q.prompt for q in session._pending_queue] == ["queued one"]


async def test_cancelling_a_park_releases_the_held_queue(manager):
    """Cancelling the auto-resume abandons the parked turn — but the prompts
    that piled up behind it were HELD, not dropped, so they must now drain
    rather than sit stranded until the user types again."""
    from server.session_manager import QueuedPrompt

    session = manager.sessions["s1"]
    session._pending_queue.append(
        QueuedPrompt(prompt="held one", attachment_ids=[])
    )

    drove: list[str] = []

    async def _capture(session_id, queued):
        drove.append(queued.prompt)

    manager._consume_message = _capture
    await manager.release_parked_queue("s1")
    if session._active_task:
        await session._active_task

    assert drove == ["held one"]
    assert session._pending_queue == []


async def test_resume_rewinds_the_session_id_in_prompt_mode(manager):
    """No output had streamed, so the resume re-runs the ORIGINAL prompt — and
    must discard the id the limit-failed attempt captured, or it would resume
    into a half-born conversation (the transient-retry rule, reused)."""
    session = manager.sessions["s1"]
    session.claude_session_id = "id-from-the-failed-attempt"

    started: list[str] = []

    async def _start(session_id, prompt):
        started.append(prompt)

    manager.start_message = _start
    await manager.resume_parked_turn(
        {
            "session_id": "s1",
            "resume_mode": "prompt",
            "payload": "the original prompt",
            "resume_at_turn_start": "id-at-turn-start",
            "attempts": 1,
        }
    )

    assert started == ["the original prompt"]
    assert session.claude_session_id == "id-at-turn-start"


async def test_resume_is_skipped_if_the_user_came_back_first(manager):
    """The user returned and started a turn while we were parked. The wake-up
    must not double-drive the session on top of their turn."""
    import asyncio

    session = manager.sessions["s1"]
    session._active_task = asyncio.create_task(asyncio.sleep(5))

    started: list[str] = []

    async def _start(session_id, prompt):
        started.append(prompt)

    manager.start_message = _start
    try:
        await manager.resume_parked_turn(
            {
                "session_id": "s1",
                "resume_mode": "prompt",
                "payload": "p",
                "resume_at_turn_start": None,
                "attempts": 1,
            }
        )
        assert started == []
    finally:
        session._active_task.cancel()


async def test_the_park_signal_survives_send_message(manager):
    """Regression: `send_message` wraps the run loop in `except Exception`, which
    would swallow the park signal — logging a bogus "Backend error" and letting
    the queue drain straight into the exhausted window. The signal must reach
    `_drive_messages` intact."""
    from server.session_manager import UsageLimitParked

    async def _parks(session, prompt):
        raise UsageLimitParked(session.id)
        yield  # pragma: no cover — makes this an async generator

    manager._run_backend = _parks

    with pytest.raises(UsageLimitParked):
        async for _ in manager.send_message("s1", "do it"):
            pass

    # And the session lock was still released, so the resumed turn can run.
    assert not manager.sessions["s1"]._lock.locked()


async def test_repark_counts_survive_the_wake_up(manager):
    """Regression: the wake-up DELETES the park row before re-running the turn.
    If the re-park then reads its attempt count from that (now absent) row it
    resets to zero, the cap never fires, and a permanently-exhausted window
    re-parks forever. The count must survive the wake."""
    epoch = int((datetime.now(timezone.utc) + timedelta(hours=3)).timestamp())
    runner = manager._parked_turns

    async def _limit_again(session_id, prompt):
        """The resumed turn hits the limit again — the whole point of the cap."""
        await _park(manager, reset_at=epoch)

    manager.start_message = _limit_again

    # First park, then let it wake and re-limit, repeatedly. Each wake consumes
    # the row; each re-park must count as the NEXT attempt.
    _event, parked = await _park(manager, reset_at=epoch)
    assert parked is True

    for _ in range(5):
        row = await runner.get("s1")
        if row is None:
            break
        await runner._wake("s1")

    # It must have GIVEN UP rather than parking forever.
    assert await runner.get("s1") is None
    assert manager.sessions["s1"]._limit_attempts["attempts"] >= 3


async def test_a_successful_turn_clears_the_park_count(manager):
    """Two parks today must not count against an unrelated limit next week.

    Drives the REAL run loop (a fake backend that streams a clean result), so
    this asserts on production behaviour rather than restating it.
    """
    from server.harness import HarnessEvent

    session = manager.sessions["s1"]
    session._limit_attempts = {"attempts": 2, "probes": 0}

    class _CleanRun:
        stderr_text = ""
        rate_limit_info = None

        async def start(self, *a, **k):
            return None

        async def stream(self):
            yield HarnessEvent(type="text", content="done")
            yield HarnessEvent(type="result", is_error=False, session_id="sid")

        async def stop(self):
            return None

    manager._make_run = lambda *a, **k: _CleanRun()

    async for _ in manager._run_backend(session, "hello"):
        pass

    assert session._limit_attempts is None
