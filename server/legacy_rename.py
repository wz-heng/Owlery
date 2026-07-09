"""One-shot Octopus → Owlery migration (docs/plans/rename-owlery.md §3).

The app used to keep its state in `~/.octopus` and its database in
`octopus.db`. Both were renamed. An existing install must cross over
losslessly on the first boot of the renamed build, so this module:

1. Moves `octopus.db` → `owlery.db` and `~/.octopus` → `~/.owlery`
   (`migrate_legacy_state`, called *before* anything opens the DB or provisions
   agent dirs), dropping a marker file in the new home to record that the home
   move happened. The two moves are INDEPENDENT: an install that never used
   attachments, forks or research has no `~/.octopus` (it's created lazily) but
   still has an `octopus.db` holding every session.
2. Rewrites the absolute paths the DB stores into the old tree — fork working
   dirs, the JSON blobs hanging off them, bg-task working dirs, research
   report paths — plus the Claude project directories those fork working dirs
   are keyed by (`rewrite_legacy_paths`, called after `Database.initialize`).

Both steps are idempotent: once the old tree is gone and no stored path
carries the old prefix, re-running them does nothing.

The marker gates step 2. If a user somehow has BOTH a populated `~/.owlery`
and a `~/.octopus`, we cannot tell which is live — and booting would pick one
and fork the install in two, so `AmbiguousLegacyStateError` aborts startup and
asks for a manual merge.

**Message transcripts are deliberately left alone.** `messages.content` /
`tool_input` are a verbatim record of what was said in a past turn, not live
pointers — rewriting them would falsify history to fix nothing. The one
consequence is that a large-prompt spill pointer in an old turn names a path
that has moved; that file was already read into the model's context when the
turn ran.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# Dropped into the new home when the move runs. Its presence is what licenses
# the DB path rewrite — see the module docstring.
MARKER_NAME = ".migrated-from-octopus"

# Every DB location that persists an absolute filesystem path. Kept explicit
# (rather than "scan all TEXT columns") so adding a path-bearing column is a
# conscious decision to add it here too.
#
#   sessions.working_dir        — `/fork` copies live under <home>/fork/
#   sessions.fork_metadata      — JSON: side_effect_summary.agent_touched_paths
#   sessions.fork_revert_record — JSON: files[] stashed by the safe-revert
#   bg_tasks.working_dir        — cwd a background command was spawned in
#   research_jobs.report_path   — <home>/research/<job_id>.md
#
# Not listed, because the path is derived at use time from settings rather than
# stored: attachment paths (`attachments_dir/<session_id>/<id><ext>`), Codex
# credential homes (`codex_home_dir/<credential_id>/`), large-prompt spills.
_PLAIN_PATH_COLUMNS = (
    ("sessions", "id", "working_dir"),
    ("bg_tasks", "id", "working_dir"),
    ("research_jobs", "id", "report_path"),
)
_JSON_PATH_COLUMNS = (
    ("sessions", "id", "fork_metadata"),
    ("sessions", "id", "fork_revert_record"),
)


def _norm(p: str) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(p)))


def _is_empty_dir(p: Path) -> bool:
    return p.is_dir() and not any(p.iterdir())


def _under(path: str, prefix: str) -> bool:
    """True iff `path` is `prefix` itself or lives beneath it."""
    return path == prefix or path.startswith(prefix + os.sep)


def _reprefix(value: str, old: str, new: str) -> str:
    """Swap a leading `old` path prefix for `new`, on a normalized comparison
    but preserving the rest of the string verbatim."""
    normalized = _norm(value)
    if not _under(normalized, old):
        return value
    return new + normalized[len(old) :]


def _legacy_db_path(db_path: str) -> str | None:
    """The pre-rename name for `db_path`: same directory, `owlery` in the
    basename swapped back to `octopus` (so `owlery-dev.db` →
    `octopus-dev.db`). None when the name doesn't participate — an in-memory
    DB, or a path the user named something else entirely."""
    if db_path == ":memory:" or not db_path:
        return None
    head, tail = os.path.split(db_path)
    if "owlery" not in tail:
        return None
    return os.path.join(head, tail.replace("owlery", "octopus"))


# --------------------------------------------------------------- step 1: files


class AmbiguousLegacyStateError(RuntimeError):
    """Both homes hold live state. Raised rather than logged: continuing would
    boot against one of them and silently fork the user's install in two."""


