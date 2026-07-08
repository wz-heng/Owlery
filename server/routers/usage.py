"""Usage aggregation routes (usage-tracking.md §5).

Read-only window aggregation over the `turn_usage` ledger. The API
returns ids as grouping keys; the frontend resolves agent/session names
from state it already holds (sessions may be deleted — their usage rows
outlive them by design).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException

from ..auth import verify_token
from ..database import Database

router = APIRouter(prefix="/api/usage", tags=["usage"])

_db: Database | None = None


def _get_db() -> Database:
    assert _db is not None, "usage router not initialised"
    return _db


def _validate_iso(value: str | None, param: str) -> str | None:
    """Window bounds are compared as TEXT against ISO-8601 UTC created_at;
    reject anything that isn't itself valid ISO-8601 up front."""
    if value is None:
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=422, detail=f"{param} must be an ISO-8601 date/datetime"
        )
    return value


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
        since=_validate_iso(since, "since"),
        until=_validate_iso(until, "until"),
        agent_id=agent_id,
        session_id=session_id,
    )
