#!/usr/bin/env python3
"""CLI entrypoint for the skill co-occurrence health check
(docs/plans/process-metrics.md §3.3; logic lives in `server/skill_health.py`
as real product code, tested by `tests/test_skill_health.py`).

Unlike the process-metrics report, this DOES write to the live production
DB — but only ever into `skill_candidates`, as a new `pending` row in the
SAME human-reviewed queue every hand-authored skill proposal already goes
through (`server/skill_registry.py`'s `propose()`/`approve()`/`reject()`).
No table, UI, or write path is new; this is the one call site that invokes
the detector.

Connects via `Database.connect_existing_without_migrating()`, NOT the
normal `Database.initialize()` — the latter runs the full schema-creation
and one-time catch-up migration pass (including destructive steps like
purging legacy Telegram bridge rows), which has no business running a
second time against a file a live `owlery serve` process already migrated
on its own startup (Snape review: calling `initialize()` here violated the
"one write point" design intent in §3.3 above).

    .venv/bin/python scripts/process_metrics/skill_backfill_cli.py \\
        --db-path /path/to/owlery.db

Safe to run as often as you like — idempotent per skill lineage while a
prior auto-flag on it is still pending review (see `AUTO_FLAG_MARKER` in
`server/skill_health.py`).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.database import Database  # noqa: E402
from server.skill_health import detect_and_flag_corroded_skills  # noqa: E402


async def _run(db_path: str) -> list[dict]:
    db = Database(db_path)
    await db.connect_existing_without_migrating()
    try:
        return await detect_and_flag_corroded_skills(db)
    finally:
        await db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True, help="Path to the LIVE production owlery.db")
    args = parser.parse_args(argv)

    db_path = Path(args.db_path)
    if not db_path.is_file():
        parser.error(f"--db-path does not exist or is not a file: {db_path}")

    created = asyncio.run(_run(str(db_path)))
    if not created:
        print("no skill crossed the co-occurrence threshold; nothing flagged")
    for row in created:
        print(f"flagged {row['slug']!r} -> candidate {row['id']} (pending review)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
