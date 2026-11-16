"""scripts/process_metrics/{metrics,report}.py (docs/plans/process-metrics.md
§3.1/§3.2). Builds a real dataset through `TaskRepository`/`SkillRegistry`/
`Database` write methods (not raw SQL) so every FK and state-machine
invariant production relies on is actually exercised, then hands the
resulting db file to the metrics module as a plain, synchronous
`sqlite3.Connection` — exactly the shape `report.py` hands it after taking
a `.backup` snapshot."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.config import settings
from server.database import Database
from server.skill_registry import SkillRegistry
from server.task_board.repository import TaskRepository

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.process_metrics import metrics  # noqa: E402
from scripts.process_metrics.report import render_markdown, snapshot  # noqa: E402


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
async def dataset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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

    db_path = str(tmp_path / "prod.db")
    db = Database(db_path)
    await db.initialize()
    task_repo = TaskRepository(db_path)
    await task_repo.initialize()
    agent = await db.get_default_agent()

    session_mgr = SimpleNamespace(
        sessions={"session-1": SimpleNamespace(agent_id=agent["id"], working_dir=str(repo_dir))}
    )
    reg = SkillRegistry()
    reg.bind(db=db, session_mgr=session_mgr)
    repository = await reg.resolve_repository(str(repo_dir))

    board = await task_repo.create_board(name="Board", working_dir=str(repo_dir))

    async def make_task(seq: int, outcome: str, *, attempt_no_matters: bool = False):
        task = await task_repo.create_task(
            board_id=board.id, title=f"Task {seq}", body="body",
            status="todo", assignee_agent_id=agent["id"],
        )
        session_id = f"session-task-{seq}"
        await db.save_session(
            session_id, f"session {seq}", str(repo_dir), "2026-01-01T00:00:00+00:00",
            agent_id=agent["id"],
        )
        run = await task_repo.claim_ready(
            task.id, workspace_mode="git_worktree",
            workspace_path=str(tmp_path / f"ws-{seq}"), run_id=f"run-{seq}",
        )
        await task_repo.attach_run_session(task.id, run.id, session_id)
        if outcome == "completed":
            await task_repo.complete_run(task.id, run.id, summary="done")
        elif outcome == "failed":
            await task_repo.fail_run(task.id, run.id, error="boom")
        elif outcome == "blocked":
            await task_repo.block_run(task.id, run.id, reason="needs input")
        elif outcome == "interrupted":
            await task_repo.interrupt_run(task.id, run.id, reason="watchdog")
        return task, run, session_id

    yield SimpleNamespace(
        db=db, task_repo=task_repo, reg=reg, agent_id=agent["id"],
        board_id=board.id, repo_dir=repo_dir, repository=repository,
        make_task=make_task, db_path=db_path,
    )
    await task_repo.close()
    await db.close()


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# --- 一次通过率 -------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_pass_rate(dataset):
    ds = dataset
    # Task 1: clean one-shot completion.
    await ds.make_task(1, "completed")
    # Task 2: completes, but only after a first attempt that was blocked —
    # not a clean one-pass story even though it eventually finished.
    task2, run2, _ = await ds.make_task(2, "blocked")
    await ds.task_repo.unblock_task(task2.id, actor_kind="user")
    run2b = await ds.task_repo.claim_ready(
        task2.id, workspace_mode="git_worktree",
        workspace_path=str(ds.repo_dir.parent / "ws-2b"), run_id="run-2b",
    )
    await ds.task_repo.complete_run(task2.id, run2b.id, summary="done on retry")
    # Task 3: never completes (still sitting blocked) — excluded from the
    # denominator entirely, not counted as a miss.
    await ds.make_task(3, "blocked")

    conn = _conn(ds.db_path)
    result = metrics.one_pass_rate(conn)
    conn.close()
    assert result["denominator"] == 2  # tasks 1 and 2 have completed at least once
    assert result["numerator"] == 1  # only task 1 was clean
    assert result["rate"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_one_pass_rate_empty_db_reports_none_not_a_crash(dataset):
    conn = _conn(dataset.db_path)
    result = metrics.one_pass_rate(conn)
    conn.close()
    assert result["denominator"] == 0
    assert result["rate"] is None


# --- 重试率 / 人工接管率 ------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_rate_and_human_takeover_rate(dataset):
    ds = dataset
    await ds.make_task(1, "completed")  # clean, attempt 1

    # Retried + completed, human had to unblock.
    task2, _run2, _ = await ds.make_task(2, "blocked")
    await ds.task_repo.unblock_task(task2.id, actor_kind="user")
    run2b = await ds.task_repo.claim_ready(
        task2.id, workspace_mode="git_worktree",
        workspace_path=str(ds.repo_dir.parent / "ws-2b"), run_id="run-2b",
    )
    await ds.task_repo.complete_run(task2.id, run2b.id, summary="done on retry")

    # Retried + completed, re-dispatched by the system (not a user unblock).
    task3, _run3, _ = await ds.make_task(3, "failed")
    await ds.task_repo.unblock_task(task3.id, actor_kind="system")
    run3b = await ds.task_repo.claim_ready(
        task3.id, workspace_mode="git_worktree",
        workspace_path=str(ds.repo_dir.parent / "ws-3b"), run_id="run-3b",
    )
    await ds.task_repo.complete_run(task3.id, run3b.id, summary="done on retry")

    conn = _conn(ds.db_path)
    retry = metrics.retry_rate(conn)
    takeover = metrics.human_takeover_rate(conn)
    conn.close()

    # 3 completed runs total (run-1, run-2b, run-3b); 2 of them attempt_no>1.
    assert retry["denominator"] == 3
    assert retry["numerator"] == 2
    assert retry["rate"] == pytest.approx(2 / 3)

    assert takeover["denominator"] == 2
    assert takeover["numerator"] == 1
    assert takeover["rate"] == pytest.approx(0.5)


# --- turn 死因分布 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_death_distribution(dataset):
    ds = dataset
    await ds.db.save_session(
        "session-task-1", "s1", str(ds.repo_dir), "2026-01-01T00:00:00+00:00",
        agent_id=ds.agent_id,
    )
    await ds.db.add_harness_exit(
        session_id="session-task-1", reason="completed", created_at="2026-01-05T00:00:00+00:00",
    )
    await ds.db.add_harness_exit(
        session_id="session-task-1", reason="watchdog_idle", created_at="2026-01-12T00:00:00+00:00",
    )
    await ds.db.add_harness_exit(
        session_id="session-task-1", reason="watchdog_idle", created_at="2026-01-13T00:00:00+00:00",
    )
    await ds.db.add_harness_exit(
        session_id="session-task-1", reason="process_error", created_at="2026-01-13T00:00:00+00:00",
    )

    conn = _conn(ds.db_path)
    dist = metrics.turn_death_distribution(conn)
    by_week = metrics.turn_death_distribution_by_week(conn)
    conn.close()

    assert dist["total"] == 4
    assert dist["counts"]["watchdog_idle"] == 2
    assert dist["rates"]["watchdog_idle"] == pytest.approx(0.5)
    assert len(by_week) == 2  # the two created_at dates land in different ISO weeks
    assert sum(sum(wk.values()) for wk in by_week.values()) == 4


# --- 委派失败率 --------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegation_failure_rate(dataset):
    ds = dataset
    await ds.db.save_session(
        "deleg-session", "deleg session", str(ds.repo_dir), "2026-01-01T00:00:00+00:00",
        agent_id=ds.agent_id,
    )
    for i, state in enumerate(["completed", "completed", "failed", "interrupted", "cancelled", "running"]):
        await ds.db.create_delegation_run(
            run_id=f"deleg-run-{i}", delegation_id="deleg-session", round_no=i,
            request="do something", start_seq=0, created_at="2026-01-01T00:00:00+00:00",
            state=state,
        )

    conn = _conn(ds.db_path)
    result = metrics.delegation_failure_rate(conn)
    conn.close()

    # 'running' and 'cancelled' excluded from both sides entirely.
    assert result["denominator"] == 4  # completed*2, failed, interrupted
    assert result["numerator"] == 2  # failed + interrupted
    assert result["rate"] == pytest.approx(0.5)


# --- 每任务成本 --------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_per_task_claude_code_only(dataset):
    ds = dataset
    task1, run1, session1 = await ds.make_task(1, "completed")
    task2, run2, session2 = await ds.make_task(2, "completed")

    await ds.db.add_turn_usage(
        created_at="2026-01-01T00:00:00+00:00", session_id=session1,
        backend="claude-code", cost=2.0, input_tokens=10,
    )
    await ds.db.add_turn_usage(
        created_at="2026-01-01T00:05:00+00:00", session_id=session1,
        backend="claude-code", cost=3.0, input_tokens=10,
    )
    await ds.db.add_turn_usage(
        created_at="2026-01-01T00:00:00+00:00", session_id=session2,
        backend="claude-code", cost=10.0, input_tokens=10,
    )
    # codex row on the same session — cost is NULL by construction (the
    # backend never reports one) and must not count toward the total.
    await ds.db.add_turn_usage(
        created_at="2026-01-01T00:00:00+00:00", session_id=session2,
        backend="codex", cost=None, input_tokens=99_999_999,
    )

    conn = _conn(ds.db_path)
    result = metrics.cost_per_task(conn)
    conn.close()

    assert result["task_count"] == 2
    assert result["mean"] == pytest.approx((5.0 + 10.0) / 2)
    assert result["total"] == pytest.approx(15.0)


# --- 技能涉败率 --------------------------------------------------------------


@pytest.mark.asyncio
async def test_skill_failure_rates_matches_skill_health_definition(dataset):
    ds = dataset
    candidate = await ds.reg.propose(
        session_id="session-1", slug="hermes-pr-flow", title="Hermes PR flow",
        description="How to open a PR.",
        body_markdown="---\nname: hermes-pr-flow\ndescription: How to open a PR.\n---\n\nBody.\n",
        rationale="Will recur.",
    )
    await ds.reg.approve(candidate["id"])

    task1, run1, _ = await ds.make_task(1, "failed")
    task2, run2, _ = await ds.make_task(2, "blocked")
    task3, run3, _ = await ds.make_task(3, "completed")
    task4, run4, _ = await ds.make_task(4, "interrupted")
    for task, run in ((task1, run1), (task2, run2), (task3, run3), (task4, run4)):
        await ds.reg.record_usage(
            "hermes-pr-flow", agent_id=ds.agent_id, repository=ds.repository,
            task_id=task.id, run_id=run.id,
        )

    conn = _conn(ds.db_path)
    rows = metrics.skill_failure_rates(conn)
    conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["slug"] == "hermes-pr-flow"
    assert row["eligible"] == 3  # interrupted excluded
    assert row["fail_like"] == 2  # failed + blocked
    assert row["rate"] == pytest.approx(2 / 3)


# --- report.py plumbing -----------------------------------------------------


def test_snapshot_is_read_only_and_copies_data(tmp_path: Path):
    source_path = str(tmp_path / "source.db")
    source = sqlite3.connect(source_path)
    source.execute("CREATE TABLE t (x INTEGER)")
    source.execute("INSERT INTO t VALUES (42)")
    source.commit()
    source.close()
    before = (tmp_path / "source.db").read_bytes()

    dest_path = str(tmp_path / "dest.db")
    snapshot(source_path, dest_path)

    after = (tmp_path / "source.db").read_bytes()
    assert before == after  # source untouched

    dest = sqlite3.connect(dest_path)
    assert dest.execute("SELECT x FROM t").fetchone() == (42,)
    dest.close()


def test_render_markdown_includes_every_metric_section():
    data = {
        "metrics_version": 1,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "one_pass_rate": {"numerator": 1, "denominator": 2, "rate": 0.5},
        "retry_rate": {"numerator": 1, "denominator": 4, "rate": 0.25},
        "human_takeover_rate": {"numerator": 1, "denominator": 1, "rate": 1.0},
        "turn_death_distribution": {"counts": {"completed": 1}, "total": 1, "rates": {"completed": 1.0}},
        "turn_death_distribution_by_week": {"2026-W01": {"completed": 1}},
        "delegation_failure_rate": {"numerator": 0, "denominator": 1, "rate": 0.0, "state_counts": {"completed": 1}},
        "cost_per_task": {"task_count": 1, "mean": 1.5, "median": 1.5, "total": 1.5},
        "skill_failure_rates": [{"slug": "s", "title": "S", "eligible": 3, "fail_like": 2, "rate": 2 / 3}],
    }
    out = render_markdown(data)
    for heading in (
        "一次通过率", "重试率", "人工接管率", "turn 死因分布", "委派失败率",
        "每任务成本", "技能涉败率",
    ):
        assert heading in out
