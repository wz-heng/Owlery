"""Usage-limit park & auto-resume (limit-auto-resume.md).

The fourth disposition for a failed turn. A turn that dies on the USER'S OWN
usage limit is not an error to surface and not a transient blip to retry in
seconds — the work was going to continue anyway, and the only missing piece is
"wait until the window resets, then press go". So we park it: persist how to
resume, schedule a wake-up at the reset, and run it unattended.

Everything here is turn-level, so it covers every caller of
`session_manager._run_backend` uniformly — chat, schedules, delegation
children, the telegram bridge.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

if TYPE_CHECKING:
    from server.database import Database
    from server.session_manager import SessionManager

logger = logging.getLogger(__name__)

# A wake-up lands slightly AFTER the reported reset: the epoch is the instant
# the window reopens, and racing it to the millisecond just earns another 429.
RESET_GRACE = timedelta(seconds=30)

# Several sessions parked on the same reset must not stampede the fresh window
# the moment it opens — space them out, first parked first served.
STAGGER = timedelta(seconds=60)

# No reset epoch was reported (a CLI changed its stream shape): fall back to
# knocking periodically. A probe that limit-fails costs one spawn and ~no
# tokens. Bounded well past one 5-hour window.
PROBE_INTERVAL = timedelta(minutes=30)
MAX_PROBES = 12

# A wake-up that immediately limit-fails re-parks — but the fresh window may
# already be eaten by whatever resumed first, so bound consecutive parks that
# make no progress.
MAX_PARK_ATTEMPTS = 3


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class ParkedTurnRunner:
    """Owns the pending-resume records and their APScheduler wake-up jobs.

    `now` is injectable so tests can drive the whole park → wake → resume cycle
    in test time instead of waiting out a real 5-hour window.
    """

    def __init__(
        self,
        session_mgr: SessionManager,
        db: Database,
        *,
        scheduler: AsyncIOScheduler | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_mgr = session_mgr
        self._db = db
        self._scheduler = scheduler or AsyncIOScheduler()
        self._owns_scheduler = scheduler is None
        self._now = now or (lambda: datetime.now(timezone.utc))

    # ---------------------------------------------------------------- lifecycle

    async def initialize(self) -> None:
        """Rebuild wake-up jobs from the DB. A park is a multi-hour wait, so it
        MUST survive a restart — the records are the source of truth and the
        jobs are just derived state that a process death destroys.
        """
        for row in await self._db.list_parked_turns():
            self._add_job(row)
        if self._owns_scheduler and not self._scheduler.running:
            self._scheduler.start()

    async def shutdown(self) -> None:
        if self._owns_scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def _job_id(self, session_id: str) -> str:
        return f"parked-turn:{session_id}"

    def _add_job(self, row: dict[str, Any]) -> None:
        wake_at = _parse_iso(row["wake_at"])
        if wake_at is None:
            logger.warning(
                "Parked turn %s has an unparseable wake_at (%r) — waking now",
                row["session_id"], row["wake_at"],
            )
            wake_at = self._now()
        # A wake-up whose time already passed (the server was down through the
        # reset) is due NOW, not dropped: the whole point is that the work
        # resumes unattended even if nobody was watching.
        run_date = max(wake_at, self._now() + timedelta(seconds=1))
        self._scheduler.add_job(
            self._wake,
            DateTrigger(run_date=run_date),
            id=self._job_id(row["session_id"]),
            args=[row["session_id"]],
            replace_existing=True,
        )

    # ---------------------------------------------------------------- parking

    async def park(
        self,
        session_id: str,
        *,
        resume_mode: str,
        payload: str,
        resume_at_turn_start: str | None,
        limit_kind: str | None,
        reset_at: datetime | None,
        attempts: int = 0,
        probes: int = 0,
    ) -> dict[str, Any]:
        """Persist a park and schedule its wake-up. Returns the stored row."""
        now = self._now()
        if reset_at is not None:
            wake_at = await self._staggered(reset_at + RESET_GRACE)
        else:
            wake_at = now + PROBE_INTERVAL
        row = {
            "session_id": session_id,
            "resume_mode": resume_mode,
            "payload": payload,
            "resume_at_turn_start": resume_at_turn_start,
            "limit_kind": limit_kind,
            "reset_at": _iso(reset_at) if reset_at else None,
            "wake_at": _iso(wake_at),
            "attempts": attempts,
            "probes": probes,
            "created_at": _iso(now),
        }
        await self._db.upsert_parked_turn(row)
        self._add_job(row)
        logger.info(
            "Session %s parked on usage limit (%s); resuming at %s (mode=%s)",
            session_id, limit_kind or "unknown", row["wake_at"], resume_mode,
        )
        return row

    async def _staggered(self, target: datetime) -> datetime:
        """Space this wake-up clear of every park already claiming the same
        moment, so a shared reset doesn't fire N sessions at once."""
        taken = {
            _parse_iso(r["wake_at"])
            for r in await self._db.list_parked_turns()
        }
        taken.discard(None)
        wake = target
        while any(
            t is not None and abs((wake - t).total_seconds()) < STAGGER.total_seconds()
            for t in taken
        ):
            wake = wake + STAGGER
        return wake

    async def cancel(self, session_id: str) -> bool:
        """Drop a pending park — the user cancelled it, or it resumed. Removes
        both the record and the job."""
        try:
            self._scheduler.remove_job(self._job_id(session_id))
        except Exception:  # JobLookupError — already fired or never scheduled
            pass
        return await self._db.delete_parked_turn(session_id)

    async def get(self, session_id: str) -> dict[str, Any] | None:
        return await self._db.get_parked_turn(session_id)

    # ---------------------------------------------------------------- wake-up

    async def _wake(self, session_id: str) -> None:
        """The reset arrived (or a probe is due) — resume the parked turn.

        The record is consumed BEFORE the turn runs: if the resumed turn hits
        the limit again it re-parks itself with a fresh reset and a bumped
        attempt count, and we must not leave a stale row behind that a boot
        would rebuild into a duplicate job.
        """
        row = await self._db.get_parked_turn(session_id)
        if row is None:
            return  # cancelled between the job firing and this coroutine running
        await self._db.delete_parked_turn(session_id)
        logger.info(
            "Session %s: usage limit reset — auto-resuming (mode=%s, attempt %d)",
            session_id, row["resume_mode"], row["attempts"] + 1,
        )
        try:
            await self._session_mgr.resume_parked_turn(row)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Session %s: auto-resume after usage limit failed", session_id
            )
