"""Usage aggregation routes (usage-tracking.md §5).

Read-only window aggregation over the `turn_usage` ledger. The API
returns ids as grouping keys; the frontend resolves agent/session names
from state it already holds (sessions may be deleted — their usage rows
outlive them by design).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException

from ..auth import verify_token
from ..database import Database

router = APIRouter(prefix="/api/usage", tags=["usage"])

_db: Database | None = None


def _get_db() -> Database:
    assert _db is not None, "usage router not initialised"
    return _db


def _normalize_bound(value: str | None, param: str) -> str | None:
    """Window bounds are compared as TEXT against `turn_usage.created_at`
    (UTC `datetime.isoformat()`), so anything reaching the DB must be in
    that same vocabulary. Two forms are accepted: a plain `YYYY-MM-DD`
    (a valid day boundary against the fixed layout) and a timezone-aware
    ISO-8601 datetime, converted to UTC here. Naive datetimes are
    rejected — guessing their zone would silently shift the window."""
    if value is None:
        return None
    try:
        if len(value) == 10:
            date.fromisoformat(value)
            return value
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"{param} must be YYYY-MM-DD or an ISO-8601 datetime",
        )
    if dt.tzinfo is None:
        raise HTTPException(
            status_code=422,
            detail=f"{param} must carry an explicit timezone (or be YYYY-MM-DD)",
        )
    return dt.astimezone(timezone.utc).isoformat()


@router.get("/summary")
async def usage_summary(
    group_by: Literal["agent", "session", "day", "backend"] = "agent",
    since: str | None = None,
    until: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
    _: str = Depends(verify_token),
) -> dict:
    return await _get_db().summarize_usage(
        group_by=group_by,
        since=_normalize_bound(since, "since"),
        until=_normalize_bound(until, "until"),
        agent_id=agent_id,
        session_id=session_id,
    )
