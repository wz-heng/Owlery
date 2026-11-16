#!/usr/bin/env python3
"""Weekly process-metrics report (docs/plans/process-metrics.md).

Read-only, zero model calls: every number in the rendered report comes out
of `metrics.py`'s deterministic SQL against a point-in-time SQLite `.backup`
snapshot of the production DB — never the live file, and never written to.
Intended to run from an Owlery Schedule once a week; the schedule's prompt
should do nothing but invoke this script and confirm the output file was
written — no model turn interprets or touches any of the numbers.

    .venv/bin/python scripts/process_metrics/report.py \\
        --db-path /path/to/owlery.db \\
        --out-dir docs/reports/process-metrics

`--db-path` is REQUIRED and never auto-discovered: this repo has had more
than one `owlery.db` on disk at once (an empty pre-rename legacy file, a dev
checkout's own copy, and the actual file a running `owlery serve` has open)
— silently picking one is exactly the trap a production deployment can fall
into (see the task card's recon comment). Resolve the real path with
`lsof -c Python | grep owlery.db` against the live process before running
this against a deployment you don't already know the path for.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import pathname2url

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.process_metrics.metrics import (  # noqa: E402
    SKILL_FAILURE_RATE_METHODOLOGY,
    compute_all,
)


def snapshot(db_path: str, dest_path: str) -> None:
    """Online SQLite backup — doesn't hold a write lock, doesn't block the
    live server, and never mutates the source file (multiagent-field-report-
    data.md §1's own method, same safety argument). The source path is
    percent-encoded via `pathname2url` before being embedded in the `file:`
    URI — a raw f-string (Snape review) would silently mis-parse a path
    containing `?`, `#`, or a space, which SQLite's URI filename syntax
    treats as query-string/fragment delimiters rather than literal path
    characters."""
    source_uri = f"file:{pathname2url(str(Path(db_path).resolve()))}?mode=ro"
    source = sqlite3.connect(source_uri, uri=True)
    try:
        dest = sqlite3.connect(dest_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.2f}"


def _sql_block(sql: str) -> list[str]:
    """`metrics.py`'s module docstring promises "the exact query is quoted
    in the rendered report so a reader can verify the number instead of
    trusting prose" — the original draft never actually did this (Snape
    review: `render_markdown` computed every section from `data[...]` but
    never read `data[...]["sql"]`), so that claim was false. This renders
    it as a collapsed `<details>` block so the prose summary stays the
    primary reading experience."""
    return [
        "",
        "<details><summary>SQL / 口径明细</summary>",
        "",
        "```sql" if "SELECT" in sql.upper() else "```",
        sql.strip(),
        "```",
        "",
        "</details>",
    ]


def render_markdown(data: dict) -> str:
    lines: list[str] = []
    lines.append("# 过程指标周报")
    lines.append("")
    lines.append(f"生成时间:{data['generated_at']}(UTC) · 指标口径版本 v{data['metrics_version']}")
    lines.append("")
    lines.append(
        "本报告零模型调用:全部数字来自对生产库 `.backup` 快照的只读 SQL 聚合"
        "(脚本:`scripts/process_metrics/metrics.py`),口径首报即冻结——"
        "改口径必须升版本号,不得静默改算法(docs/plans/process-metrics.md §3.1)。"
    )
    lines.append("")

    opr = data["one_pass_rate"]
    lines.append("## 一次通过率")
    lines.append(
        f"- {_pct(opr['rate'])}({opr['numerator']}/{opr['denominator']})"
    )
    lines.append(
        "- 口径:completed 且 attempt=1 且该任务从未有任何 run 到达过 "
        "blocked 状态 / 全部曾经 completed 过的任务数。"
    )
    lines.extend(_sql_block(opr["sql"]))
    lines.append("")

    rr = data["retry_rate"]
    lines.append("## 重试率")
    lines.append(f"- {_pct(rr['rate'])}({rr['numerator']}/{rr['denominator']})")
    lines.append("- 口径:`task_runs.state='completed' AND attempt_no>1` / 全部 completed run(run 级,沿田野报告基线)。")
    lines.extend(_sql_block(rr["sql"]))
    lines.append("")

    htr = data["human_takeover_rate"]
    lines.append("## 人工接管率")
    lines.append(f"- {_pct(htr['rate'])}({htr['numerator']}/{htr['denominator']})")
    lines.append(
        "- 口径:上述重试 run 里,`claimed_at` 之前最近一条 `task_events."
        "kind='task_unblocked'`(人工解除阻塞)、且 actor_kind 是 user 的占比"
        "——不是任意最近事件,普通评论等其它 kind 不计入(需要人工 unblock "
        "才能重跑)。"
    )
    lines.extend(_sql_block(htr["sql"]))
    lines.append("")

    td = data["turn_death_distribution"]
    lines.append("## turn 死因分布(harness_exits.reason,六分类)")
    if td["total"]:
        for reason in sorted(td["counts"], key=lambda r: -td["counts"][r]):
            n = td["counts"][reason]
            lines.append(f"- {reason}: {n} ({_pct(td['rates'][reason])})")
    else:
        lines.append("- (无数据)")
    lines.append("")
    lines.append("### 按周趋势")
    by_week = data["turn_death_distribution_by_week"]
    if by_week:
        reasons = sorted({r for wk in by_week.values() for r in wk})
        lines.append("| 周 | " + " | ".join(reasons) + " |")
        lines.append("|---|" + "---|" * len(reasons))
        for wk, counts in by_week.items():
            lines.append(f"| {wk} | " + " | ".join(str(counts.get(r, 0)) for r in reasons) + " |")
    else:
        lines.append("(无数据)")
    lines.extend(_sql_block(td["sql"]))
    lines.append("")

    dfr = data["delegation_failure_rate"]
    lines.append("## 委派失败率")
    lines.append(f"- {_pct(dfr['rate'])}({dfr['numerator']}/{dfr['denominator']})")
    lines.append(
        "- 口径:`delegation_runs.state IN ('failed','interrupted')` / "
        "`state IN ('completed','failed','interrupted')`(排除仍在跑的 "
        "running 和显式停止的 cancelled)。"
    )
    state_counts = dfr["state_counts"]
    if state_counts:
        lines.append(
            "- 状态分布:" + ", ".join(f"{k}={v}" for k, v in sorted(state_counts.items()))
        )
    lines.extend(_sql_block(dfr["sql"]))
    lines.append("")

    cpt = data["cost_per_task"]
    lines.append("## 每任务成本(仅 backend='claude-code')")
    lines.append(
        f"- 均值 {_money(cpt['mean'])} · 中位数 {_money(cpt['median'])} · "
        f"覆盖 {cpt['task_count']} 个任务 · 合计 {_money(cpt['total'])}"
    )
    lines.append(
        "- codex 的 cost 恒为 NULL、total_tokens 是线程累计值而非单 turn 增量"
        "(multiagent-field-report-data.md §2.4),不计入。"
    )
    lines.extend(_sql_block(cpt["sql"]))
    lines.append("")

    sfr = data["skill_failure_rates"]
    lines.append("## 技能涉败率(当前活跃已批准技能,每技能一行)")
    lines.append(
        "- 口径与 `server/skill_health.py` 完全一致:decisive = "
        "completed/failed/blocked,涉败 = failed/blocked,interrupted/"
        "cancelled/running 两边都不计入。涉败率 ≥50% 且可判定调用 ≥3 的技能"
        "已经/将会在复核队列里被自动举手候补标记,此处只是只读展示。"
    )
    if sfr:
        lines.append("")
        lines.append("| slug | title | 可判定调用 | 涉败 | 涉败率 |")
        lines.append("|---|---|---|---|---|")
        for row in sfr:
            lines.append(
                f"| {row['slug']} | {row['title']} | {row['eligible']} | "
                f"{row['fail_like']} | {_pct(row['rate'])} |"
            )
    else:
        lines.append("- (当前没有活跃的已批准技能)")
    lines.extend(_sql_block(SKILL_FAILURE_RATE_METHODOLOGY))
    lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True, help="Path to the LIVE production owlery.db")
    parser.add_argument(
        "--out-dir", default="docs/reports/process-metrics",
        help="Directory the dated markdown report is written into",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db_path)
    if not db_path.is_file():
        parser.error(f"--db-path does not exist or is not a file: {db_path}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="process-metrics-") as tmp:
        snapshot_path = str(Path(tmp) / "snapshot.db")
        snapshot(str(db_path), snapshot_path)
        conn = sqlite3.connect(snapshot_path)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                print(f"snapshot failed integrity check: {integrity}", file=sys.stderr)
                return 1
            data = compute_all(conn)
        finally:
            conn.close()

    markdown = render_markdown(data)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = out_dir / f"{stamp}.md"
    out_path.write_text(markdown, encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