def migrate_legacy_state(settings) -> bool:
    """Move the pre-rename home directory and database file into place.

    The two moves are INDEPENDENT. An install that never used attachments,
    forks or research has no `~/.octopus` at all — its home dir is created
    lazily — yet its `octopus.db` holds every session. Gating the database
    move on the home dir existing would strand that history and boot the user
    into an empty database (Snape review, blocker #1).

    Returns True iff the new home now carries the migration marker — i.e. the
    home move ran, now or on an earlier boot — which is what
    `rewrite_legacy_paths` keys off. Must run before anything opens the DB or
    provisions agent dirs.

    Raises `AmbiguousLegacyStateError` when both homes hold live state.
    """
    if not settings.legacy_home_dir:
        return False

    old_home = Path(_norm(settings.legacy_home_dir))
    new_home = Path(_norm(settings.home_dir))
    if old_home == new_home:
        return False

    # The database moves on its own terms, whatever the home dirs look like.
    _migrate_db_file(settings.db_path)

    marker = new_home / MARKER_NAME

    if not old_home.is_dir():
        # No old home to move. A marker from a previous boot still licenses the
        # (idempotent) path rewrite.
        return marker.exists()

    if marker.exists():
        # Migrated already; `old_home` is a leftover the user can delete.
        logger.info(
            "Owlery: %s was migrated to %s previously; %s still exists and can "
            "be removed.", old_home, new_home, old_home,
        )
        return True

    if new_home.exists() and not _is_empty_dir(new_home):
        # Refuse to guess which tree is live — and refuse to BOOT, because
        # continuing would open a database against one of them and fork the
        # install in two (Snape review, blocker #2).
        raise AmbiguousLegacyStateError(
            f"Both {old_home} and {new_home} exist and neither is empty. "
            f"Owlery cannot tell which holds your live state, and starting "
            f"would silently pick one. Merge them by hand, then remove "
            f"{old_home}. To skip this check entirely, set "
            f"OWLERY_LEGACY_HOME_DIR='' (the old tree is then ignored)."
        )

    if new_home.exists():
        new_home.rmdir()  # empty placeholder — get out of `move`'s way

    new_home.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old_home), str(new_home))
    marker.write_text(
        "This directory was migrated from ~/.octopus when the project was "
        "renamed to Owlery. Deleting this file is safe once no stored path "
        "still points into the old location.\n",
        encoding="utf-8",
    )
    logger.info("Owlery: migrated app home %s -> %s", old_home, new_home)
    return True


def _migrate_db_file(db_path: str) -> None:
    """Rename `octopus.db` → `owlery.db`. Skipped when the destination already
    exists — a live DB is never clobbered.

    The WAL is folded back into the main file first, then the sidecars are
    dropped. Moving `-wal`/`-shm` alongside the main file as three separate
    renames is not crash-safe: a crash after the main file lands but before its
    WAL does would silently discard every committed-but-uncheckpointed
    transaction. `TRUNCATE` forces a full checkpoint, after which the main file
    is self-contained and the sidecars carry nothing (SQLite recreates them on
    next open).
    """
    legacy = _legacy_db_path(db_path)
    if legacy is None or not os.path.isfile(legacy) or os.path.exists(db_path):
        return

    try:
        conn = sqlite3.connect(legacy)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.Error:
        # Not a WAL database, or an unreadable one. Either way the sidecars
        # below are dealt with the same; a corrupt file is the user's to fix.
        logger.exception("Owlery: could not checkpoint %s before migrating", legacy)

    os.replace(legacy, db_path)
    for suffix in ("-wal", "-shm"):
        # Post-checkpoint these are empty/stale. Leaving a `-wal` next to the
        # OLD name is harmless; leaving one that SQLite would pair with the new
        # name is not, so remove rather than move.
        try:
            os.unlink(legacy + suffix)
        except FileNotFoundError:
            pass
    logger.info("Owlery: migrated database %s -> %s", legacy, db_path)


# ----------------------------------------------------------------- step 2: DB


