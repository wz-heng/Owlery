"""Skill co-occurrence health check (process-metrics.md §3.3 — "技能成败回填").

A skill's `use_count` only tracks how often it was invoked, never whether the
run it was invoked in actually worked out — a corroding skill and a healthy
one look identical on the shelf. This module closes that gap with the
narrowest claim the data supports: **co-occurrence, not causation**. It never
decides "this failure was the skill's fault" — a run that invoked a skill and
then failed only proves the two coincided, not that the skill caused it (the
exact "伪精确因果归因" trap `experience-consolidation-v2.md` §4 flagged when it
deferred this feature: "等第一个真实的 skill 腐化案例"). What this module does
is purely mechanical counting ("机器举手"): when a skill's invocations
coincide with fail-like Task Board outcomes often enough, it raises a new
`pending` row in the EXISTING skill candidate review queue — the same table
and the same human-approve/-reject gate every hand-authored candidate goes
through (`server/skill_registry.py`). No new storage, no new UI, and no
outcome ever takes effect without a human looking at the evidence first
(§4 "不做": "任何自动生效").

Threshold (process-metrics.md §3.3, frozen on first report):
    涉败率 ≥ 50% 且可判定调用 ≥ 3  (MIN_FAILURE_RATE / MIN_INVOCATIONS below)

"涉败" (co-occurred-with-failure) and "可判定" (decisive) are both defined in
terms of `task_runs.state`, scoped to the run that actually consumed the
skill:

- decisive  = state in {completed, failed, blocked} — the run reached a real
  outcome the skill could plausibly be associated with.
- 涉败       = state in {failed, blocked} — the run's own worker reported it
  didn't work out (a hard failure, or a block for input/capability/protocol).
- excluded entirely (not decisive, not 涉败) = {running, cancelled,
  interrupted}. `interrupted` is the task_runs-level signature of a run that
  died to watchdog/infra, not to anything the skill did or didn't do — this
  IS the "排除纯环境死因,如 watchdog/interrupted" carve-out from §3.3, and
  `task_runs.state='interrupted'` already carries that distinction without
  needing a second join into `harness_exits`. `cancelled` is an explicit stop,
  not a content judgment either way.

A "skill" for this purpose is a *lineage* — (slug, scope, proposed_by_agent_id,
repository) — not a single DB row, because approving a revision supersedes
the prior row while the invocation history (and the thing a reviewer cares
about) spans both (`Database.list_skill_invocations_for_lineage`, already
built for the review page's own evidence chain).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .database import Database

MIN_INVOCATIONS = 3
MIN_FAILURE_RATE = 0.5

_DECISIVE_STATES = frozenset({"completed", "failed", "blocked"})
_FAIL_LIKE_STATES = frozenset({"failed", "blocked"})

# Every auto-flag candidate's rationale carries this marker so a later run of
# `detect_and_flag_corroded_skills` can recognize "this lineage already has an
# open hand-up awaiting review" and skip re-flagging it — the queue is for a
# human to clear, not something this job should keep re-ringing.
AUTO_FLAG_MARKER = "[process-metrics:auto-flag]"

_EVIDENCE_LIMIT = 20

# `Database.list_skill_candidates`/`list_skill_invocations_for_lineage` both
# take a `limit` that defaults to a UI-page-sized number (200 / 50) — fine
# for a review page, silently wrong for a batch job that must see every
# active lineage and every invocation to compute a correct rate (Snape
# review: the defaults made older lineages past the 200th never get
# scanned, and could re-flag an already-pending lineage whose earlier flag
# had aged past that cutoff). `scripts/process_metrics/metrics.py`'s
# read-only reporting view of this same math uses raw, unlimited SQL — this
# constant is this module's way of matching that "see everything" behavior
# without writing a second, real-pagination API that nothing else needs at
# today's table sizes.
_LIST_ALL = 1_000_000


async def _lineage_decisive_runs(
    db: "Database", candidate: dict[str, Any]
) -> list[dict[str, Any]]:
    """Every run_id in this skill's full invocation lineage that reached a
    decisive `task_runs.state`, most-recent first, annotated with that state
    and the owning task_id."""
    invocations = await db.list_skill_invocations_for_lineage(
        slug=candidate["slug"],
        scope=candidate["scope"],
        agent_id=candidate["proposed_by_agent_id"],
        repository=candidate["repository"],
        limit=_LIST_ALL,
    )
    decisive: list[dict[str, Any]] = []
    seen_runs: set[str] = set()
    for inv in invocations:
        run_id = inv.get("run_id")
        if not run_id or run_id in seen_runs:
            continue
        seen_runs.add(run_id)
        run = await db.get_run_summary(run_id)
        if run is None or run["state"] not in _DECISIVE_STATES:
            continue
        decisive.append(
            {
                "run_id": run_id,
                "task_id": run["task_id"],
                "state": run["state"],
                "used_at": inv.get("used_at"),
            }
        )
    return decisive


def _lineage_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    """Canonical lineage identity. MUST mirror
    `Database.list_skill_invocations_for_lineage`'s own definition exactly:
    `repository` only participates for `scope='agent+repo'` — an
    `agent-global` candidate has no repository dimension (same reasoning as
    `get_latest_approved_skill_by_slug`). Comparing `repository`
    unconditionally (Snape review) treated the SAME `agent-global` lineage
    proposed from two different repositories as two independent lineages:
    `detect_and_flag_corroded_skills` would evaluate it twice and raise two
    redundant auto-flags for what a reviewer would recognize as one skill,
    and `scripts/process_metrics/metrics.py`'s report would list it twice
    with identical co-occurrence stats."""
    repository = candidate["repository"] if candidate["scope"] == "agent+repo" else None
    return (
        candidate["slug"],
        candidate["scope"],
        candidate["proposed_by_agent_id"],
        repository,
    )


def _already_flagged(
    pending_candidates: list[dict[str, Any]], candidate: dict[str, Any]
) -> bool:
    target = _lineage_key(candidate)
    for row in pending_candidates:
        if (
            _lineage_key(row) == target
            and AUTO_FLAG_MARKER in (row.get("rationale") or "")
        ):
            return True
    return False


def _rationale(
    candidate: dict[str, Any],
    *,
    eligible: int,
    fail_like: int,
    rate: float,
    evidence: list[dict[str, Any]],
) -> str:
    lines = [
        AUTO_FLAG_MARKER,
        "过程指标·技能成败回填(docs/plans/process-metrics.md §3.3)自动生成"
        "的共现统计告警,机器只举手、不判因果——是否真是这个技能的锅、该修订"
        "还是废弃,由人审判断。",
        "",
        f"口径:该技能(含同宗族历史版本)在 Task Board 的 {eligible} 次可判定"
        f"调用中,{fail_like} 次所在的 run 终态是 failed/blocked"
        f"(涉败率 {rate:.0%})。阈值:涉败率 ≥ {MIN_FAILURE_RATE:.0%} 且可判定"
        f"调用 ≥ {MIN_INVOCATIONS}(已排除 interrupted/cancelled 等环境性终态"
        "及仍在跑的 run)。",
        "",
        f"涉败 run 清单(task_id · run_id · 终态,最多列 {_EVIDENCE_LIMIT} 条,"
        "按时间倒序;在 Task Board 按 task_id 搜索即可打开对应任务的完整"
        "审计回放——评论、每轮 run、result_summary):",
    ]
    for row in evidence[:_EVIDENCE_LIMIT]:
        lines.append(f"- {row['task_id']} · {row['run_id']} · {row['state']}")
    return "\n".join(lines)


async def detect_and_flag_corroded_skills(
    db: "Database",
    *,
    min_invocations: int = MIN_INVOCATIONS,
    min_failure_rate: float = MIN_FAILURE_RATE,
) -> list[dict[str, Any]]:
    """Scan every currently-active approved skill lineage for a co-occurrence
    rate crossing the frozen threshold, and raise a `pending` skill candidate
    for each one not already flagged. Returns the newly created candidate
    rows (empty if nothing crossed the threshold or everything that did was
    already flagged) — never raises for an individual lineage's bad data, so
    one malformed row can't take the whole scan down.

    This is the ONE write point for the whole feature (process-metrics.md
    §3.3: "数据写点仅一处:候补生成,复用现有 candidate 机制") — it inserts into
    the SAME `skill_candidates` table the hand-authored `skills.propose` MCP
    tool writes to (via the atomic `Database.
    create_skill_candidate_if_not_already_flagged`, not a new table or a new
    code path). Safe to run as often as you like, including concurrently
    with itself (idempotent per lineage via `AUTO_FLAG_MARKER`, enforced at
    the database layer rather than by this function's own read-then-write —
    see that method's docstring); intended to run from the same weekly
    schedule as the process-metrics report, but
    doesn't depend on it.
    """
    approved = await db.list_skill_candidates(status="approved", limit=_LIST_ALL)
    active = [c for c in approved if not c.get("superseded_at")]
    # One lineage can appear more than once among `active` rows only if two
    # different (agent, repository) pairs independently approved the same
    # slug — genuinely two different skills that happen to share a name —
    # OR if the same `agent-global` lineage was independently proposed from
    # two different repositories (same skill, no repository dimension to
    # tell them apart). Dedup on the CANONICAL lineage key (`_lineage_key`)
    # so each is only ever evaluated once.
    seen_lineages: set[tuple[Any, ...]] = set()
    pending = await db.list_skill_candidates(status="pending", limit=_LIST_ALL)
    created: list[dict[str, Any]] = []

    for candidate in active:
        lineage_key = _lineage_key(candidate)
        if lineage_key in seen_lineages:
            continue
        seen_lineages.add(lineage_key)

        if _already_flagged(pending, candidate):
            continue

        try:
            decisive = await _lineage_decisive_runs(db, candidate)
        except Exception:
            # One lineage's bad data (a dangling run_id, a malformed row)
            # must never block evaluating the rest.
            continue

        eligible = len(decisive)
        if eligible < min_invocations:
            continue
        fail_like = [r for r in decisive if r["state"] in _FAIL_LIKE_STATES]
        rate = len(fail_like) / eligible
        if rate < min_failure_rate:
            continue

        # Most recent 涉败 run gives the existing review-page UI its one real
        # clickable "open task" evidence link; the full list still goes into
        # the rationale text (see `_rationale`) since skill_candidates has
        # only a single task_id/run_id FK pair, not a list column.
        anchor = fail_like[0]
        candidate_id = uuid.uuid4().hex[:12]
        new_row = await db.create_skill_candidate_if_not_already_flagged(
            candidate_id=candidate_id,
            slug=candidate["slug"],
            title=f"[涉败告警] {candidate['title']}",
            description=candidate["description"],
            body_markdown=candidate["body_markdown"],
            repository=candidate["repository"],
            rationale=_rationale(
                candidate,
                eligible=eligible,
                fail_like=len(fail_like),
                rate=rate,
                evidence=fail_like,
            ),
            proposed_by_agent_id=candidate["proposed_by_agent_id"],
            proposed_by_session_id=None,
            task_id=anchor["task_id"],
            run_id=anchor["run_id"],
            scope=candidate["scope"],
            bundle_files=candidate.get("bundle_files"),
            lint_results={
                "frontmatter_valid": True,
                "slug_conflict": True,
                "bundle_refs_valid": True,
                "issues": [
                    f"auto-flag: co-occurrence rate {rate:.0%} over "
                    f"{eligible} decisive invocations crossed the "
                    f"{min_failure_rate:.0%}/{min_invocations}+ threshold — "
                    "approving this re-affirms the skill unchanged; reject "
                    "it (with a note) if the evidence is a false alarm, or "
                    "propose a hand-authored revision/retirement separately "
                    "if it isn't",
                ],
            },
            created_at=datetime.now(timezone.utc).isoformat(),
            auto_flag_marker=AUTO_FLAG_MARKER,
        )
        # A concurrent scan (this process or another) may have won the race
        # and inserted a flag for this same lineage between our `pending`
        # snapshot above and this INSERT — the atomic WHERE-NOT-EXISTS guard
        # in `create_skill_candidate_if_not_already_flagged` is what actually
        # prevents the duplicate; `None` here just means we lost that race,
        # which is exactly as idempotent as never having raced at all.
        if new_row is not None:
            created.append(new_row)

    return created
