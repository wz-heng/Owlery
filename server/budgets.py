"""Budget domain logic (budget-model-routing.md §3).

A self-contained layer over the `budgets` table and the `turn_usage`
ledger. It answers one question — *is this session's next turn allowed to
run, and should we warn?* — without knowing anything about the harness,
routers, or session manager. The session manager owns the single
pre-run checkpoint (§3.2) and calls in here; the REST layer reuses
`budget_statuses` for `GET /api/budgets/status`.

Two deliberate scoping decisions carried from §3.1:

- **USD only, Claude only.** Spend is `SUM(turn_usage.cost)` over the
  window; Codex turns report `cost=NULL` and contribute 0. The gate is
  therefore only meaningful for Claude spend, so the session manager
  evaluates it exclusively for `claude-code` sessions — a free Codex
  turn can neither raise Claude spend nor be denied over a Claude
  budget. `origin='research'` rows are included: research burns real
  Claude money.
- **Natural-calendar windows in the server's local timezone**, weeks
  starting Monday. The boundary is computed here as an aware datetime,
  converted to UTC, and compared as an ISO-8601 TEXT lower bound against
  `turn_usage.created_at` (also UTC `datetime.isoformat()`). Both sides
  speak the exact same vocabulary, so the plain `>=` compare the usage
  aggregation already relies on stays correct — the trap the design
  flags (comparing the `T`-separated `created_at` against a
  space-separated `datetime('now')`) is avoided by never letting
  `datetime('now')` into the comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .database import Database

WINDOWS = ("daily", "weekly", "monthly")


def budget_window_start(window: str, now: datetime | None = None) -> datetime:
    """Aware UTC datetime at which the current `window` began.

    Boundaries follow the *server's local timezone* natural calendar
    (weeks start Monday), then convert to UTC so the returned value is
    directly comparable to `turn_usage.created_at`. `now` (aware) is an
    injection seam for tests; it defaults to the current instant.
    """
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local_date = base.astimezone().date()  # today in the server's local zone
    if window == "daily":
        day = local_date
    elif window == "weekly":
        day = local_date - timedelta(days=local_date.weekday())  # back to Monday
    elif window == "monthly":
        day = local_date.replace(day=1)
    else:
        raise ValueError(f"unknown window: {window!r}")
    # Build local midnight from the date, then let astimezone() attach the local
    # zone — this sidesteps DST arithmetic drift that subtracting timedeltas from
    # an aware datetime can introduce around a transition.
    local_midnight = datetime(day.year, day.month, day.day).astimezone()
    return local_midnight.astimezone(timezone.utc)


@dataclass(frozen=True)
class BudgetStatus:
    """One enabled budget resolved against live spend for its window."""

    budget_id: str
    scope: str  # 'global' | 'agent'
    agent_id: str | None
    window: str
    window_start: str  # UTC ISO-8601; also the soft-warn dedupe key
    limit_usd: float
    soft_pct: float
    spent_usd: float

    @property
    def is_hard(self) -> bool:
        return self.spent_usd >= self.limit_usd

    @property
    def is_soft(self) -> bool:
        return self.spent_usd >= self.limit_usd * self.soft_pct

    @property
    def overage_ratio(self) -> float:
        # limit_usd is > 0 by construction (schema CHECK + request validation).
        return self.spent_usd / self.limit_usd


class BudgetExceededError(Exception):
    """A hard budget threshold is crossed: the turn must fail fast before
    the harness runs. Carries the offending `BudgetStatus` so the session
    manager can emit a structured error event (§3.2)."""

    def __init__(self, status: BudgetStatus):
        self.status = status
        super().__init__(self.render())

    def render(self) -> str:
        s = self.status
        scope = "global" if s.scope == "global" else f"agent {s.agent_id}"
        return (
            f"Budget limit reached: {scope} {s.window} budget of "
            f"${s.limit_usd:.2f} is spent (${s.spent_usd:.4f} used this window). "
            f"This turn was blocked before running. Raise or disable the budget, "
            f"or route this work to a different agent, to continue."
        )


async def budget_statuses(
    db: "Database",
    *,
    only_agent_id: str | None = None,
    include_disabled: bool = False,
    now: datetime | None = None,
) -> list[BudgetStatus]:
    """Resolve every budget against its window's live spend.

    `only_agent_id` narrows to the budgets that *apply* to a session run
    by that agent — the global budgets plus that agent's own — which is
    exactly the gate's input. Left as None (the status endpoint's case)
    every budget is resolved, each against the correct spend scope
    (agent budgets see only their agent's spend, global budgets see all).
    """
    budgets = await db.list_budgets(enabled_only=not include_disabled)
    if only_agent_id is not None:
        budgets = [
            b
            for b in budgets
            if b["scope"] == "global" or b["agent_id"] == only_agent_id
        ]
    out: list[BudgetStatus] = []
    for b in budgets:
        start = budget_window_start(b["window"], now)
        spent = await db.budget_spent_usd(
            window_start=start.isoformat(),
            agent_id=b["agent_id"] if b["scope"] == "agent" else None,
        )
        out.append(
            BudgetStatus(
                budget_id=b["id"],
                scope=b["scope"],
                agent_id=b["agent_id"],
                window=b["window"],
                window_start=start.isoformat(),
                limit_usd=b["limit_usd"],
                soft_pct=b["soft_pct"],
                spent_usd=spent,
            )
        )
    return out


def classify_budget_statuses(
    statuses: list[BudgetStatus],
) -> tuple[BudgetStatus | None, list[BudgetStatus]]:
    """Split resolved statuses into the single hard blocker (if any) and
    the soft-threshold warnings.

    Multiple budgets can co-exist (global + per-agent, across windows);
    "whoever crosses first wins" (§3.1). When more than one is hard we
    surface the *tightest* — the largest overage ratio — since that is the
    most informative bound to show the user. A hard budget is never also
    reported as a soft warning.
    """
    hard: BudgetStatus | None = None
    soft: list[BudgetStatus] = []
    for s in statuses:
        if s.is_hard:
            if hard is None or s.overage_ratio > hard.overage_ratio:
                hard = s
        elif s.is_soft:
            soft.append(s)
    return hard, soft