async def rewrite_legacy_paths(db, settings) -> int:
    """Rewrite every stored absolute path that still points into the old home,
    and re-key the Claude project directories the moved fork working dirs are
    addressed by. Returns the number of DB values changed.

    Safe to call on every boot: without the marker, or once no stored path
    carries the old prefix, it does nothing.
    """
    if not settings.legacy_home_dir:
        return 0
    old = _norm(settings.legacy_home_dir)
    new = _norm(settings.home_dir)
    if old == new or not os.path.exists(os.path.join(new, MARKER_NAME)):
        return 0

    conn = db._conn
    changed = 0
    moved_working_dirs: list[tuple[str, str]] = []

    for table, key, column in _PLAIN_PATH_COLUMNS:
        rows = await _fetch(conn, table, key, column)
        for row_id, value in rows:
            rewritten = _reprefix(value, old, new)
            if rewritten == value:
                continue
            await conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE {key} = ?",  # noqa: S608
                (rewritten, row_id),
            )
            changed += 1
            if (table, column) == ("sessions", "working_dir"):
                moved_working_dirs.append((value, rewritten))

    for table, key, column in _JSON_PATH_COLUMNS:
        rows = await _fetch(conn, table, key, column)
        for row_id, value in rows:
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError):
                continue  # not our JSON; leave it untouched
            rewritten = _rewrite_json(decoded, old, new)
            if rewritten == decoded:
                continue
            await conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE {key} = ?",  # noqa: S608
                (json.dumps(rewritten), row_id),
            )
            changed += 1

    await conn.commit()

    for old_wd, new_wd in moved_working_dirs:
        _move_claude_project_dir(old_wd, new_wd)

    if changed:
        logger.info("Owlery: rewrote %d stored path(s) %s -> %s", changed, old, new)
    return changed


async def _fetch(conn, table: str, key: str, column: str) -> list[tuple[str, str]]:
    """Non-null values of `column`, with their row keys. Tolerates a table that
    doesn't exist yet (a DB created before that feature landed)."""
    try:
        cursor = await conn.execute(
            f"SELECT {key}, {column} FROM {table} "  # noqa: S608
            f"WHERE {column} IS NOT NULL AND {column} != ''"
        )
    except Exception:
        return []
    return [(row[0], row[1]) for row in await cursor.fetchall()]


def _rewrite_json(node, old: str, new: str):
    """Structure-preserving walk: every string that resolves under `old` gets
    re-prefixed. Keys are left alone — no schema here uses a path as a key."""
    if isinstance(node, str):
        return _reprefix(node, old, new)
    if isinstance(node, list):
        return [_rewrite_json(v, old, new) for v in node]
    if isinstance(node, dict):
        return {k: _rewrite_json(v, old, new) for k, v in node.items()}
    return node


def _move_claude_project_dir(old_wd: str, new_wd: str) -> None:
    """Claude keys each session transcript by a slug of its working dir, so a
    fork whose working dir just moved would resume into an empty project and
    silently lose its history. Re-key the directory and rewrite the `cwd` each
    transcript line records, exactly as the fork-copy path does.

    Best-effort: a missing source, or a destination that already exists, means
    there's nothing to do. Never raises — a failure here degrades a fork's
    resume, it must not block startup.
    """
    from .harness.claude_code import _claude_project_dir

    try:
        src = _claude_project_dir(old_wd)
        dest = _claude_project_dir(new_wd)
        if not src.is_dir() or dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        for transcript in dest.glob("*.jsonl"):
            _rewrite_transcript_cwd(transcript, old_wd, new_wd)
        logger.info("Owlery: re-keyed Claude transcripts %s -> %s", src, dest)
    except Exception:
        logger.exception(
            "Owlery: could not re-key Claude transcripts for %s; that fork may "
            "resume without its history", new_wd,
        )


def _rewrite_transcript_cwd(path: Path, old_wd: str, new_wd: str) -> None:
    """Swap the `cwd` recorded on each JSONL line. Written to a temp file and
    renamed atomically so a crash can't leave a half-written transcript that
    still looks resumable."""
    tmp = path.with_suffix(".jsonl.tmp")
    try:
        with path.open(encoding="utf-8") as fin, tmp.open("w", encoding="utf-8") as fout:
            for line in fin:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except ValueError:
                    fout.write(line)
                    continue
                if isinstance(record, dict) and record.get("cwd") == old_wd:
                    record["cwd"] = new_wd
                fout.write(json.dumps(record) + "\n")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
