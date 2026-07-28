"""Budget CRUD + status routes (budget-model-routing.md §3.3).

CRUD over the `budgets` table plus a read-only `GET /status` that resolves
each enabled budget against live spend for its window. The gate itself
lives in the session manager (§3.2); this router is only configuration and
observation.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from ..auth import verify_token
from ..budgets import budget_statuses
from ..database import Database
from ..models import (
    BudgetRead,
    BudgetStatusEntry,
    CreateBudgetRequest,
    UpdateBudgetRequest,
)

router = APIRouter(prefix="/api/budgets", tags=["budgets"])

_db: Database | None = None


def _get_db() -> Database:
    assert _db is not None, "budgets router not initialised"
    return _db


@router.get("", response_model=list[BudgetRead])
async def list_budgets(_: str = Depends(verify_token)) -> list[BudgetRead]:
    rows = await _get_db().list_budgets()
    return [BudgetRead(**r) for r in rows]


@router.get("/status", response_model=list[BudgetStatusEntry])
async def budget_status(_: str = Depends(verify_token)) -> list[BudgetStatusEntry]:
    statuses = await budget_statuses(_get_db())
    return [
        BudgetStatusEntry(
            scope=s.scope,
            agent_id=s.agent_id,
            window=s.window,
            limit_usd=s.limit_usd,
            spent_usd=s.spent_usd,
        )
        for s in statuses
    ]


@router.post("", response_model=BudgetRead, status_code=201)
async def create_budget(
    req: CreateBudgetRequest, _: str = Depends(verify_token)
) -> BudgetRead:
    db = _get_db()
    if req.scope == "agent":
        assert req.agent_id is not None  # guaranteed by request validation
        if await db.get_agent(req.agent_id) is None:
            raise HTTPException(status_code=404, detail="agent not found")
    try:
        row = await db.create_budget(
            scope=req.scope,
            agent_id=req.agent_id,
            window=req.window,
            limit_usd=req.limit_usd,
            soft_pct=req.soft_pct,
            enabled=req.enabled,
        )
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail=(
                f"a {req.scope} budget for the {req.window} window already "
                f"exists"
            ),
        )
    return BudgetRead(**row)


@router.patch("/{budget_id}", response_model=BudgetRead)
async def update_budget(
    budget_id: str,
    req: UpdateBudgetRequest,
    _: str = Depends(verify_token),
) -> BudgetRead:
    db = _get_db()
    if await db.get_budget(budget_id) is None:
        raise HTTPException(status_code=404, detail="budget not found")
    row = await db.update_budget(
        budget_id,
        window=req.window,
        limit_usd=req.limit_usd,
        soft_pct=req.soft_pct,
        enabled=req.enabled,
    )
    assert row is not None
    return BudgetRead(**row)


@router.delete("/{budget_id}", status_code=204)
async def delete_budget(
    budget_id: str, _: str = Depends(verify_token)
) -> None:
    if not await _get_db().delete_budget(budget_id):
        raise HTTPException(status_code=404, detail="budget not found")
