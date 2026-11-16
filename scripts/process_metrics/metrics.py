"""Pure metric computations for the weekly process-metrics report
(docs/plans/process-metrics.md §3.1). Every function here takes an already-
open, read-only `sqlite3.Connection` onto a SNAPSHOT of the production DB
(never the live file — see `report.py`) and returns a plain dict; nothing
here writes anything, calls a model, or knows about markdown rendering.

Each metric's SQL is this report's one and only definition of it — the
exact query is quoted in the rendered report (`report.py`) so a reader can
verify the number instead of trusting prose. Methodology is pinned here,
in code, not in a separate doc that can drift from what actually ran
(docs/plans/process-metrics.md §3.1: "口径一旦首报即冻结;改口径=新版本号
并在报告中并列旧口径,禁止静默改算法").

Every report run recomputes every metric over the FULL table history (no
persisted incremental state, no history file to corrupt) — the weekly
"trend" is produced by re-slicing that same full history into ISO-week
buckets fresh each time, exactly how `multiagent-field-report-data.md`
(§2.1/§2.4, private `owlery-planning` repo) did its own weekly tables. This
makes every report run fully reproducible from the snapshot alone.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

METRICS_VERSION = 1

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _iso_week(ts: str) -> str:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# §3.1 一次通过率 — completed 且 attempt=1 且无 blocked 史的任务占比
# ---------------------------------------------------------------------------
#
# Denominator: tasks that have reached `completed` at least once (through
# any attempt) — a task that has never completed can't be judged pass/fail
# yet, so it's excluded from the universe rather than counted as a miss.
# Numerator: of those, the ones whose *first* attempt was the one that
# completed, AND that never had a run reach `blocked` on ANY attempt — a
# task that needed a human to unblock it before eventually completing on
# attempt 1 (possible if blocked happened on a run that was itself later
# superseded) still isn't a clean one-pass story.

ONE_PASS_RATE_SQL = """
WITH completed_tasks AS (
    SELECT DISTINCT task_id FROM task_runs WHERE state = 'completed'
),
blocked_tasks AS (
    SELECT DISTINCT task_id FROM task_runs WHERE state = 'blocked'
),
first_attempt_completed AS (
    SELECT DISTINCT task_id FROM task_runs
    WHERE state = 'completed' AND attempt_no = 1
)
SELECT
    (SELECT COUNT(*) FROM completed_tasks) AS denominator,
    (
        SELECT COUNT(*) FROM first_attempt_completed fac
        WHERE fac.task_id NOT IN (SELECT task_id FROM blocked_tasks)
    ) AS numerator
"""


def one_pass_rate(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(ONE_PASS_RATE_SQL).fetchone()
    denominator, numerator = row["denominator"], row["numerator"]
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": (numerator / denominator) if denominator else None,
        "sql": ONE_PASS_RATE_SQL,
    }


# ---------------------------------------------------------------------------
# §3.1 重试率 — run 级口径,沿田野报告基线(37.9% = 25/66)
# ---------------------------------------------------------------------------

RETRY_RATE_SQL = """
SELECT
    COUNT(*) AS denominator,
    SUM(CASE WHEN attempt_no > 1 THEN 1 ELSE 0 END) AS numerator
