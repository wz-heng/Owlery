"""server/skill_health.py — skill co-occurrence health check
(process-metrics.md §3.3, "技能成败回填"). Builds real Task Board tasks/runs
through `TaskRepository` (not raw SQL) so `task_runs.state` carries the same
FK-enforced, lifecycle-correct values production does, then drives real
`skill_invocations` rows through `SkillRegistry.record_usage` — the exact
path a live session's Skill tool use takes."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from server.config import settings
from server.database import Database
from server.skill_health import (
    AUTO_FLAG_MARKER,
    MIN_FAILURE_RATE,
    MIN_INVOCATIONS,
    detect_and_flag_corroded_skills,
)
from server.skill_registry import SkillRegistry
from server.task_board.repository import TaskRepository

SLUG = "hermes-pr-flow"
DESCRIPTION = "How to open a PR against an external repo."


def _skill_body(name: str = SLUG, description: str = DESCRIPTION) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\nBody content.\n"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
async def health_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _git(repo_dir, "init", "-q")
    _git(repo_dir, "config", "user.name", "Test")
    _git(repo_dir, "config", "user.email", "test@example.com")
    (repo_dir / "README.md").write_text("base\n")
    _git(repo_dir, "add", "README.md")
    _git(repo_dir, "commit", "-q", "-m", "base")

    monkeypatch.setattr(settings, "task_workspaces_dir", str(tmp_path / "workspaces"))
    monkeypatch.setattr(settings, "task_artifacts_dir", str(tmp_path / "artifacts"))

    db_path = str(tmp_path / "health.db")
    db = Database(db_path)
    await db.initialize()
    task_repo = TaskRepository(db_path)
    await task_repo.initialize()
    agent = await db.get_default_agent()

    from types import SimpleNamespace

    session_mgr = SimpleNamespace(
        sessions={"session-1": SimpleNamespace(agent_id=agent["id"], working_dir=str(repo_dir))}
    )
    reg = SkillRegistry()
    reg.bind(db=db, session_mgr=session_mgr)

    board = await task_repo.create_board(name="Board", working_dir=str(repo_dir))
    # Resolve the same way `propose()` resolves it internally (via `git
    # rev-parse --git-common-dir`) rather than trusting the raw tmp_path
    # string — on macOS `/tmp` is itself a symlink, so a raw-string
    # `repository` passed to `record_usage` can silently mismatch the
    # resolved one `propose()` stored, making the lookup return no match
    # and the invocation record silently (record_usage never raises) vanish.
    repository = await reg.resolve_repository(str(repo_dir))

    try:
        yield SimpleNamespace(
            db=db, task_repo=task_repo, reg=reg, agent_id=agent["id"],
            board_id=board.id, repo_dir=repo_dir, repository=repository,
        )
    finally:
        await task_repo.close()
        await db.close()


async def _run_with_outcome(rt, outcome: str, *, seq: int):
    """Create one task + one run reaching the given terminal `task_runs`
    state, then record a skill invocation against that run — mirroring a
    live session invoking the skill mid-task."""
    task = await rt.task_repo.create_task(
        board_id=rt.board_id,
        title=f"Task {seq}",
        body="body",
        status="todo",
        assignee_agent_id=rt.agent_id,
    )
    run = await rt.task_repo.claim_ready(
        task.id,
        workspace_mode="git_worktree",
        workspace_path=str(rt.repo_dir.parent / f"ws-{seq}"),
        run_id=f"run-{seq}",
    )
    if outcome == "completed":
        await rt.task_repo.complete_run(task.id, run.id, summary="done")
    elif outcome == "failed":
        await rt.task_repo.fail_run(task.id, run.id, error="boom")
    elif outcome == "blocked":
        await rt.task_repo.block_run(task.id, run.id, reason="needs input")
    elif outcome == "interrupted":
        await rt.task_repo.interrupt_run(task.id, run.id, reason="watchdog")
    elif outcome == "cancelled":
        await rt.task_repo.cancel_run(task.id, run.id)
    else:
        raise ValueError(outcome)

    await rt.reg.record_usage(
        SLUG, agent_id=rt.agent_id, repository=rt.repository,
        task_id=task.id, run_id=run.id,
    )
    return task, run


async def _approve_skill(rt, **overrides):
    kwargs = dict(
        session_id="session-1",
        slug=SLUG,
        title="Hermes PR flow",
        description=DESCRIPTION,
        body_markdown=_skill_body(),
        rationale="Walked this once, will recur.",
    )
    kwargs.update(overrides)
    candidate = await rt.reg.propose(**kwargs)
    return await rt.reg.approve(candidate["id"])


def test_threshold_constants_match_process_metrics_frozen_baseline():
    # process-metrics.md §3.3: "涉败率 ≥ 50% 且总调用 ≥ 3" — frozen on first
    # report; changing either number is a baseline change, not a tuning knob.
    assert MIN_INVOCATIONS == 3
    assert MIN_FAILURE_RATE == 0.5


@pytest.mark.asyncio
async def test_detect_and_flag_creates_candidate_when_threshold_crossed(health_runtime):
    rt = health_runtime
    await _approve_skill(rt)
    # failed, blocked, completed → 2/3 fail-like = 66.7% ≥ 50%, eligible=3 ≥ 3.
    # interrupted is the environmental carve-out — must not count either way.
    await _run_with_outcome(rt, "failed", seq=1)
    await _run_with_outcome(rt, "blocked", seq=2)
    await _run_with_outcome(rt, "completed", seq=3)
    await _run_with_outcome(rt, "interrupted", seq=4)

    created = await detect_and_flag_corroded_skills(rt.db)

    assert len(created) == 1
    flagged = created[0]
    assert flagged["status"] == "pending"
    assert flagged["slug"] == SLUG
    assert flagged["title"] == "[涉败告警] Hermes PR flow"
    # Body/description copied verbatim — approving this is a safe no-op
    # re-affirmation, never an auto-edit of the skill's actual content.
    assert flagged["body_markdown"] == _skill_body()
    assert flagged["description"] == DESCRIPTION
    assert AUTO_FLAG_MARKER in flagged["rationale"]
    assert "66%" in flagged["rationale"] or "67%" in flagged["rationale"]
    # The anchor task/run is one of the fail-like ones — gives the review
    # page's existing single "open task" evidence link a real target.
    anchor_tasks = {f"run-1", f"run-2"}
    assert flagged["run_id"] in anchor_tasks
    assert flagged["lint_results"]["slug_conflict"] is True

    # The review queue now shows it alongside the approved original.
    pending = await rt.db.list_skill_candidates(status="pending")
    assert [c["id"] for c in pending] == [flagged["id"]]


@pytest.mark.asyncio
async def test_detect_and_flag_excludes_environmental_states_from_denominator(health_runtime):
    rt = health_runtime
    await _approve_skill(rt)
    # 1 failed + 2 completed = 3 eligible, 33% fail rate: below 50% threshold
    # EVEN THOUGH there are also 5 interrupted/cancelled runs that would tip
    # it over 50% if wrongly counted as fail-like or even just as eligible.
    await _run_with_outcome(rt, "failed", seq=1)
    await _run_with_outcome(rt, "completed", seq=2)
    await _run_with_outcome(rt, "completed", seq=3)
    for seq in (4, 5, 6):
        await _run_with_outcome(rt, "interrupted", seq=seq)
    for seq in (7, 8):
        await _run_with_outcome(rt, "cancelled", seq=seq)

    created = await detect_and_flag_corroded_skills(rt.db)
    assert created == []


@pytest.mark.asyncio
async def test_detect_and_flag_requires_minimum_invocations(health_runtime):
    rt = health_runtime
    await _approve_skill(rt)
    # 2 eligible, both fail-like (100%) — rate clears the bar but the
    # MIN_INVOCATIONS floor (3) does not; too thin a sample to flag.
    await _run_with_outcome(rt, "failed", seq=1)
    await _run_with_outcome(rt, "blocked", seq=2)

    created = await detect_and_flag_corroded_skills(rt.db)
    assert created == []


@pytest.mark.asyncio
async def test_detect_and_flag_requires_minimum_failure_rate(health_runtime):
    rt = health_runtime
    await _approve_skill(rt)
    # 4 eligible, 1 fail-like = 25% < 50%.
    await _run_with_outcome(rt, "failed", seq=1)
    await _run_with_outcome(rt, "completed", seq=2)
    await _run_with_outcome(rt, "completed", seq=3)
    await _run_with_outcome(rt, "completed", seq=4)

    created = await detect_and_flag_corroded_skills(rt.db)
    assert created == []


@pytest.mark.asyncio
async def test_detect_and_flag_is_idempotent_while_the_flag_is_still_pending(health_runtime):
    rt = health_runtime
    await _approve_skill(rt)
    await _run_with_outcome(rt, "failed", seq=1)
    await _run_with_outcome(rt, "blocked", seq=2)
    await _run_with_outcome(rt, "completed", seq=3)

    first = await detect_and_flag_corroded_skills(rt.db)
    assert len(first) == 1

    # Another co-occurring failure shows up, but the open flag from the
    # first scan hasn't been reviewed yet — must not spam a second one.
    await _run_with_outcome(rt, "failed", seq=4)
    second = await detect_and_flag_corroded_skills(rt.db)
    assert second == []
    pending = await rt.db.list_skill_candidates(status="pending")
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_detect_and_flag_reflags_after_the_prior_flag_is_resolved(health_runtime):
    rt = health_runtime
    await _approve_skill(rt)
    await _run_with_outcome(rt, "failed", seq=1)
    await _run_with_outcome(rt, "blocked", seq=2)
    await _run_with_outcome(rt, "completed", seq=3)

    first = await detect_and_flag_corroded_skills(rt.db)
    assert len(first) == 1
    await rt.reg.reject(first[0]["id"], review_note="false alarm, unrelated env flake")

    await _run_with_outcome(rt, "failed", seq=4)
    second = await detect_and_flag_corroded_skills(rt.db)
    assert len(second) == 1


@pytest.mark.asyncio
async def test_detect_and_flag_follows_a_same_scope_revision_but_keeps_shared_lineage_history(
    health_runtime,
):
    rt = health_runtime
    await _approve_skill(rt)
    await _run_with_outcome(rt, "failed", seq=1)
    await _run_with_outcome(rt, "blocked", seq=2)
    await _run_with_outcome(rt, "completed", seq=3)

    # A later, same-scope revision is approved. `approve()`'s `superseded_at`
    # marking only fires on a SCOPE change (a relocation) — a same-scope
    # re-approval leaves the prior row's `superseded_at` NULL forever, and
    # `list_skill_candidates(status='approved')` orders newest-first, so the
    # revision (not the original) is the one `detect_and_flag` evaluates for
    # this lineage. It has zero invocations of its OWN, but
    # `list_skill_invocations_for_lineage` groups by (slug, scope, agent,
    # repository) rather than by candidate row id — so the prior failures
    # still belong to "this skill" and must still surface.
    revision = await rt.reg.propose(
        session_id="session-1", slug=SLUG, title="Hermes PR flow",
        description=DESCRIPTION, body_markdown=_skill_body() + "\nMore.\n",
        rationale="Hand-authored revision, unrelated to the auto-flag.",
    )
    await rt.reg.approve(revision["id"])
    assert (await rt.db.get_skill_candidate(revision["id"]))["superseded_at"] is None

    created = await detect_and_flag_corroded_skills(rt.db)
    assert len(created) == 1
    assert created[0]["slug"] == SLUG
    # Anchored off the active (revision) row's identity, not the dead one.
    assert created[0]["proposed_by_agent_id"] == revision["proposed_by_agent_id"]


@pytest.mark.asyncio
async def test_detect_and_flag_ignores_a_cross_scope_superseded_row(health_runtime):
    rt = health_runtime
    await _approve_skill(rt)  # agent+repo (default)
    await _run_with_outcome(rt, "failed", seq=1)
    await _run_with_outcome(rt, "blocked", seq=2)
    await _run_with_outcome(rt, "completed", seq=3)

    # Relocating to agent-global IS a scope change — this one really does
    # mark the prior row `superseded_at` and de-materializes it. The
    # now-dead `agent+repo` row must not be separately evaluated under its
    # own (now-defunct) lineage key just because `superseded_at` wasn't
    # checked.
    global_revision = await rt.reg.propose(
        session_id="session-1", slug=SLUG, title="Hermes PR flow",
        description=DESCRIPTION, body_markdown=_skill_body() + "\nGlobal.\n",
        rationale="Relocating to agent-global.", scope="agent-global",
    )
    approved_global = await rt.reg.approve(global_revision["id"])
    assert approved_global["superseded_at"] is None
    old_candidates = await rt.db.list_skill_candidates(status="approved")
    old_row = next(c for c in old_candidates if c["scope"] == "agent+repo")
    assert old_row["superseded_at"] is not None

    # The relocated (agent-global) lineage has zero invocations of its own
    # — below MIN_INVOCATIONS — so nothing should be flagged at all.
    created = await detect_and_flag_corroded_skills(rt.db)
    assert created == []


@pytest.mark.asyncio
async def test_detect_and_flag_never_raises_on_a_lineage_with_a_dangling_run_id(health_runtime, monkeypatch):
    rt = health_runtime
    await _approve_skill(rt)
    await _run_with_outcome(rt, "failed", seq=1)
    await _run_with_outcome(rt, "blocked", seq=2)
    await _run_with_outcome(rt, "completed", seq=3)

    # Simulate a run_id that no longer resolves (e.g. a race with cascade
    # delete) by monkeypatching get_run_summary to blow up for one id.
    original_get_run_summary = rt.db.get_run_summary

    async def flaky(run_id):
        if run_id == "run-2":
            raise RuntimeError("simulated dangling row")
        return await original_get_run_summary(run_id)

    monkeypatch.setattr(rt.db, "get_run_summary", flaky)

    # The whole lineage is skipped for this scan rather than raising and
    # taking the entire job down — it'll be re-evaluated clean next run.
    created = await detect_and_flag_corroded_skills(rt.db)
    assert created == []


@pytest.mark.asyncio
async def test_detect_and_flag_dedupes_cross_repository_agent_global_lineage(health_runtime):
    rt = health_runtime
    await _approve_skill(rt, scope="agent-global")
    await _run_with_outcome(rt, "failed", seq=1)
    await _run_with_outcome(rt, "blocked", seq=2)
    await _run_with_outcome(rt, "completed", seq=3)

    # An `agent-global` candidate has no repository dimension
    # (`Database.list_skill_invocations_for_lineage` only filters on
    # `repository` for `scope='agent+repo'`) — proposing the SAME slug from
    # a SECOND repository is the SAME lineage, sharing the same invocation
    # history, not an independent one. Comparing `repository`
    # unconditionally (Snape review) would evaluate it twice and raise two
    # redundant auto-flags.
    other_repo = rt.repo_dir.parent / "other-repo"
    other_repo.mkdir()
    _git(other_repo, "init", "-q")
    _git(other_repo, "config", "user.name", "Test")
    _git(other_repo, "config", "user.email", "test@example.com")
    (other_repo / "README.md").write_text("base\n")
    _git(other_repo, "add", "README.md")
    _git(other_repo, "commit", "-q", "-m", "base")
    from types import SimpleNamespace

    rt.reg.session_mgr.sessions["session-2"] = SimpleNamespace(
        agent_id=rt.agent_id, working_dir=str(other_repo)
    )
    other_repository = await rt.reg.resolve_repository(str(other_repo))
    assert other_repository != rt.repository

    second = await rt.reg.propose(
        session_id="session-2", slug=SLUG, title="Hermes PR flow",
        description=DESCRIPTION, body_markdown=_skill_body(),
        rationale="Proposed again from a different repo.", scope="agent-global",
    )
    await rt.reg.approve(second["id"])

    created = await detect_and_flag_corroded_skills(rt.db)
    assert len(created) == 1


@pytest.mark.asyncio
async def test_detect_and_flag_scans_past_the_default_200_row_candidate_page(health_runtime):
    rt = health_runtime
    # `Database.list_skill_candidates` defaults to a UI-page-sized
    # `limit=200`, `ORDER BY created_at DESC` — pad the table with enough
    # NEWER approved candidates that the real lineage under test falls
    # PAST that default page. The target must be approved FIRST (Snape
    # review, round 2: inserting fillers before the target left the target
    # newest-first and passing trivially even with the old, truncating
    # default — this ordering is what actually exercises the boundary).
    await _approve_skill(rt)
    await _run_with_outcome(rt, "failed", seq=1)
    await _run_with_outcome(rt, "blocked", seq=2)
    await _run_with_outcome(rt, "completed", seq=3)

    from datetime import datetime, timezone

    for i in range(205):
        await rt.db._conn.execute(
            "INSERT INTO skill_candidates "
            "(id, slug, title, description, body_markdown, repository, "
            "rationale, status, proposed_by_agent_id, scope, use_count, "
            "created_at, updated_at) "
            "VALUES (?, ?, 'Filler', 'filler', 'filler', ?, 'filler', "
            "'approved', ?, 'agent+repo', 0, ?, ?)",
            (
                f"filler-{i}", f"filler-skill-{i}", str(rt.repo_dir),
                rt.agent_id,
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    await rt.db._conn.commit()

    # Prove the setup actually exercises the truncation boundary: with the
    # UI-page-sized default, the target lineage is NOT on the first page.
    default_page = await rt.db.list_skill_candidates(status="approved")
    assert len(default_page) == 200
    assert SLUG not in {c["slug"] for c in default_page}

    created = await detect_and_flag_corroded_skills(rt.db)
    assert len(created) == 1
    assert created[0]["slug"] == SLUG


@pytest.mark.asyncio
async def test_detect_and_flag_is_race_safe_under_concurrent_scans(health_runtime):
    import asyncio

    rt = health_runtime
    await _approve_skill(rt)
    await _run_with_outcome(rt, "failed", seq=1)
    await _run_with_outcome(rt, "blocked", seq=2)
    await _run_with_outcome(rt, "completed", seq=3)

    # Two scans racing against the same lineage (e.g. two overlapping
    # Schedule-triggered runs, or a manual CLI run overlapping the Schedule)
    # must still produce exactly ONE pending auto-flag — a plain
    # check-then-insert (read `pending`, then INSERT) lets both coroutines
    # pass the check before either commits its insert (Snape review);
    # `Database.create_skill_candidate_if_not_already_flagged`'s atomic
    # `INSERT ... WHERE NOT EXISTS` is what actually has to prevent this.
    results = await asyncio.gather(
        detect_and_flag_corroded_skills(rt.db),
        detect_and_flag_corroded_skills(rt.db),
    )
    total_created = sum(len(r) for r in results)
    assert total_created == 1
    pending = await rt.db.list_skill_candidates(status="pending")
    assert len(pending) == 1


# --- Database.create_skill_candidate_if_not_already_flagged (direct) -------
# --- Database.connect_existing_without_migrating (direct) ------------------


def _candidate_kwargs(**overrides) -> dict:
    from datetime import datetime, timezone

    kwargs = dict(
        candidate_id="c1", slug=SLUG, title="Hermes PR flow",
        description=DESCRIPTION, body_markdown=_skill_body(),
        repository="/tmp/some-repo", rationale=f"{AUTO_FLAG_MARKER}\nbody",
        proposed_by_agent_id=None, proposed_by_session_id=None,
        task_id=None, run_id=None, scope="agent+repo", bundle_files=None,
        lint_results=None, created_at=datetime.now(timezone.utc).isoformat(),
        auto_flag_marker=AUTO_FLAG_MARKER,
    )
    kwargs.update(overrides)
    return kwargs


@pytest.mark.asyncio
async def test_create_skill_candidate_if_not_already_flagged_skips_a_second_call(tmp_path: Path):
    db = Database(str(tmp_path / "direct.db"))
    await db.initialize()
    try:
        first = await db.create_skill_candidate_if_not_already_flagged(
            **_candidate_kwargs(candidate_id="c1")
        )
        assert first is not None
        second = await db.create_skill_candidate_if_not_already_flagged(
            **_candidate_kwargs(candidate_id="c2")
        )
        assert second is None
        rows = await db.list_skill_candidates(status="pending")
        assert [r["id"] for r in rows] == ["c1"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_create_skill_candidate_if_not_already_flagged_scopes_by_repository_for_agent_repo(
    tmp_path: Path,
):
    db = Database(str(tmp_path / "direct.db"))
    await db.initialize()
    try:
        first = await db.create_skill_candidate_if_not_already_flagged(
            **_candidate_kwargs(candidate_id="c1", repository="/tmp/repo-a")
        )
        # A different repository under `agent+repo` scope IS a different
        # lineage — must NOT be skipped.
        second = await db.create_skill_candidate_if_not_already_flagged(
            **_candidate_kwargs(candidate_id="c2", repository="/tmp/repo-b")
        )
        assert first is not None
        assert second is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_create_skill_candidate_if_not_already_flagged_ignores_repository_for_agent_global(
    tmp_path: Path,
):
    db = Database(str(tmp_path / "direct.db"))
    await db.initialize()
    try:
        first = await db.create_skill_candidate_if_not_already_flagged(
            **_candidate_kwargs(
                candidate_id="c1", repository="/tmp/repo-a", scope="agent-global"
            )
        )
        # `agent-global` has no repository dimension — a different
        # repository is still the SAME lineage and must be skipped.
        second = await db.create_skill_candidate_if_not_already_flagged(
            **_candidate_kwargs(
                candidate_id="c2", repository="/tmp/repo-b", scope="agent-global"
            )
        )
        assert first is not None
        assert second is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_connect_existing_without_migrating_raises_on_a_schema_less_file(tmp_path: Path):
    # An empty file has no `skill_candidates` table — this connection mode
    # intentionally refuses to help (that's what `initialize()` is for) so
    # a caller never silently operates against a stale/absent schema.
    empty_path = tmp_path / "empty.db"
    empty_path.touch()
    db = Database(str(empty_path))
    with pytest.raises(RuntimeError, match="skill_candidates"):
        await db.connect_existing_without_migrating()


@pytest.mark.asyncio
async def test_connect_existing_without_migrating_does_not_rerun_migrations(tmp_path: Path):
    db_path = str(tmp_path / "live.db")
    # Bring the schema fully current the normal way first — mirrors a real
    # production file a live `owlery serve` process already migrated.
    db = Database(db_path)
    await db.initialize()
    # A row that only still exists because the one-time Telegram-bridge
    # purge in `_apply_migrations()` already ran and won't run again.
    await db._conn.execute(
        "INSERT INTO bridge_mappings (chat_id, agent_id, session_id, platform) "
        "VALUES ('chat-1', NULL, NULL, 'telegram')"
    )
    await db._conn.commit()
    await db.close()

    # Reconnect via the migration-skipping path (what
    # `skill_backfill_cli.py` uses against a live production file) and
    # confirm the purge did NOT run a second time (Snape review: the CLI
    # previously called `initialize()` here, which would delete this row).
    db2 = Database(db_path)
    await db2.connect_existing_without_migrating()
    try:
        row = await db2._conn.execute(
            "SELECT COUNT(*) FROM bridge_mappings WHERE platform = 'telegram'"
        )
        count = (await row.fetchone())[0]
        assert count == 1

        # The connection is still fully usable for the one real write point.
        created = await db2.create_skill_candidate_if_not_already_flagged(
            **_candidate_kwargs(candidate_id="c1")
        )
        assert created is not None
    finally:
        await db2.close()


@pytest.mark.asyncio
async def test_skill_backfill_cli_only_ever_writes_skill_candidates(
    health_runtime, tmp_path: Path
):
    """Snape review, round 3 (and the narrower round-4 correction on this
    same test): `docs/reports/process-metrics/2026-11-16-schedule-fire-
    evidence.md` had claimed the Schedule-fire run was "全程只读", but
    `skill_backfill_cli.py` connects via
    `connect_existing_without_migrating()`, which is read-WRITE capable —
    it has to be, since inserting a new `skill_candidates` row IS the one
    real write point §3.3 calls for. This test is a narrower, honestly-
    scoped proof, run through the REAL CLI entrypoint (subprocess, not a
    direct Python call) against a disposable db — never production — in a
    scenario that DOES cross the threshold: in THIS scenario, the only
    table whose ROW COUNT changes is `skill_candidates`, by exactly the
    one row the detector creates. A row-count diff is NOT a full
    content/schema audit — it cannot catch an UPDATE, or a same-table
    delete+insert that nets to the same count, in some other table. Don't
    cite this as proof no other table was touched; cite it as: in this
    run, these were the row-count deltas."""
    rt = health_runtime
    await _approve_skill(rt)
    await _run_with_outcome(rt, "failed", seq=1)
    await _run_with_outcome(rt, "blocked", seq=2)
    await _run_with_outcome(rt, "completed", seq=3)

    db_path = tmp_path / "health.db"

    def _table_row_counts() -> dict[str, int]:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            return {
                t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in tables
            }
        finally:
            conn.close()

    before = _table_row_counts()

    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "process_metrics" / "skill_backfill_cli.py"),
            "--db-path",
            str(db_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "flagged" in result.stdout

    after = _table_row_counts()
    changed = {t: (before[t], after[t]) for t in before if before[t] != after[t]}
    assert changed == {
        "skill_candidates": (
            before["skill_candidates"],
            before["skill_candidates"] + 1,
        )
    }