FROM task_runs WHERE state = 'completed'
"""


def retry_rate(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(RETRY_RATE_SQL).fetchone()
    denominator, numerator = row["denominator"], row["numerator"] or 0
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": (numerator / denominator) if denominator else None,
        "sql": RETRY_RATE_SQL,
    }


# ---------------------------------------------------------------------------
# §3.1 人工接管率 — among the retried completed runs, did a human have to
# unblock the task before this attempt could be claimed (沿田野报告基线 56%)
# ---------------------------------------------------------------------------


HUMAN_TAKEOVER_SQL = (
    "per-row: does a task_events row with kind='task_unblocked' AND "
    "actor_kind='user' exist at or before task_runs.claimed_at (most recent "
    "one if several), for every completed run with attempt_no>1"
)


def human_takeover_rate(conn: sqlite3.Connection) -> dict[str, Any]:
    retried = _rows(
        conn,
        "SELECT id, task_id, claimed_at FROM task_runs "
        "WHERE state = 'completed' AND attempt_no > 1",
    )
    numerator = 0
    for run in retried:
        # Earlier draft looked at "whatever task_events row is most recent
        # before claimed_at" and trusted its actor_kind — that misclassifies
        # an ordinary user COMMENT left after a SYSTEM-driven retry as a
        # takeover, and conversely can miss a real human unblock if some
        # later system event (e.g. a subsequent automatic retry's own
        # bookkeeping) landed between the unblock and this claim (Snape
        # review). The actual signal is narrower and unambiguous: did a
        # `task_unblocked` event — the one `TaskRepository.unblock()` stamps
        # (server/task_board/repository.py) — attributed to a human precede
        # this claim. Any other event kind, including comments, is
        # irrelevant to whether a human had to intervene.
        preceding = conn.execute(
            "SELECT actor_kind FROM task_events "
            "WHERE task_id = ? AND created_at <= ? AND kind = 'task_unblocked' "
            "ORDER BY created_at DESC, seq DESC LIMIT 1",
            (run["task_id"], run["claimed_at"]),
        ).fetchone()
        if preceding is not None and preceding["actor_kind"] == "user":
            numerator += 1
    denominator = len(retried)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": (numerator / denominator) if denominator else None,
        "sql": HUMAN_TAKEOVER_SQL,
    }


# ---------------------------------------------------------------------------
# §3.1 turn 死因分布 — harness_exits.reason 六分类占比
# ---------------------------------------------------------------------------

TURN_DEATH_SQL = "SELECT reason, COUNT(*) AS n FROM harness_exits GROUP BY reason"


def turn_death_distribution(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = _rows(conn, TURN_DEATH_SQL)
    counts = {r["reason"]: r["n"] for r in rows}
    total = sum(counts.values())
    return {
        "counts": counts,
        "total": total,
        "rates": {k: v / total for k, v in counts.items()} if total else {},
        "sql": TURN_DEATH_SQL,
    }


def turn_death_distribution_by_week(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    rows = _rows(conn, "SELECT reason, created_at FROM harness_exits")
    by_week: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        by_week[_iso_week(row["created_at"])][row["reason"]] += 1
    return {wk: dict(v) for wk, v in sorted(by_week.items())}


# ---------------------------------------------------------------------------
# §3.1 委派失败率
# ---------------------------------------------------------------------------
#
# 'running' is excluded (outcome not yet known). 'cancelled' is excluded
# from both sides — an explicit stop is neither success nor failure
# evidence, same carve-out as the environmental states in the skill
# co-occurrence check (server/skill_health.py). 'failed' and 'interrupted'
# both count as failure here (unlike the skill check, a delegation run has
# no separate "the worker explicitly gave up vs. got killed" distinction
# the way task_runs.state does) — this choice is executor's call, pinned
# here, see docs/plans/process-metrics.md §3.1 "口径细节执行者定并写死".

DELEGATION_FAILURE_SQL = """
SELECT state, COUNT(*) AS n FROM delegation_runs
WHERE state IN ('completed', 'failed', 'interrupted')
GROUP BY state
"""


def delegation_failure_rate(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = _rows(conn, DELEGATION_FAILURE_SQL)
    counts = {r["state"]: r["n"] for r in rows}
    denominator = sum(counts.values())
    numerator = counts.get("failed", 0) + counts.get("interrupted", 0)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": (numerator / denominator) if denominator else None,
        "state_counts": counts,
        "sql": DELEGATION_FAILURE_SQL,
    }


# ---------------------------------------------------------------------------
# §3.1 每任务成本 — turn_usage 聚合,仅 backend='claude-code'
# ---------------------------------------------------------------------------
#
# codex's `cost` is always NULL (the backend doesn't report pricing) and its
# `total_tokens` is a per-thread cumulative value, not a per-turn increment
# (multiagent-field-report-data.md §2.4) — mixing it in would silently
# inflate totals by an order of magnitude. Scoped to claude-code only, and
# that scope is stated in the rendered report, not left implicit.

COST_PER_TASK_SQL = """
SELECT tr.task_id AS task_id, SUM(tu.cost) AS task_cost
FROM task_runs tr
JOIN turn_usage tu ON tu.session_id = tr.session_id
WHERE tr.session_id IS NOT NULL AND tu.backend = 'claude-code'
GROUP BY tr.task_id
"""


def cost_per_task(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = _rows(conn, COST_PER_TASK_SQL)
    costs = sorted(r["task_cost"] or 0.0 for r in rows)
    n = len(costs)
    mean = (sum(costs) / n) if n else None
    median = (costs[n // 2] if n % 2 else (costs[n // 2 - 1] + costs[n // 2]) / 2) if n else None
    return {
        "task_count": n,
        "mean": mean,
        "median": median,
        "total": sum(costs) if n else None,
        "sql": COST_PER_TASK_SQL,
    }


# ---------------------------------------------------------------------------
# §3.1 / §3.3 技能涉败率 — current snapshot per active approved skill lineage
# ---------------------------------------------------------------------------
#
# Mirrors server/skill_health.py's definition exactly (decisive states =
# completed/failed/blocked, fail-like = failed/blocked, interrupted/
# cancelled/running excluded from both sides) — this is the READ-ONLY
# reporting view of that same co-occurrence math; it never writes a
# candidate. Kept as a separate, independently-written query (not an
# import of skill_health.py, which is async and Database-object-shaped)
# so the report stays a single, synchronous, dependency-light script that
# runs against a plain sqlite3 snapshot connection — but the formula below
# must stay in lockstep with skill_health.py's; both are frozen together.

_DECISIVE_STATES = ("completed", "failed", "blocked")
_FAIL_LIKE_STATES = ("failed", "blocked")

SKILL_FAILURE_RATE_METHODOLOGY = (
    "per active approved+non-superseded skill lineage (deduped by canonical "
    "lineage key — repository only distinguishes agent+repo scope): "
    "SELECT DISTINCT si.run_id FROM skill_invocations si JOIN "
    "skill_candidates sc ON sc.id = si.candidate_id WHERE si.run_id IS NOT "
    "NULL AND <lineage filter>; then for each run_id, task_runs.state in "
    "(completed,failed,blocked) is decisive, state in (failed,blocked) is "
    "fail-like; rate = fail-like / decisive"
)


def _skill_lineage_key(cand: sqlite3.Row) -> tuple[Any, ...]:
    """Canonical lineage identity — same rule as `server/skill_health.py`'s
    `_lineage_key`: `repository` only distinguishes lineages for
    `scope='agent+repo'`. Without this, a same-scope revision (which leaves
    the PRIOR row's `superseded_at` NULL forever — only a scope CHANGE
    supersedes) makes both rows satisfy `status='approved' AND
    superseded_at IS NULL` and the report lists the same lineage's identical
    co-occurrence stats twice (Snape review)."""
    repository = cand["repository"] if cand["scope"] == "agent+repo" else None
    return (cand["slug"], cand["scope"], cand["proposed_by_agent_id"], repository)


def skill_failure_rates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    all_approved = _rows(
        conn,
        "SELECT id, slug, title, scope, proposed_by_agent_id, repository, "
        "created_at FROM skill_candidates "
        "WHERE status = 'approved' AND superseded_at IS NULL "
        "ORDER BY created_at DESC",
    )
    seen: set[tuple[Any, ...]] = set()
    candidates: list[sqlite3.Row] = []
    for cand in all_approved:
        key = _skill_lineage_key(cand)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(cand)

    results: list[dict[str, Any]] = []
    for cand in candidates:
        lineage_filter = "sc.slug = ? AND sc.scope = ?"
        params: list[Any] = [cand["slug"], cand["scope"]]
        if cand["proposed_by_agent_id"] is not None:
            lineage_filter += " AND sc.proposed_by_agent_id = ?"
            params.append(cand["proposed_by_agent_id"])
        else:
            lineage_filter += " AND sc.proposed_by_agent_id IS NULL"
        if cand["scope"] == "agent+repo":
            lineage_filter += " AND sc.repository = ?"
            params.append(cand["repository"])
        run_ids = _rows(
            conn,
            "SELECT DISTINCT si.run_id FROM skill_invocations si "
            f"JOIN skill_candidates sc ON sc.id = si.candidate_id "
            f"WHERE si.run_id IS NOT NULL AND {lineage_filter}",
            tuple(params),
        )
        states = []
        for row in run_ids:
            run = conn.execute(
                "SELECT state FROM task_runs WHERE id = ?", (row["run_id"],)
            ).fetchone()
            if run is not None and run["state"] in _DECISIVE_STATES:
                states.append(run["state"])
        eligible = len(states)
        fail_like = sum(1 for s in states if s in _FAIL_LIKE_STATES)
        results.append(
            {
                "slug": cand["slug"],
                "title": cand["title"],
                "eligible": eligible,
                "fail_like": fail_like,
                "rate": (fail_like / eligible) if eligible else None,
            }
        )
    return results


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def compute_all(conn: sqlite3.Connection) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    return {
        "metrics_version": METRICS_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "one_pass_rate": one_pass_rate(conn),
        "retry_rate": retry_rate(conn),
        "human_takeover_rate": human_takeover_rate(conn),
        "turn_death_distribution": turn_death_distribution(conn),
        "turn_death_distribution_by_week": turn_death_distribution_by_week(conn),
        "delegation_failure_rate": delegation_failure_rate(conn),
        "cost_per_task": cost_per_task(conn),
        "skill_failure_rates": skill_failure_rates(conn),
    }
