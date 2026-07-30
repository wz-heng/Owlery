"""Transactional persistence for the durable Task Board.

This is intentionally a second SQLite connection.  Owlery's general Database
connection is shared by many collaborators, so a Python sequence of two
``execute`` calls is not an isolation boundary.  Every graph/lifecycle write
here is serialized per repository and wrapped in ``BEGIN IMMEDIATE``; separate
repository instances still race safely at SQLite's WAL writer boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .models import (
    ACTOR_KINDS,
    BLOCKED_KINDS,
    DELIVERY_EXTERNAL_OP_KINDS,
    DELIVERY_OP_KINDS,
    DELIVERY_REASON_KINDS,
    DELIVERY_RETENTIONS,
    DELIVERY_STATUSES,
    WORKSPACE_MODES,
    ArtifactRecord,
    BoardRecord,
    CommentRecord,
    DeliveryOpRecord,
    DeliveryRecord,
    DependencyRecord,
    EventRecord,
    RunRecord,
    TaskCapacityError,
    TaskConflictError,
    TaskNotFoundError,
    TaskRecord,
    TaskValidationError,
)

_DELIVERY_TERMINAL_STATUSES = frozenset(
    {"delivered", "conflicted", "blocked", "failed"}
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_id() -> str:
    return uuid.uuid4().hex[:12]


def _json_object(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TaskValidationError("metadata/payload must be a JSON object")
    try:
        return json.dumps(dict(value), separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise TaskValidationError("metadata/payload must be JSON serializable") from exc


def _load_object(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else {}


class TaskRepository:
    """Sole writer for task-board tables.

    ``bind`` is separate from ``initialize`` so the application singleton can
    be imported without opening files at module import time.  Tests and race
    checks may construct multiple instances against the same file.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    def bind(self, db_path: str) -> None:
        if self._conn is not None:
            raise RuntimeError("cannot rebind an initialized TaskRepository")
        self._db_path = db_path

    async def initialize(self) -> None:
        if self._conn is not None:
            return
        if not self._db_path:
            raise RuntimeError("bind(db_path) before TaskRepository.initialize()")
        if self._db_path == ":memory:":
            raise ValueError("TaskRepository requires a file database")
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("TaskRepository is not initialized")
        return self._conn

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            conn = self.conn
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()

    async def _fetchone(
        self, conn: aiosqlite.Connection, sql: str, args: Sequence[Any] = ()
    ) -> aiosqlite.Row | None:
        cursor = await conn.execute(sql, tuple(args))
        return await cursor.fetchone()

    async def _fetchall(
        self, conn: aiosqlite.Connection, sql: str, args: Sequence[Any] = ()
    ) -> list[aiosqlite.Row]:
        cursor = await conn.execute(sql, tuple(args))
        return list(await cursor.fetchall())

    async def _task_row(
        self, conn: aiosqlite.Connection, task_id: str
    ) -> aiosqlite.Row:
        row = await self._fetchone(conn, "SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise TaskNotFoundError(f"task {task_id!r} not found")
        return row

    async def _board_row(
        self, conn: aiosqlite.Connection, board_id: str
    ) -> aiosqlite.Row:
        row = await self._fetchone(
            conn, "SELECT * FROM task_boards WHERE id = ?", (board_id,)
        )
        if row is None:
            raise TaskNotFoundError(f"task board {board_id!r} not found")
        return row

    async def _run_row(self, conn: aiosqlite.Connection, run_id: str) -> aiosqlite.Row:
        row = await self._fetchone(conn, "SELECT * FROM task_runs WHERE id = ?", (run_id,))
        if row is None:
            raise TaskNotFoundError(f"task run {run_id!r} not found")
        return row

    @staticmethod
    def _run_record(row: Mapping[str, Any]) -> RunRecord:
        values = dict(row)
        values["metadata"] = _load_object(values["metadata"])
        return RunRecord(**values)

    @staticmethod
    def _event_record(row: Mapping[str, Any]) -> EventRecord:
        values = dict(row)
        values["payload"] = _load_object(values["payload"]) or {}
        return EventRecord(**values)

    async def _event(
        self,
        conn: aiosqlite.Connection,
        *,
        task: Mapping[str, Any],
        kind: str,
        actor_kind: str,
        actor_agent_id: str | None = None,
        run_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        now: str | None = None,
    ) -> None:
        if actor_kind not in ACTOR_KINDS:
            raise TaskValidationError(f"invalid actor kind: {actor_kind}")
        await conn.execute(
            "INSERT INTO task_events "
            "(board_id, task_id, run_id, kind, actor_kind, actor_agent_id, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task["board_id"],
                task["id"],
                run_id,
                kind,
                actor_kind,
                actor_agent_id,
                _json_object(payload) or "{}",
                now or _now_iso(),
            ),
        )

    async def _board_event(
        self,
        conn: aiosqlite.Connection,
        *,
        board: Mapping[str, Any],
        kind: str,
        actor_kind: str = "user",
        payload: Mapping[str, Any] | None = None,
        now: str | None = None,
    ) -> None:
        if actor_kind not in ACTOR_KINDS:
            raise TaskValidationError(f"invalid actor kind: {actor_kind}")
        await conn.execute(
            "INSERT INTO task_events "
            "(board_id, task_id, run_id, kind, actor_kind, payload, created_at) "
            "VALUES (?, NULL, NULL, ?, ?, ?, ?)",
            (
                board["id"],
                kind,
                actor_kind,
                _json_object(payload) or "{}",
                now or _now_iso(),
            ),
        )

    @staticmethod
    def _validated_dir(value: str, *, must_exist: bool = True) -> str:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise TaskValidationError("working directory must be absolute")
        try:
            resolved = str(path.resolve(strict=must_exist))
        except OSError as exc:
            raise TaskValidationError(
                "working directory must be an existing directory"
            ) from exc
        if must_exist and not Path(resolved).is_dir():
            raise TaskValidationError("working directory must be an existing directory")
        return resolved

    @staticmethod
    def _positive_limit(value: int | None, name: str) -> None:
        if value is not None and (isinstance(value, bool) or value <= 0):
            raise TaskValidationError(f"{name} must be a positive integer or null")

    async def _agent_is_live(
        self, conn: aiosqlite.Connection, agent_id: str | None
    ) -> bool:
        if not agent_id:
            return False
        row = await self._fetchone(
            conn, "SELECT 1 FROM agents WHERE id = ? AND archived = 0", (agent_id,)
        )
        return row is not None

    async def _eligible(
        self, conn: aiosqlite.Connection, task: Mapping[str, Any], now: str
    ) -> bool:
        if task["archived"] or not await self._agent_is_live(
            conn, task["assignee_agent_id"]
        ):
            return False
        board = await self._board_row(conn, task["board_id"])
        if board["archived"]:
            return False
        scheduled = task["scheduled_at"]
        if scheduled is not None and scheduled > now:
            return False
        pending = await self._fetchone(
            conn,
            "SELECT 1 FROM task_dependencies d JOIN tasks dep "
            "ON dep.id = d.depends_on_task_id "
            "WHERE d.task_id = ? AND dep.status != 'done' LIMIT 1",
            (task["id"],),
        )
        return pending is None

    async def _recompute_one(
        self,
        conn: aiosqlite.Connection,
        task_id: str,
        *,
        actor_kind: str = "system",
        actor_agent_id: str | None = None,
        now: str | None = None,
    ) -> aiosqlite.Row:
        task = await self._task_row(conn, task_id)
        if task["status"] not in ("todo", "ready"):
            return task
        stamp = now or _now_iso()
        wanted = "ready" if await self._eligible(conn, task, stamp) else "todo"
        if wanted == task["status"]:
            return task
        await conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (wanted, stamp, task_id),
        )
        updated = await self._task_row(conn, task_id)
        await self._event(
            conn,
            task=updated,
            kind="task_status_changed",
            actor_kind=actor_kind,
            actor_agent_id=actor_agent_id,
            payload={"from": task["status"], "to": wanted, "reason": "eligibility"},
            now=stamp,
        )
        return updated

    # ---------------------------------------------------------------- boards

    async def create_board(
        self,
        *,
        name: str,
        working_dir: str,
        description: str = "",
        default_workspace_mode: str = "shared",
        max_running: int | None = None,
        max_running_per_agent: int | None = None,
        max_tree_depth: int = 8,
        max_children_per_run: int = 32,
        max_open_tasks: int = 500,
        dispatch_enabled: bool = True,
        git_delivery_remote: str = "origin",
        git_delivery_retention: str = "keep",
        git_delivery_author_name: str = "Owlery Task",
        git_delivery_author_email: str = "owlery-tasks@localhost",
        git_delivery_default_draft_pr: bool = True,
        git_delivery_default_merge: str = "none",
        allow_local_deploy: bool = False,
        board_id: str | None = None,
    ) -> BoardRecord:
        clean_name = name.strip()
        if not clean_name:
            raise TaskValidationError("board name is required")
        if default_workspace_mode not in WORKSPACE_MODES:
            raise TaskValidationError("invalid default workspace mode")
        if git_delivery_retention not in DELIVERY_RETENTIONS:
            raise TaskValidationError("invalid Git delivery retention")
        if git_delivery_default_merge not in {"none", "fast_forward_only"}:
            raise TaskValidationError("invalid default Git delivery merge strategy")
        git_delivery_remote = git_delivery_remote.strip()
        git_delivery_author_name = git_delivery_author_name.strip()
        git_delivery_author_email = git_delivery_author_email.strip()
        if not git_delivery_remote:
            raise TaskValidationError("Git delivery remote is required")
        if not git_delivery_author_name or not git_delivery_author_email:
            raise TaskValidationError("Git delivery author name and email are required")
        for value, label in (
            (max_running, "max_running"),
            (max_running_per_agent, "max_running_per_agent"),
            (max_tree_depth, "max_tree_depth"),
            (max_children_per_run, "max_children_per_run"),
            (max_open_tasks, "max_open_tasks"),
        ):
            self._positive_limit(value, label)
        stamp = _now_iso()
        ident = board_id or _short_id()
        path = self._validated_dir(working_dir)
        async with self._transaction() as conn:
            try:
                await conn.execute(
                    "INSERT INTO task_boards "
                    "(id,name,description,working_dir,default_workspace_mode,max_running,"
                    "max_running_per_agent,max_tree_depth,max_children_per_run,max_open_tasks,"
                    "dispatch_enabled,git_delivery_remote,git_delivery_retention,"
                    "git_delivery_author_name,git_delivery_author_email,"
                    "git_delivery_default_draft_pr,git_delivery_default_merge,"
                    "allow_local_deploy,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        ident,
                        clean_name,
                        description,
                        path,
                        default_workspace_mode,
                        max_running,
                        max_running_per_agent,
                        max_tree_depth,
                        max_children_per_run,
                        max_open_tasks,
                        int(dispatch_enabled),
                        git_delivery_remote,
                        git_delivery_retention,
                        git_delivery_author_name,
                        git_delivery_author_email,
                        int(bool(git_delivery_default_draft_pr)),
                        git_delivery_default_merge,
                        int(bool(allow_local_deploy)),
                        stamp,
                        stamp,
                    ),
                )
            except aiosqlite.IntegrityError as exc:
                raise TaskConflictError(f"live board name already exists: {clean_name}") from exc
            row = await self._board_row(conn, ident)
            await self._board_event(
                conn, board=row, kind="board_created", payload={"name": clean_name}, now=stamp
            )
        return BoardRecord.from_row(row)

    async def get_board(self, board_id: str) -> BoardRecord:
        async with self._lock:
            return BoardRecord.from_row(await self._board_row(self.conn, board_id))

    async def list_boards(self, *, include_archived: bool = False) -> list[BoardRecord]:
        sql = "SELECT * FROM task_boards"
        if not include_archived:
            sql += " WHERE archived = 0"
        sql += " ORDER BY created_at, id"
        async with self._lock:
            rows = await self._fetchall(self.conn, sql)
        return [BoardRecord.from_row(row) for row in rows]

    async def update_board(
        self, board_id: str, *, expected_updated_at: str | None = None, **updates: Any
    ) -> BoardRecord:
        allowed = {
            "name",
            "description",
            "working_dir",
            "default_workspace_mode",
            "max_running",
            "max_running_per_agent",
            "max_tree_depth",
            "max_children_per_run",
            "max_open_tasks",
            "git_delivery_remote",
            "git_delivery_retention",
            "git_delivery_author_name",
            "git_delivery_author_email",
            "git_delivery_default_draft_pr",
            "git_delivery_default_merge",
            "allow_local_deploy",
        }
        unknown = set(updates) - allowed
        if unknown:
            raise TaskValidationError(f"unsupported board fields: {sorted(unknown)}")
        if not updates:
            return await self.get_board(board_id)
        if "name" in updates:
            updates["name"] = str(updates["name"]).strip()
            if not updates["name"]:
                raise TaskValidationError("board name is required")
        if "working_dir" in updates:
            updates["working_dir"] = self._validated_dir(str(updates["working_dir"]))
        if "default_workspace_mode" in updates and updates["default_workspace_mode"] not in WORKSPACE_MODES:
            raise TaskValidationError("invalid default workspace mode")
        if (
            "git_delivery_retention" in updates
            and updates["git_delivery_retention"] not in DELIVERY_RETENTIONS
        ):
            raise TaskValidationError("invalid Git delivery retention")
        if (
            "git_delivery_default_merge" in updates
            and updates["git_delivery_default_merge"]
            not in {"none", "fast_forward_only"}
        ):
            raise TaskValidationError("invalid default Git delivery merge strategy")
        for field in (
            "git_delivery_remote",
            "git_delivery_author_name",
            "git_delivery_author_email",
        ):
            if field in updates:
                updates[field] = str(updates[field]).strip()
                if not updates[field]:
                    raise TaskValidationError(f"{field} is required")
        if "git_delivery_default_draft_pr" in updates:
            updates["git_delivery_default_draft_pr"] = int(
                bool(updates["git_delivery_default_draft_pr"])
            )
        if "allow_local_deploy" in updates:
            updates["allow_local_deploy"] = int(bool(updates["allow_local_deploy"]))
        for field in (
            "max_running",
            "max_running_per_agent",
            "max_tree_depth",
            "max_children_per_run",
            "max_open_tasks",
        ):
            if field in updates:
                self._positive_limit(updates[field], field)
        stamp = _now_iso()
        async with self._transaction() as conn:
            current = await self._board_row(conn, board_id)
            if expected_updated_at is not None and current["updated_at"] != expected_updated_at:
                raise TaskConflictError("board was modified by another caller")
            sets = ", ".join(f"{key} = ?" for key in updates)
            try:
                await conn.execute(
                    f"UPDATE task_boards SET {sets}, updated_at = ? WHERE id = ?",
                    (*updates.values(), stamp, board_id),
                )
            except aiosqlite.IntegrityError as exc:
                raise TaskConflictError("live board name already exists") from exc
            # Board edits may change task eligibility.
            task_rows = await self._fetchall(
                conn,
                "SELECT id FROM tasks WHERE board_id = ? AND status IN ('todo','ready')",
                (board_id,),
            )
            for task in task_rows:
                await self._recompute_one(conn, task["id"], now=stamp)
            row = await self._board_row(conn, board_id)
            await self._board_event(
                conn,
                board=row,
                kind="board_updated",
                payload={"fields": sorted(updates)},
                now=stamp,
            )
        return BoardRecord.from_row(row)

    async def set_dispatch_enabled(
        self, board_id: str, enabled: bool
    ) -> BoardRecord:
        stamp = _now_iso()
        async with self._transaction() as conn:
            await self._board_row(conn, board_id)
            await conn.execute(
                "UPDATE task_boards SET dispatch_enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), stamp, board_id),
            )
            row = await self._board_row(conn, board_id)
            await self._board_event(
                conn,
                board=row,
                kind="dispatcher_resumed" if enabled else "dispatcher_paused",
                payload={"enabled": enabled},
                now=stamp,
            )
        return BoardRecord.from_row(row)

    async def archive_board(self, board_id: str, *, archived: bool = True) -> BoardRecord:
        stamp = _now_iso()
        async with self._transaction() as conn:
            board = await self._board_row(conn, board_id)
            if archived:
                running = await self._fetchone(
                    conn,
                    "SELECT 1 FROM tasks WHERE board_id = ? AND status = 'running' LIMIT 1",
                    (board_id,),
                )
                if running:
                    raise TaskConflictError("cannot archive a board with running tasks")
            await conn.execute(
                "UPDATE task_boards SET archived = ?, updated_at = ? WHERE id = ?",
                (int(archived), stamp, board_id),
            )
            task_rows = await self._fetchall(
                conn,
                "SELECT id FROM tasks WHERE board_id = ? AND status IN ('todo','ready')",
                (board_id,),
            )
            for task in task_rows:
                await self._recompute_one(conn, task["id"], now=stamp)
            row = await self._board_row(conn, board_id)
            await self._board_event(
                conn,
                board=row,
                kind="board_unarchived" if not archived else "board_archived",
                payload={"archived": archived},
                now=stamp,
            )
        return BoardRecord.from_row(row)

    # ----------------------------------------------------------------- tasks

    async def _tree_depth(
        self, conn: aiosqlite.Connection, task_id: str
    ) -> int:
        row = await self._fetchone(
            conn,
            "WITH RECURSIVE ancestors(id, depth) AS ("
            " SELECT id, 1 FROM tasks WHERE id = ?"
            " UNION ALL"
            " SELECT p.id, ancestors.depth + 1 FROM tasks p"
            " JOIN ancestors ON p.id = (SELECT parent_task_id FROM tasks WHERE id = ancestors.id)"
            ") SELECT COALESCE(MAX(depth), 0) AS depth FROM ancestors",
            (task_id,),
        )
        return int(row["depth"])

    async def _subtree_height(
        self, conn: aiosqlite.Connection, task_id: str
    ) -> int:
        row = await self._fetchone(
            conn,
            "WITH RECURSIVE descendants(id, depth) AS ("
            " SELECT id, 1 FROM tasks WHERE id = ?"
            " UNION ALL SELECT t.id, descendants.depth + 1 FROM tasks t"
            " JOIN descendants ON t.parent_task_id = descendants.id"
            ") SELECT COALESCE(MAX(depth), 0) AS height FROM descendants",
            (task_id,),
        )
        return int(row["height"])

    async def _validate_parent(
        self,
        conn: aiosqlite.Connection,
        *,
        board: Mapping[str, Any],
        parent_task_id: str | None,
        moving_task_id: str | None = None,
    ) -> None:
        if parent_task_id is None:
            return
        parent = await self._task_row(conn, parent_task_id)
        if parent["board_id"] != board["id"]:
            raise TaskValidationError("task tree links cannot cross boards")
        if moving_task_id == parent_task_id:
            raise TaskValidationError("a task cannot be its own parent")
        if moving_task_id:
            cycle = await self._fetchone(
                conn,
                "WITH RECURSIVE descendants(id) AS ("
                " SELECT id FROM tasks WHERE parent_task_id = ?"
                " UNION ALL SELECT t.id FROM tasks t JOIN descendants d"
                " ON t.parent_task_id = d.id"
                ") SELECT 1 FROM descendants WHERE id = ? LIMIT 1",
                (moving_task_id, parent_task_id),
            )
            if cycle:
                raise TaskConflictError("task parent would create a tree cycle")
            parent_depth = await self._tree_depth(conn, parent_task_id)
            subtree_height = await self._subtree_height(conn, moving_task_id)
            if parent_depth + subtree_height > board["max_tree_depth"]:
                raise TaskCapacityError("task tree depth limit exceeded")
        elif await self._tree_depth(conn, parent_task_id) + 1 > board["max_tree_depth"]:
            raise TaskCapacityError("task tree depth limit exceeded")

    async def create_task(
        self,
        *,
        board_id: str,
        title: str,
        body: str = "",
        status: str = "triage",
        parent_task_id: str | None = None,
        assignee_agent_id: str | None = None,
        priority: int = 0,
        origin_session_id: str | None = None,
        idempotency_key: str | None = None,
        scheduled_at: str | None = None,
        workspace_mode: str | None = None,
        working_dir_override: str | None = None,
        created_by_kind: str = "user",
        created_by_agent_id: str | None = None,
        creator_run_id: str | None = None,
        dependencies: Sequence[str] | None = None,
        task_id: str | None = None,
    ) -> TaskRecord:
        clean_title = title.strip()
        if not clean_title:
            raise TaskValidationError("task title is required")
        if status not in ("triage", "todo", "ready"):
            raise TaskValidationError("new tasks may only start in triage or specified state")
        if created_by_kind not in {"user", "agent", "schedule", "api"}:
            raise TaskValidationError("invalid task creator kind")
        if workspace_mode is not None and workspace_mode not in WORKSPACE_MODES:
            raise TaskValidationError("invalid workspace mode")
        if working_dir_override is not None:
            if created_by_kind == "agent":
                raise TaskValidationError("agent-created tasks must inherit the board path")
            working_dir_override = self._validated_dir(working_dir_override)
        ident = task_id or _short_id()
        stamp = _now_iso()
        async with self._transaction() as conn:
            board = await self._board_row(conn, board_id)
            if board["archived"]:
                raise TaskConflictError("cannot create tasks on an archived board")
            if idempotency_key is not None:
                existing = await self._fetchone(
                    conn,
                    "SELECT * FROM tasks WHERE board_id = ? AND idempotency_key = ?",
                    (board_id, idempotency_key),
                )
                if existing:
                    return TaskRecord.from_row(existing)
            open_row = await self._fetchone(
                conn,
                "SELECT COUNT(*) AS n FROM tasks WHERE board_id = ? "
                "AND archived = 0 AND status != 'done'",
                (board_id,),
            )
            if open_row["n"] >= board["max_open_tasks"]:
                raise TaskCapacityError("board open-task limit exceeded")
            await self._validate_parent(
                conn, board=board, parent_task_id=parent_task_id
            )
            dependency_ids = list(dict.fromkeys(dependencies or ()))
            if ident in dependency_ids:
                raise TaskValidationError("a task cannot depend on itself")
            dependency_rows: list[aiosqlite.Row] = []
            for dependency_id in dependency_ids:
                dependency = await self._task_row(conn, dependency_id)
                if dependency["board_id"] != board_id:
                    raise TaskValidationError("task dependencies cannot cross boards")
                dependency_rows.append(dependency)
            if assignee_agent_id is not None and not await self._agent_is_live(
                conn, assignee_agent_id
            ):
                raise TaskValidationError("assignee Agent does not exist or is archived")
            if creator_run_id is not None:
                creator = await self._run_row(conn, creator_run_id)
                creator_task = await self._task_row(conn, creator["task_id"])
                if creator["state"] != "running" or creator_task["board_id"] != board_id:
                    raise TaskConflictError("creator run is not active on this board")
                count = await self._fetchone(
                    conn,
                    "SELECT COUNT(*) AS n FROM task_events "
                    "WHERE run_id = ? AND kind = 'task_created'",
                    (creator_run_id,),
                )
                if count["n"] >= board["max_children_per_run"]:
                    raise TaskCapacityError("per-run task creation limit exceeded")
            initial = "todo" if status in ("todo", "ready") else "triage"
            try:
                await conn.execute(
                    "INSERT INTO tasks "
                    "(id,board_id,parent_task_id,title,body,status,assignee_agent_id,priority,"
                    "origin_session_id,idempotency_key,scheduled_at,workspace_mode,"
                    "working_dir_override,created_by_kind,created_by_agent_id,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        ident,
                        board_id,
                        parent_task_id,
                        clean_title,
                        body,
                        initial,
                        assignee_agent_id,
                        priority,
                        origin_session_id,
                        idempotency_key,
                        scheduled_at,
                        workspace_mode,
                        working_dir_override,
                        created_by_kind,
                        created_by_agent_id,
                        stamp,
                        stamp,
                    ),
                )
            except aiosqlite.IntegrityError as exc:
                raise TaskConflictError("task id or idempotency key already exists") from exc
            task = await self._task_row(conn, ident)
            await self._event(
                conn,
                task=task,
                kind="task_created",
                actor_kind=created_by_kind,
                actor_agent_id=created_by_agent_id,
                run_id=creator_run_id,
                payload={"status": initial},
                now=stamp,
            )
            for dependency in dependency_rows:
                await conn.execute(
                    "INSERT INTO task_dependencies "
                    "(task_id,depends_on_task_id,created_by_kind,created_by_agent_id,created_at) "
                    "VALUES (?,?,?,?,?)",
                    (
                        ident,
                        dependency["id"],
                        created_by_kind,
                        created_by_agent_id,
                        stamp,
                    ),
                )
                await self._event(
                    conn,
                    task=task,
                    kind="task_dependency_added",
                    actor_kind=created_by_kind,
                    actor_agent_id=created_by_agent_id,
                    run_id=creator_run_id,
                    payload={"depends_on_task_id": dependency["id"]},
                    now=stamp,
                )
            if initial == "todo":
                task = await self._recompute_one(
                    conn,
                    ident,
                    actor_kind=created_by_kind,
                    actor_agent_id=created_by_agent_id,
                    now=stamp,
                )
        return TaskRecord.from_row(task)

    async def create_task_result(self, **kwargs: Any) -> tuple[TaskRecord, bool]:
        """Create idempotently and report whether this call inserted the row."""
        proposed_id = kwargs.pop("task_id", None) or _short_id()
        record = await self.create_task(task_id=proposed_id, **kwargs)
        return record, record.id == proposed_id

    async def get_task(self, task_id: str) -> TaskRecord:
        async with self._lock:
            return TaskRecord.from_row(await self._task_row(self.conn, task_id))

    async def get_task_by_idempotency_key(
        self, board_id: str, idempotency_key: str
    ) -> TaskRecord | None:
        async with self._lock:
            row = await self._fetchone(
                self.conn,
                "SELECT * FROM tasks WHERE board_id = ? AND idempotency_key = ?",
                (board_id, idempotency_key),
            )
        return TaskRecord.from_row(row) if row else None

    async def list_tasks(
        self,
        *,
        board_id: str | None = None,
        status: str | None = None,
        assignee_agent_id: str | None = None,
        parent_task_id: str | None = None,
        root_only: bool = False,
        include_archived: bool = False,
        limit: int = 200,
    ) -> list[TaskRecord]:
        if limit < 1 or limit > 1000:
            raise TaskValidationError("limit must be between 1 and 1000")
        clauses: list[str] = []
        args: list[Any] = []
        if board_id is not None:
            clauses.append("board_id = ?")
            args.append(board_id)
        if status is not None:
            clauses.append("status = ?")
            args.append(status)
        if assignee_agent_id is not None:
            clauses.append("assignee_agent_id = ?")
            args.append(assignee_agent_id)
        if parent_task_id is not None:
            clauses.append("parent_task_id = ?")
            args.append(parent_task_id)
        elif root_only:
            clauses.append("parent_task_id IS NULL")
        if not include_archived:
            clauses.append("archived = 0")
        sql = "SELECT * FROM tasks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY priority DESC, created_at, id LIMIT ?"
        args.append(limit)
        async with self._lock:
            rows = await self._fetchall(self.conn, sql, args)
        return [TaskRecord.from_row(row) for row in rows]

    async def get_tree(
        self, board_id: str, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        """Return a stable nested tree; dependency edges remain separate."""
        tasks = await self.list_tasks(
            board_id=board_id, include_archived=include_archived, limit=1000
        )
        nodes = {task.id: {**task.to_dict(), "children": []} for task in tasks}
        roots: list[dict[str, Any]] = []
        for task in tasks:
            node = nodes[task.id]
            if task.parent_task_id and task.parent_task_id in nodes:
                nodes[task.parent_task_id]["children"].append(node)
            else:
                roots.append(node)
        return roots

    async def update_task(
        self, task_id: str, *, expected_updated_at: str | None = None, **updates: Any
    ) -> TaskRecord:
        allowed = {
            "title",
            "body",
            "priority",
            "scheduled_at",
            "workspace_mode",
            "working_dir_override",
            "origin_session_id",
            "parent_task_id",
        }
        unknown = set(updates) - allowed
        if unknown:
            raise TaskValidationError(
                f"status/assignment/tree changes require lifecycle methods: {sorted(unknown)}"
            )
        if not updates:
            return await self.get_task(task_id)
        if "title" in updates:
            updates["title"] = str(updates["title"]).strip()
            if not updates["title"]:
                raise TaskValidationError("task title is required")
        if "workspace_mode" in updates and updates["workspace_mode"] is not None:
            if updates["workspace_mode"] not in WORKSPACE_MODES:
                raise TaskValidationError("invalid workspace mode")
        if "working_dir_override" in updates and updates["working_dir_override"] is not None:
            updates["working_dir_override"] = self._validated_dir(
                str(updates["working_dir_override"])
            )
        stamp = _now_iso()
        async with self._transaction() as conn:
            task = await self._task_row(conn, task_id)
            if expected_updated_at is not None and task["updated_at"] != expected_updated_at:
                raise TaskConflictError(
                    "task was modified by another caller", current=TaskRecord.from_row(task)
                )
            if task["status"] in ("running", "done"):
                raise TaskConflictError("running/done tasks cannot be edited", current=TaskRecord.from_row(task))
            if "parent_task_id" in updates:
                board = await self._board_row(conn, task["board_id"])
                await self._validate_parent(
                    conn,
                    board=board,
                    parent_task_id=updates["parent_task_id"],
                    moving_task_id=task_id,
                )
            sets = ", ".join(f"{key} = ?" for key in updates)
            await conn.execute(
                f"UPDATE tasks SET {sets}, updated_at = ? WHERE id = ?",
                (*updates.values(), stamp, task_id),
            )
            task = await self._recompute_one(conn, task_id, now=stamp)
            await self._event(
                conn,
                task=task,
                kind="task_updated",
                actor_kind="user",
                payload={"fields": sorted(updates)},
                now=stamp,
            )
        return TaskRecord.from_row(task)

    async def set_parent(
        self,
        task_id: str,
        parent_task_id: str | None,
        *,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
    ) -> TaskRecord:
        stamp = _now_iso()
        async with self._transaction() as conn:
            task = await self._task_row(conn, task_id)
            if task["status"] in ("running", "done"):
                raise TaskConflictError("running/done tasks cannot move in the tree")
            board = await self._board_row(conn, task["board_id"])
            await self._validate_parent(
                conn,
                board=board,
                parent_task_id=parent_task_id,
                moving_task_id=task_id,
            )
            await conn.execute(
                "UPDATE tasks SET parent_task_id = ?, updated_at = ? WHERE id = ?",
                (parent_task_id, stamp, task_id),
            )
            updated = await self._task_row(conn, task_id)
            await self._event(
                conn,
                task=updated,
                kind="task_parent_changed",
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
                payload={"from": task["parent_task_id"], "to": parent_task_id},
                now=stamp,
            )
        return TaskRecord.from_row(updated)

    async def triage_task(
        self,
        task_id: str,
        *,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
    ) -> TaskRecord:
        stamp = _now_iso()
        async with self._transaction() as conn:
            task = await self._task_row(conn, task_id)
            if task["status"] not in ("triage", "todo", "ready"):
                raise TaskConflictError("only triage/todo/ready tasks may move to triage", current=TaskRecord.from_row(task))
            old = task["status"]
            if old != "triage":
                await conn.execute(
                    "UPDATE tasks SET status = 'triage', updated_at = ? WHERE id = ?",
                    (stamp, task_id),
                )
            updated = await self._task_row(conn, task_id)
            if old != "triage":
                await self._event(
                    conn,
                    task=updated,
                    kind="task_status_changed",
                    actor_kind=actor_kind,
                    actor_agent_id=actor_agent_id,
                    payload={"from": old, "to": "triage"},
                    now=stamp,
                )
        return TaskRecord.from_row(updated)

    async def specify_task(
        self,
        task_id: str,
        *,
        body: str | None = None,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
    ) -> TaskRecord:
        stamp = _now_iso()
        async with self._transaction() as conn:
            task = await self._task_row(conn, task_id)
            if task["status"] != "triage":
                raise TaskConflictError("only triage tasks may be specified", current=TaskRecord.from_row(task))
            if body is not None:
                await conn.execute(
                    "UPDATE tasks SET body = ?, status = 'todo', updated_at = ? WHERE id = ?",
                    (body, stamp, task_id),
                )
            else:
                await conn.execute(
                    "UPDATE tasks SET status = 'todo', updated_at = ? WHERE id = ?",
                    (stamp, task_id),
                )
            updated = await self._recompute_one(
                conn,
                task_id,
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
                now=stamp,
            )
            await self._event(
                conn,
                task=updated,
                kind="task_specified",
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
                payload={"to": updated["status"]},
                now=stamp,
            )
        return TaskRecord.from_row(updated)

    async def ready_task(
        self,
        task_id: str,
        *,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
    ) -> TaskRecord:
        stamp = _now_iso()
        async with self._transaction() as conn:
            task = await self._task_row(conn, task_id)
            if task["status"] not in ("todo", "ready"):
                raise TaskConflictError("only todo tasks may become ready", current=TaskRecord.from_row(task))
            if not await self._eligible(conn, task, stamp):
                raise TaskConflictError("task is not eligible to become ready", current=TaskRecord.from_row(task))
            if task["status"] != "ready":
                await conn.execute(
                    "UPDATE tasks SET status = 'ready', updated_at = ? WHERE id = ?",
                    (stamp, task_id),
                )
                task = await self._task_row(conn, task_id)
                await self._event(
                    conn,
                    task=task,
                    kind="task_status_changed",
                    actor_kind=actor_kind,
                    actor_agent_id=actor_agent_id,
                    payload={"from": "todo", "to": "ready"},
                    now=stamp,
                )
        return TaskRecord.from_row(task)

    async def assign_task(
        self,
        task_id: str,
        agent_id: str | None,
        *,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
    ) -> TaskRecord:
        stamp = _now_iso()
        async with self._transaction() as conn:
            task = await self._task_row(conn, task_id)
            if task["status"] in ("running", "done"):
                raise TaskConflictError("running/done tasks cannot be reassigned", current=TaskRecord.from_row(task))
            if agent_id is not None and not await self._agent_is_live(conn, agent_id):
                raise TaskValidationError("assignee Agent does not exist or is archived")
            await conn.execute(
                "UPDATE tasks SET assignee_agent_id = ?, updated_at = ? WHERE id = ?",
                (agent_id, stamp, task_id),
            )
            updated = await self._recompute_one(
                conn,
                task_id,
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
                now=stamp,
            )
            await self._event(
                conn,
                task=updated,
                kind="task_assigned",
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
                payload={"from": task["assignee_agent_id"], "to": agent_id},
                now=stamp,
            )
        return TaskRecord.from_row(updated)

    async def unblock_task(
        self,
        task_id: str,
        *,
        comment: str | None = None,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
    ) -> TaskRecord:
        stamp = _now_iso()
        async with self._transaction() as conn:
            task = await self._task_row(conn, task_id)
            if task["status"] != "blocked" or task["current_run_id"] is not None:
                raise TaskConflictError("only a closed blocked task may be unblocked", current=TaskRecord.from_row(task))
            await conn.execute(
                "UPDATE tasks SET status = 'todo', blocked_kind = NULL, blocked_reason = NULL, "
                "updated_at = ? WHERE id = ?",
                (stamp, task_id),
            )
            updated = await self._recompute_one(
                conn,
                task_id,
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
                now=stamp,
            )
            await self._event(
                conn,
                task=updated,
                kind="task_unblocked",
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
                payload={"to": updated["status"]},
                now=stamp,
            )
            if comment is not None:
                clean_comment = comment.strip()
                if not clean_comment:
                    raise TaskValidationError("unblock comment cannot be blank")
                comment_id = _short_id()
                author_kind = actor_kind if actor_kind in {"user", "agent", "system"} else "user"
                await conn.execute(
                    "INSERT INTO task_comments "
                    "(id,task_id,author_kind,author_agent_id,body,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        comment_id,
                        task_id,
                        author_kind,
                        actor_agent_id,
                        clean_comment,
                        stamp,
                    ),
                )
                await self._event(
                    conn,
                    task=updated,
                    kind="task_comment_added",
                    actor_kind=actor_kind,
                    actor_agent_id=actor_agent_id,
                    payload={"comment_id": comment_id},
                    now=stamp,
                )
        return TaskRecord.from_row(updated)

    async def cancel_task(
        self,
        task_id: str,
        *,
        reason: str | None = None,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
    ) -> TaskRecord:
        stamp = _now_iso()
        async with self._transaction() as conn:
            task = await self._task_row(conn, task_id)
            if task["status"] not in ("triage", "todo", "ready"):
                raise TaskConflictError("only non-running executable tasks may be cancelled", current=TaskRecord.from_row(task))
            await conn.execute(
                "UPDATE tasks SET status = 'blocked', blocked_kind = 'cancelled', "
                "blocked_reason = ?, updated_at = ? WHERE id = ?",
                (reason or "cancelled", stamp, task_id),
            )
            updated = await self._task_row(conn, task_id)
            await self._event(
                conn,
                task=updated,
                kind="task_cancelled",
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
                payload={"from": task["status"], "reason": reason},
                now=stamp,
            )
        return TaskRecord.from_row(updated)

    async def archive_task(
        self,
        task_id: str,
        *,
        archived: bool = True,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
    ) -> TaskRecord:
        stamp = _now_iso()
        async with self._transaction() as conn:
            task = await self._task_row(conn, task_id)
            if task["status"] == "running":
                raise TaskConflictError("running tasks must be closed before archival", current=TaskRecord.from_row(task))
            await conn.execute(
                "UPDATE tasks SET archived = ?, archived_at = ?, updated_at = ? WHERE id = ?",
                (int(archived), stamp if archived else None, stamp, task_id),
            )
            updated = await self._task_row(conn, task_id)
            await self._event(
                conn,
                task=updated,
                kind="task_archived" if archived else "task_unarchived",
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
                now=stamp,
            )
        return TaskRecord.from_row(updated)

    async def reconcile_eligibility(
        self, *, board_id: str | None = None, now: str | None = None
    ) -> list[TaskRecord]:
        stamp = now or _now_iso()
        async with self._transaction() as conn:
            sql = "SELECT id FROM tasks WHERE status IN ('todo','ready')"
            args: tuple[Any, ...] = ()
            if board_id is not None:
                await self._board_row(conn, board_id)
                sql += " AND board_id = ?"
                args = (board_id,)
            rows = await self._fetchall(conn, sql, args)
            result = [
                TaskRecord.from_row(
                    await self._recompute_one(conn, row["id"], now=stamp)
                )
                for row in rows
            ]
        return result

    # ---------------------------------------------------------- dependencies

    async def add_dependency(
        self,
        task_id: str,
        depends_on_task_id: str,
        *,
        created_by_kind: str = "user",
        created_by_agent_id: str | None = None,
    ) -> DependencyRecord:
        if task_id == depends_on_task_id:
            raise TaskValidationError("a task cannot depend on itself")
        stamp = _now_iso()
        async with self._transaction() as conn:
            task = await self._task_row(conn, task_id)
            dependency = await self._task_row(conn, depends_on_task_id)
            if task["board_id"] != dependency["board_id"]:
                raise TaskValidationError("task dependencies cannot cross boards")
            if task["status"] in ("running", "done"):
                raise TaskConflictError("dependencies cannot be added to running/done tasks", current=TaskRecord.from_row(task))
            cycle = await self._fetchone(
                conn,
                "WITH RECURSIVE reachable(id) AS ("
                " SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ?"
                " UNION SELECT d.depends_on_task_id FROM task_dependencies d"
                " JOIN reachable r ON d.task_id = r.id"
                ") SELECT 1 FROM reachable WHERE id = ? LIMIT 1",
                (depends_on_task_id, task_id),
            )
            if cycle:
                raise TaskConflictError("dependency would create a cycle")
            try:
                await conn.execute(
                    "INSERT INTO task_dependencies "
                    "(task_id,depends_on_task_id,created_by_kind,created_by_agent_id,created_at) "
                    "VALUES (?,?,?,?,?)",
                    (task_id, depends_on_task_id, created_by_kind, created_by_agent_id, stamp),
                )
            except aiosqlite.IntegrityError as exc:
                raise TaskConflictError("dependency already exists") from exc
            if task["status"] == "ready":
                await conn.execute(
                    "UPDATE tasks SET status = 'todo', updated_at = ? WHERE id = ?",
                    (stamp, task_id),
                )
            updated = await self._task_row(conn, task_id)
            await self._event(
                conn,
                task=updated,
                kind="task_dependency_added",
                actor_kind=created_by_kind,
                actor_agent_id=created_by_agent_id,
                payload={"depends_on_task_id": depends_on_task_id},
                now=stamp,
            )
            row = await self._fetchone(
                conn,
                "SELECT * FROM task_dependencies WHERE task_id = ? AND depends_on_task_id = ?",
                (task_id, depends_on_task_id),
            )
        return DependencyRecord(**dict(row))

    async def remove_dependency(
        self,
        task_id: str,
        depends_on_task_id: str,
        *,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
    ) -> TaskRecord:
        stamp = _now_iso()
        async with self._transaction() as conn:
            task = await self._task_row(conn, task_id)
            if task["status"] in ("running", "done"):
                raise TaskConflictError("dependencies cannot be removed from running/done tasks", current=TaskRecord.from_row(task))
            cursor = await conn.execute(
                "DELETE FROM task_dependencies WHERE task_id = ? AND depends_on_task_id = ?",
                (task_id, depends_on_task_id),
            )
            if cursor.rowcount != 1:
                raise TaskNotFoundError("dependency link not found")
            updated = await self._recompute_one(
                conn,
                task_id,
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
                now=stamp,
            )
            await self._event(
                conn,
                task=updated,
                kind="task_dependency_removed",
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
                payload={"depends_on_task_id": depends_on_task_id},
                now=stamp,
            )
        return TaskRecord.from_row(updated)

    async def list_dependencies(self, task_id: str) -> list[DependencyRecord]:
        async with self._lock:
            await self._task_row(self.conn, task_id)
            rows = await self._fetchall(
                self.conn,
                "SELECT * FROM task_dependencies WHERE task_id = ? ORDER BY created_at",
                (task_id,),
            )
        return [DependencyRecord(**dict(row)) for row in rows]

    async def list_dependents(self, task_id: str) -> list[DependencyRecord]:
        async with self._lock:
            await self._task_row(self.conn, task_id)
            rows = await self._fetchall(
                self.conn,
                "SELECT * FROM task_dependencies WHERE depends_on_task_id = ? ORDER BY created_at",
                (task_id,),
            )
        return [DependencyRecord(**dict(row)) for row in rows]

    # ------------------------------------------------------------------ runs

    async def claim_ready(
        self,
        task_id: str,
        *,
        workspace_mode: str,
        workspace_path: str,
        lease_expires_at: str | None = None,
        run_id: str | None = None,
        now: str | None = None,
    ) -> RunRecord:
        if workspace_mode not in WORKSPACE_MODES:
            raise TaskValidationError("invalid workspace mode")
        canonical_path = self._validated_dir(
            workspace_path, must_exist=workspace_mode == "shared"
        )
        stamp = now or _now_iso()
        ident = run_id or _short_id()
        async with self._transaction() as conn:
            task = await self._task_row(conn, task_id)
            if (
                task["status"] != "ready"
                or task["current_run_id"] is not None
                or task["archived"]
                or not await self._eligible(conn, task, stamp)
            ):
                raise TaskConflictError("task lost the ready claim race", current=TaskRecord.from_row(task))
            board = await self._board_row(conn, task["board_id"])
            if not board["dispatch_enabled"] or board["archived"]:
                raise TaskConflictError("board dispatcher is paused or archived", current=TaskRecord.from_row(task))
            if board["max_running"] is not None:
                count = await self._fetchone(
                    conn,
                    "SELECT COUNT(*) AS n FROM tasks WHERE board_id = ? AND status = 'running'",
                    (task["board_id"],),
                )
                if count["n"] >= board["max_running"]:
                    raise TaskCapacityError("board running-task limit reached", current=TaskRecord.from_row(task))
            if board["max_running_per_agent"] is not None:
                count = await self._fetchone(
                    conn,
                    "SELECT COUNT(*) AS n FROM tasks WHERE board_id = ? "
                    "AND status = 'running' AND assignee_agent_id = ?",
                    (task["board_id"], task["assignee_agent_id"]),
                )
                if count["n"] >= board["max_running_per_agent"]:
                    raise TaskCapacityError("per-Agent running-task limit reached", current=TaskRecord.from_row(task))
            if workspace_mode == "shared":
                conflict = await self._fetchone(
                    conn,
                    "SELECT 1 FROM task_runs WHERE state = 'running' "
                    "AND workspace_mode = 'shared' AND workspace_path = ? LIMIT 1",
                    (canonical_path,),
                )
                if conflict:
                    raise TaskCapacityError("shared workspace is already owned by a running task", current=TaskRecord.from_row(task))
            attempt = await self._fetchone(
                conn,
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS n FROM task_runs WHERE task_id = ?",
                (task_id,),
            )
            await conn.execute(
                "INSERT INTO task_runs "
                "(id,task_id,attempt_no,agent_id,state,workspace_mode,workspace_path,"
                "claimed_at,last_heartbeat_at,lease_expires_at) "
                "VALUES (?,?,?,?, 'running',?,?,?,?,?)",
                (
                    ident,
                    task_id,
                    attempt["n"],
                    task["assignee_agent_id"],
                    workspace_mode,
                    canonical_path,
                    stamp,
                    stamp,
                    lease_expires_at,
                ),
            )
            cursor = await conn.execute(
                "UPDATE tasks SET status = 'running', current_run_id = ?, updated_at = ? "
                "WHERE id = ? AND status = 'ready' AND current_run_id IS NULL",
                (ident, stamp, task_id),
            )
            if cursor.rowcount != 1:
                raise TaskConflictError("task lost the ready claim race", current=TaskRecord.from_row(await self._task_row(conn, task_id)))
            updated = await self._task_row(conn, task_id)
            await self._event(
                conn,
                task=updated,
                kind="task_claimed",
                actor_kind="system",
                actor_agent_id=task["assignee_agent_id"],
                run_id=ident,
                payload={"attempt_no": attempt["n"], "workspace_mode": workspace_mode},
                now=stamp,
            )
            run = await self._run_row(conn, ident)
        return self._run_record(run)

    async def attach_run_session(
        self, task_id: str, run_id: str, session_id: str, *, started_at: str | None = None
    ) -> RunRecord:
        stamp = started_at or _now_iso()
        async with self._transaction() as conn:
            task = await self._task_row(conn, task_id)
            run = await self._run_row(conn, run_id)
            if task["current_run_id"] != run_id or run["task_id"] != task_id or run["state"] != "running":
                raise TaskConflictError("run no longer owns the task", current=TaskRecord.from_row(task))
            if run["session_id"] not in (None, session_id):
                raise TaskConflictError("run already has a different worker session")
            await conn.execute(
                "UPDATE task_runs SET session_id = ?, started_at = COALESCE(started_at, ?) "
                "WHERE id = ? AND state = 'running'",
                (session_id, stamp, run_id),
            )
            run = await self._run_row(conn, run_id)
            await self._event(
                conn,
                task=task,
                kind="task_run_started",
                actor_kind="system",
                run_id=run_id,
                payload={"session_id": session_id},
                now=stamp,
            )
        return self._run_record(run)

    async def heartbeat_run(
        self,
        task_id: str,
        run_id: str,
        *,
        lease_expires_at: str,
        note: str | None = None,
        now: str | None = None,
        emit_event: bool = True,
    ) -> RunRecord:
        stamp = now or _now_iso()
        async with self._transaction() as conn:
            task = await self._task_row(conn, task_id)
            cursor = await conn.execute(
                "UPDATE task_runs SET last_heartbeat_at = ?, lease_expires_at = ? "
                "WHERE id = ? AND task_id = ? AND state = 'running' "
                "AND ? = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (stamp, lease_expires_at, run_id, task_id, run_id, task_id),
            )
            if cursor.rowcount != 1:
                raise TaskConflictError("run no longer owns the task", current=TaskRecord.from_row(task))
            run = await self._run_row(conn, run_id)
            if emit_event:
                await self._event(
                    conn,
                    task=task,
                    kind="task_run_heartbeat",
                    actor_kind="agent",
                    actor_agent_id=run["agent_id"],
                    run_id=run_id,
                    payload={"note": note} if note else {},
                    now=stamp,
                )
        return self._run_record(run)

    async def _finish_run(
        self,
        task_id: str,
        run_id: str,
        *,
        run_state: str,
        task_status: str,
        summary: str | None,
        metadata: Mapping[str, Any] | None,
        error: str | None,
        blocked_kind: str | None,
        blocked_reason: str | None,
        actor_kind: str,
        actor_agent_id: str | None,
        now: str | None,
        artifacts: Sequence[Mapping[str, Any]] | None = None,
    ) -> tuple[TaskRecord, RunRecord]:
        if run_state not in {"completed", "blocked", "failed", "cancelled", "interrupted"}:
            raise TaskValidationError("invalid terminal run state")
        if task_status not in {"done", "blocked"}:
            raise TaskValidationError("invalid terminal task status")
        if blocked_kind is not None and blocked_kind not in BLOCKED_KINDS:
            raise TaskValidationError("invalid blocked kind")
        stamp = now or _now_iso()
        async with self._transaction() as conn:
            task = await self._task_row(conn, task_id)
            run = await self._run_row(conn, run_id)
            if task["status"] != "running" or task["current_run_id"] != run_id or run["task_id"] != task_id or run["state"] != "running":
                raise TaskConflictError("run no longer owns the task", current=TaskRecord.from_row(task))
            # Dispatch-time evidence (notably the git_worktree base_ref and
            # base_head) is authoritative input to the later delivery flow.
            # Terminal callers may add metadata, but must never erase the
            # evidence already attached to the run.
            merged_metadata = _load_object(run["metadata"]) or {}
            merged_metadata.update(dict(metadata or {}))
            await conn.execute(
                "UPDATE task_runs SET state = ?, summary = ?, metadata = ?, error = ?, "
                "finished_at = ? WHERE id = ? AND state = 'running'",
                (
                    run_state,
                    summary,
                    _json_object(merged_metadata),
                    error,
                    stamp,
                    run_id,
                ),
            )
            cursor = await conn.execute(
                "UPDATE tasks SET status = ?, current_run_id = NULL, blocked_kind = ?, "
                "blocked_reason = ?, result_summary = ?, updated_at = ?, completed_at = ? "
                "WHERE id = ? AND status = 'running' AND current_run_id = ?",
                (
                    task_status,
                    blocked_kind,
                    blocked_reason,
                    summary,
                    stamp,
                    stamp if task_status == "done" else None,
                    task_id,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise TaskConflictError("run lost the terminal CAS", current=TaskRecord.from_row(await self._task_row(conn, task_id)))
            updated = await self._task_row(conn, task_id)
            artifact_names: set[str] = set()
            for item in artifacts or ():
                name = str(item.get("name", "")).strip()
                sha256 = str(item.get("sha256", "")).strip()
                size = item.get("size")
                if (
                    not name
                    or name in artifact_names
                    or isinstance(size, bool)
                    or not isinstance(size, int)
                    or size < 0
                    or not sha256
                ):
                    raise TaskValidationError("captured artifacts require unique names, size and sha256")
                artifact_names.add(name)
                artifact_id = str(item.get("id") or _short_id())
                await conn.execute(
                    "INSERT INTO task_artifacts "
                    "(id,task_id,run_id,name,stored_path,source_path,mime_type,size,sha256,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        artifact_id,
                        task_id,
                        run_id,
                        name,
                        str(item.get("stored_path", "")),
                        str(item.get("source_path", "")),
                        item.get("mime_type"),
                        size,
                        sha256,
                        stamp,
                    ),
                )
                await self._event(
                    conn,
                    task=updated,
                    kind="task_artifact_added",
                    actor_kind="system",
                    run_id=run_id,
                    payload={"artifact_id": artifact_id, "name": name},
                    now=stamp,
                )
            await self._event(
                conn,
                task=updated,
                kind=f"task_run_{run_state}",
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
                run_id=run_id,
                payload={"summary": summary, "error": error, "blocked_kind": blocked_kind},
                now=stamp,
            )
            if task_status == "done":
                dependents = await self._fetchall(
                    conn,
                    "SELECT task_id FROM task_dependencies WHERE depends_on_task_id = ?",
                    (task_id,),
                )
                for dependent in dependents:
                    await self._recompute_one(conn, dependent["task_id"], now=stamp)
            final_run = await self._run_row(conn, run_id)
        return TaskRecord.from_row(updated), self._run_record(final_run)

    async def complete_run(
        self,
        task_id: str,
        run_id: str,
        *,
        summary: str,
        metadata: Mapping[str, Any] | None = None,
        artifacts: Sequence[Mapping[str, Any]] | None = None,
        actor_agent_id: str | None = None,
        now: str | None = None,
    ) -> tuple[TaskRecord, RunRecord]:
        if not summary.strip():
            raise TaskValidationError("completion summary is required")
        return await self._finish_run(
            task_id,
            run_id,
            run_state="completed",
            task_status="done",
            summary=summary,
            metadata=metadata,
            error=None,
            blocked_kind=None,
            blocked_reason=None,
            actor_kind="agent",
            actor_agent_id=actor_agent_id,
            now=now,
            artifacts=artifacts,
        )

    async def block_run(
        self,
        task_id: str,
        run_id: str,
        *,
        reason: str,
        kind: str = "input",
        summary: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        actor_agent_id: str | None = None,
        now: str | None = None,
    ) -> tuple[TaskRecord, RunRecord]:
        if kind not in BLOCKED_KINDS:
            raise TaskValidationError("invalid blocked kind")
        return await self._finish_run(
            task_id,
            run_id,
            run_state="blocked",
            task_status="blocked",
            summary=summary,
            metadata=metadata,
            error=None,
            blocked_kind=kind,
            blocked_reason=reason,
            actor_kind="agent",
            actor_agent_id=actor_agent_id,
            now=now,
            artifacts=None,
        )

    async def fail_run(
        self,
        task_id: str,
        run_id: str,
        *,
        error: str,
        kind: str = "failure",
        summary: str | None = None,
        now: str | None = None,
    ) -> tuple[TaskRecord, RunRecord]:
        if kind not in ("failure", "protocol", "capability"):
            raise TaskValidationError("failed runs require failure/protocol/capability kind")
        return await self._finish_run(
            task_id,
            run_id,
            run_state="failed",
            task_status="blocked",
            summary=summary,
            metadata=None,
            error=error,
            blocked_kind=kind,
            blocked_reason=error,
            actor_kind="system",
            actor_agent_id=None,
            now=now,
            artifacts=None,
        )

    async def cancel_run(
        self, task_id: str, run_id: str, *, reason: str = "cancelled", now: str | None = None
    ) -> tuple[TaskRecord, RunRecord]:
        return await self._finish_run(
            task_id,
            run_id,
            run_state="cancelled",
            task_status="blocked",
            summary=None,
            metadata=None,
            error=None,
            blocked_kind="cancelled",
            blocked_reason=reason,
            actor_kind="user",
            actor_agent_id=None,
            now=now,
            artifacts=None,
        )

    async def interrupt_run(
        self, task_id: str, run_id: str, *, reason: str, now: str | None = None
    ) -> tuple[TaskRecord, RunRecord]:
        return await self._finish_run(
            task_id,
            run_id,
            run_state="interrupted",
            task_status="blocked",
            summary=None,
            metadata=None,
            error=reason,
            blocked_kind="interrupted",
            blocked_reason=reason,
            actor_kind="system",
            actor_agent_id=None,
            now=now,
            artifacts=None,
        )

    async def get_run(self, run_id: str) -> RunRecord:
        async with self._lock:
            return self._run_record(await self._run_row(self.conn, run_id))

    async def list_runs(self, task_id: str) -> list[RunRecord]:
        async with self._lock:
            await self._task_row(self.conn, task_id)
            rows = await self._fetchall(
                self.conn,
                "SELECT * FROM task_runs WHERE task_id = ? ORDER BY attempt_no",
                (task_id,),
            )
        return [self._run_record(row) for row in rows]

    async def list_running_runs(self) -> list[RunRecord]:
        async with self._lock:
            rows = await self._fetchall(
                self.conn, "SELECT * FROM task_runs WHERE state = 'running' ORDER BY claimed_at"
            )
        return [self._run_record(row) for row in rows]

    async def list_terminal_runs(self) -> list[tuple[TaskRecord, RunRecord]]:
        """Rows eligible for deterministic terminal-notification repair."""
        async with self._lock:
            rows = await self._fetchall(
                self.conn,
                "SELECT r.*, t.id AS joined_task_id FROM task_runs r JOIN tasks t "
                "ON t.id = r.task_id WHERE r.state != 'running' "
                "ORDER BY r.finished_at, r.id",
            )
            result: list[tuple[TaskRecord, RunRecord]] = []
            for row in rows:
                task = await self._task_row(self.conn, row["task_id"])
                run_values = {key: row[key] for key in (
                    "id", "task_id", "attempt_no", "agent_id", "session_id", "state",
                    "summary", "metadata", "error", "workspace_mode", "workspace_path",
                    "claimed_at", "started_at", "last_heartbeat_at", "lease_expires_at",
                    "finished_at",
                )}
                result.append((TaskRecord.from_row(task), self._run_record(run_values)))
        return result

    async def interrupt_all_running(
        self, *, reason: str, now: str | None = None
    ) -> list[tuple[TaskRecord, RunRecord]]:
        """Boot/shutdown reconciliation; terminalizes every durable active claim."""
        stamp = now or _now_iso()
        async with self._transaction() as conn:
            rows = await self._fetchall(
                conn,
                "SELECT r.id AS run_id, r.task_id FROM task_runs r JOIN tasks t "
                "ON t.id = r.task_id WHERE r.state = 'running' "
                "AND t.status = 'running' AND t.current_run_id = r.id ORDER BY r.claimed_at",
            )
            result: list[tuple[TaskRecord, RunRecord]] = []
            for row in rows:
                task = await self._task_row(conn, row["task_id"])
                await conn.execute(
                    "UPDATE task_runs SET state = 'interrupted', error = ?, finished_at = ? "
                    "WHERE id = ? AND state = 'running'",
                    (reason, stamp, row["run_id"]),
                )
                await conn.execute(
                    "UPDATE tasks SET status = 'blocked', current_run_id = NULL, "
                    "blocked_kind = 'interrupted', blocked_reason = ?, updated_at = ? "
                    "WHERE id = ? AND current_run_id = ?",
                    (reason, stamp, row["task_id"], row["run_id"]),
                )
                updated = await self._task_row(conn, row["task_id"])
                await self._event(
                    conn,
                    task=updated,
                    kind="task_run_interrupted",
                    actor_kind="system",
                    run_id=row["run_id"],
                    payload={"error": reason, "recovery": True},
                    now=stamp,
                )
                run = await self._run_row(conn, row["run_id"])
                result.append((TaskRecord.from_row(updated), self._run_record(run)))
        return result

    # ----------------------------------------------------- comments / events

    async def add_comment(
        self,
        task_id: str,
        body: str,
        *,
        run_id: str | None = None,
        author_kind: str = "user",
        author_agent_id: str | None = None,
        comment_id: str | None = None,
    ) -> CommentRecord:
        clean = body.strip()
        if not clean:
            raise TaskValidationError("comment body is required")
        if author_kind not in {"user", "agent", "system"}:
            raise TaskValidationError("invalid comment author kind")
        stamp = _now_iso()
        ident = comment_id or _short_id()
        async with self._transaction() as conn:
            task = await self._task_row(conn, task_id)
            if run_id is not None:
                run = await self._run_row(conn, run_id)
                if run["task_id"] != task_id:
                    raise TaskValidationError("comment run does not belong to task")
            await conn.execute(
                "INSERT INTO task_comments "
                "(id,task_id,run_id,author_kind,author_agent_id,body,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (ident, task_id, run_id, author_kind, author_agent_id, clean, stamp),
            )
            await self._event(
                conn,
                task=task,
                kind="task_comment_added",
                actor_kind=author_kind,
                actor_agent_id=author_agent_id,
                run_id=run_id,
                payload={"comment_id": ident},
                now=stamp,
            )
            row = await self._fetchone(
                conn, "SELECT * FROM task_comments WHERE id = ?", (ident,)
            )
        return CommentRecord(**dict(row))

    async def list_comments(self, task_id: str) -> list[CommentRecord]:
        async with self._lock:
            await self._task_row(self.conn, task_id)
            rows = await self._fetchall(
                self.conn,
                "SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at, id",
                (task_id,),
            )
        return [CommentRecord(**dict(row)) for row in rows]

    async def list_task_events(
        self, task_id: str, *, after_seq: int = 0, limit: int = 500
    ) -> list[EventRecord]:
        async with self._lock:
            await self._task_row(self.conn, task_id)
            rows = await self._fetchall(
                self.conn,
                "SELECT * FROM task_events WHERE task_id = ? AND seq > ? "
                "ORDER BY seq LIMIT ?",
                (task_id, after_seq, limit),
            )
        return [self._event_record(row) for row in rows]

    async def get_latest_task_event(self, task_id: str) -> EventRecord | None:
        async with self._lock:
            await self._task_row(self.conn, task_id)
            row = await self._fetchone(
                self.conn,
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY seq DESC LIMIT 1",
                (task_id,),
            )
        return self._event_record(row) if row else None

    async def get_latest_board_event(self, board_id: str) -> EventRecord | None:
        async with self._lock:
            await self._board_row(self.conn, board_id)
            row = await self._fetchone(
                self.conn,
                "SELECT * FROM task_events WHERE board_id = ? AND task_id IS NULL "
                "ORDER BY seq DESC LIMIT 1",
                (board_id,),
            )
        return self._event_record(row) if row else None

    async def list_board_events(
        self, board_id: str, *, after_seq: int = 0, limit: int = 500
    ) -> list[EventRecord]:
        async with self._lock:
            await self._board_row(self.conn, board_id)
            rows = await self._fetchall(
                self.conn,
                "SELECT * FROM task_events WHERE board_id = ? AND seq > ? "
                "ORDER BY seq LIMIT ?",
                (board_id, after_seq, limit),
            )
        return [self._event_record(row) for row in rows]

    async def record_notification_unavailable(
        self, task_id: str, run_id: str, *, reason: str
    ) -> EventRecord:
        """Idempotent audit marker used when a terminal origin was deleted."""
        stamp = _now_iso()
        async with self._transaction() as conn:
            task = await self._task_row(conn, task_id)
            run = await self._run_row(conn, run_id)
            if run["task_id"] != task_id or run["state"] == "running":
                raise TaskConflictError("notification audit requires a terminal owning run")
            existing = await self._fetchone(
                conn,
                "SELECT * FROM task_events WHERE task_id = ? AND run_id = ? "
                "AND kind = 'notification_unavailable' LIMIT 1",
                (task_id, run_id),
            )
            if existing:
                return self._event_record(existing)
            await self._event(
                conn,
                task=task,
                kind="notification_unavailable",
                actor_kind="system",
                run_id=run_id,
                payload={"reason": reason},
                now=stamp,
            )
            row = await self._fetchone(
                conn,
                "SELECT * FROM task_events WHERE task_id = ? AND run_id = ? "
                "AND kind = 'notification_unavailable' ORDER BY seq DESC LIMIT 1",
                (task_id, run_id),
            )
        return self._event_record(row)

    # --------------------------------------------------------------- artifacts

    async def add_artifact(
        self,
        task_id: str,
        run_id: str,
        *,
        name: str,
        stored_path: str,
        source_path: str,
        size: int,
        sha256: str,
        mime_type: str | None = None,
        artifact_id: str | None = None,
    ) -> ArtifactRecord:
        if not name.strip() or size < 0 or not sha256:
            raise TaskValidationError("artifact name, non-negative size and sha256 are required")
        ident = artifact_id or _short_id()
        stamp = _now_iso()
        async with self._transaction() as conn:
            task = await self._task_row(conn, task_id)
            run = await self._run_row(conn, run_id)
            if run["task_id"] != task_id:
                raise TaskValidationError("artifact run does not belong to task")
            try:
                await conn.execute(
                    "INSERT INTO task_artifacts "
                    "(id,task_id,run_id,name,stored_path,source_path,mime_type,size,sha256,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        ident,
                        task_id,
                        run_id,
                        name.strip(),
                        stored_path,
                        source_path,
                        mime_type,
                        size,
                        sha256,
                        stamp,
                    ),
                )
            except aiosqlite.IntegrityError as exc:
                raise TaskConflictError("artifact name already exists for this run") from exc
            await self._event(
                conn,
                task=task,
                kind="task_artifact_added",
                actor_kind="system",
                run_id=run_id,
                payload={"artifact_id": ident, "name": name},
                now=stamp,
            )
            row = await self._fetchone(
                conn, "SELECT * FROM task_artifacts WHERE id = ?", (ident,)
            )
        return ArtifactRecord(**dict(row))

    async def list_artifacts(
        self, task_id: str, *, include_deleted: bool = False
    ) -> list[ArtifactRecord]:
        async with self._lock:
            await self._task_row(self.conn, task_id)
            sql = "SELECT * FROM task_artifacts WHERE task_id = ?"
            if not include_deleted:
                sql += " AND deleted_at IS NULL"
            sql += " ORDER BY created_at, id"
            rows = await self._fetchall(self.conn, sql, (task_id,))
        return [ArtifactRecord(**dict(row)) for row in rows]

    async def get_artifact(self, task_id: str, artifact_id: str) -> ArtifactRecord:
        async with self._lock:
            await self._task_row(self.conn, task_id)
            row = await self._fetchone(
                self.conn,
                "SELECT * FROM task_artifacts WHERE id = ? AND task_id = ?",
                (artifact_id, task_id),
            )
        if row is None:
            raise TaskNotFoundError("artifact not found")
        return ArtifactRecord(**dict(row))

    async def delete_artifact(self, task_id: str, artifact_id: str) -> ArtifactRecord:
        """Tombstone metadata; the caller removes already-validated bytes."""
        stamp = _now_iso()
        async with self._transaction() as conn:
            task = await self._task_row(conn, task_id)
            row = await self._fetchone(
                conn,
                "SELECT * FROM task_artifacts WHERE id = ? AND task_id = ?",
                (artifact_id, task_id),
            )
            if row is None:
                raise TaskNotFoundError("artifact not found")
            if row["deleted_at"] is None:
                await conn.execute(
                    "UPDATE task_artifacts SET deleted_at = ? WHERE id = ?",
                    (stamp, artifact_id),
                )
                await self._event(
                    conn,
                    task=task,
                    kind="task_artifact_deleted",
                    actor_kind="user",
                    run_id=row["run_id"],
                    payload={"artifact_id": artifact_id},
                    now=stamp,
                )
                row = await self._fetchone(
                    conn, "SELECT * FROM task_artifacts WHERE id = ?", (artifact_id,)
                )
        return ArtifactRecord(**dict(row))

    # --- Git delivery (task-git-delivery.md §11, §12) --------------------

    @staticmethod
    def _delivery_record(row: Mapping[str, Any]) -> DeliveryRecord:
        values = dict(row)
        values["dirty"] = bool(values["dirty"])
        values["diffstat"] = _load_object(values["diffstat"])
        return DeliveryRecord(**values)

    @staticmethod
    def _delivery_op_record(row: Mapping[str, Any]) -> DeliveryOpRecord:
        values = dict(row)
        values["external"] = bool(values["external"])
        values["request"] = _load_object(values["request"]) or {}
        values["result"] = _load_object(values["result"])
        return DeliveryOpRecord(**values)

    async def _delivery_row(
        self, conn: aiosqlite.Connection, delivery_id: str
    ) -> aiosqlite.Row:
        row = await self._fetchone(
            conn, "SELECT * FROM task_deliveries WHERE id = ?", (delivery_id,)
        )
        if row is None:
            raise TaskNotFoundError(f"delivery {delivery_id!r} not found")
        return row

    async def _delivery_op_row(
        self, conn: aiosqlite.Connection, op_id: str, delivery_id: str
    ) -> aiosqlite.Row:
        row = await self._fetchone(
            conn,
            "SELECT * FROM task_delivery_ops WHERE id = ? AND delivery_id = ?",
            (op_id, delivery_id),
        )
        if row is None:
            raise TaskNotFoundError(f"delivery op {op_id!r} not found")
        return row

    async def _delivery_event(
        self,
        conn: aiosqlite.Connection,
        *,
        delivery_row: Mapping[str, Any],
        kind: str,
        actor_kind: str,
        actor_agent_id: str | None = None,
        op_row: Mapping[str, Any] | None = None,
        now: str | None = None,
    ) -> None:
        """Write a task-scoped audit event carrying the delivery (and op).

        The payload shape ``{delivery, op}`` is what the browser store folds in
        (task-git-delivery.md §14); ``publish_task_update`` merges the task
        snapshot alongside it.
        """
        task = await self._task_row(conn, delivery_row["task_id"])
        payload: dict[str, Any] = {
            "delivery": self._delivery_record(delivery_row).to_dict()
        }
        if op_row is not None:
            payload["op"] = self._delivery_op_record(op_row).to_dict()
        await self._event(
            conn,
            task=task,
            kind=kind,
            actor_kind=actor_kind,
            actor_agent_id=actor_agent_id,
            run_id=delivery_row["run_id"],
            payload=payload,
            now=now,
        )

    async def set_run_metadata(
        self, task_id: str, run_id: str, metadata: Mapping[str, Any]
    ) -> None:
        """Merge dispatch/prep metadata onto a run so it survives completion.

        The git_worktree base branch/commit are captured at prepare time and
        must outlive the terminal-metadata overwrite so delivery can read them
        (task-git-delivery.md §5, B1)."""
        async with self._transaction() as conn:
            run = await self._run_row(conn, run_id)
            if run["task_id"] != task_id:
                raise TaskConflictError("run does not belong to task")
            merged = _load_object(run["metadata"]) or {}
            merged.update(dict(metadata or {}))
            await conn.execute(
                "UPDATE task_runs SET metadata = ? WHERE id = ?",
                (_json_object(merged), run_id),
            )

    async def get_delivery(self, delivery_id: str) -> DeliveryRecord:
        async with self._lock:
            return self._delivery_record(
                await self._delivery_row(self.conn, delivery_id)
            )

    async def get_delivery_by_run(self, run_id: str) -> DeliveryRecord | None:
        async with self._lock:
            row = await self._fetchone(
                self.conn, "SELECT * FROM task_deliveries WHERE run_id = ?", (run_id,)
            )
        return self._delivery_record(row) if row else None

    async def list_delivery_ops(self, delivery_id: str) -> list[DeliveryOpRecord]:
        async with self._lock:
            rows = await self._fetchall(
                self.conn,
                "SELECT * FROM task_delivery_ops WHERE delivery_id = ? "
                "ORDER BY created_at, id",
                (delivery_id,),
            )
        return [self._delivery_op_record(r) for r in rows]

    async def record_delivery_retention(
        self,
        delivery_id: str,
        retention: str,
        *,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
    ) -> DeliveryRecord:
        """Persist the operator-selected teardown policy before cleanup starts."""
        if retention not in DELIVERY_RETENTIONS:
            raise TaskValidationError("invalid Git delivery retention")
        stamp = _now_iso()
        async with self._transaction() as conn:
            row = await self._delivery_row(conn, delivery_id)
            if row["retention"] == retention:
                return self._delivery_record(row)
            await conn.execute(
                "UPDATE task_deliveries SET retention=?, updated_at=? WHERE id=?",
                (retention, stamp, delivery_id),
            )
            row = await self._delivery_row(conn, delivery_id)
            await self._delivery_event(
                conn,
                delivery_row=row,
                kind="delivery_retention_updated",
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
                now=stamp,
            )
        return self._delivery_record(row)

    async def create_delivery(
        self,
        run_id: str,
        *,
        repository: str,
        attempt_branch: str,
        base_ref: str | None,
        base_head: str | None,
        dirty: bool = False,
        retention: str = "keep",
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
    ) -> DeliveryRecord:
        """Idempotently create the one delivery for a completed worktree run.

        If a delivery already exists it is returned UNCHANGED — an idempotent
        re-accept must never rewind a terminal or in-flight delivery (nit 2).
        """
        if retention not in DELIVERY_RETENTIONS:
            raise TaskValidationError("invalid Git delivery retention")
        async with self._transaction() as conn:
            run = await self._run_row(conn, run_id)
            existing = await self._fetchone(
                conn, "SELECT * FROM task_deliveries WHERE run_id = ?", (run_id,)
            )
            if existing is not None:
                return self._delivery_record(existing)
            if run["state"] != "completed" or run["workspace_mode"] != "git_worktree":
                raise TaskConflictError(
                    "delivery requires a completed git_worktree run"
                )
            stamp = _now_iso()
            ident = _short_id()
            await conn.execute(
                "INSERT INTO task_deliveries (id, task_id, run_id, status, repository, "
                "base_ref, base_head, attempt_branch, dirty, retention, created_at, updated_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ident,
                    run["task_id"],
                    run_id,
                    repository,
                    base_ref,
                    base_head,
                    attempt_branch,
                    int(bool(dirty)),
                    retention,
                    stamp,
                    stamp,
                ),
            )
            row = await self._delivery_row(conn, ident)
            await self._delivery_event(
                conn,
                delivery_row=row,
                kind="delivery_created",
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
                now=stamp,
            )
        return self._delivery_record(row)

    async def start_accept(
        self,
        delivery_id: str,
        *,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
    ) -> DeliveryRecord:
        """Move a delivery into ``preparing`` for a (re)capture of its baseline.

        Legal only from ``pending``/``ready``/``failed`` (nit 1: pending→preparing
        is a valid re-accept; failed→preparing retries a baseline that could not
        be captured). ``delivering`` and non-``failed`` terminal states are
        rejected so an idempotent re-run cannot rewind them (nit 2)."""
        stamp = _now_iso()
        async with self._transaction() as conn:
            row = await self._delivery_row(conn, delivery_id)
            if row["status"] not in {"pending", "ready", "failed"}:
                raise TaskConflictError(
                    "delivery cannot be re-accepted from its current state",
                    current=self._delivery_record(row),
                )
            cursor = await conn.execute(
                "UPDATE task_deliveries SET status='preparing', reason_kind=NULL, "
                "reason_detail=NULL, updated_at=? WHERE id=? AND "
                "status IN ('pending','ready','failed')",
                (stamp, delivery_id),
            )
            if cursor.rowcount != 1:
                raise TaskConflictError(
                    "delivery accept lost the CAS",
                    current=self._delivery_record(
                        await self._delivery_row(conn, delivery_id)
                    ),
                )
            row = await self._delivery_row(conn, delivery_id)
            await self._delivery_event(
                conn,
                delivery_row=row,
                kind="delivery_accept_started",
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
                now=stamp,
            )
        return self._delivery_record(row)

    async def record_baseline(
        self,
        delivery_id: str,
        *,
        status: str,
        base_ref: str | None = None,
        attempt_head: str | None = None,
        dirty: bool | None = None,
        commits_ahead: int | None = None,
        diffstat: Mapping[str, Any] | None = None,
        remote_name: str | None = None,
        remote_url: str | None = None,
        reason_kind: str | None = None,
        reason_detail: str | None = None,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
    ) -> DeliveryRecord:
        """Fold the captured baseline in and settle ``preparing`` → terminal-of-prepare.

        Only the base pair is immutable; ``attempt_head``/``commits_ahead``/
        ``diffstat`` are refreshed here and after each commit op (N1)."""
        if status not in {"ready", "failed", "blocked"}:
            raise TaskValidationError("invalid baseline status")
        if reason_kind is not None and reason_kind not in DELIVERY_REASON_KINDS:
            raise TaskValidationError("invalid delivery reason kind")
        stamp = _now_iso()
        fields: dict[str, Any] = {
            "status": status,
            "reason_kind": reason_kind,
            "reason_detail": reason_detail,
            "updated_at": stamp,
        }
        for key, val in (
            ("base_ref", base_ref),
            ("attempt_head", attempt_head),
            ("commits_ahead", commits_ahead),
            ("remote_name", remote_name),
            ("remote_url", remote_url),
        ):
            if val is not None:
                fields[key] = val
        if dirty is not None:
            fields["dirty"] = int(bool(dirty))
        if diffstat is not None:
            fields["diffstat"] = _json_object(diffstat)
        async with self._transaction() as conn:
            row = await self._delivery_row(conn, delivery_id)
            if row["status"] != "preparing":
                raise TaskConflictError(
                    "baseline can only be recorded while preparing",
                    current=self._delivery_record(row),
                )
            sets = ", ".join(f"{k} = ?" for k in fields)
            cursor = await conn.execute(
                f"UPDATE task_deliveries SET {sets} WHERE id = ? AND status='preparing'",
                (*fields.values(), delivery_id),
            )
            if cursor.rowcount != 1:
                raise TaskConflictError(
                    "baseline lost the CAS",
                    current=self._delivery_record(
                        await self._delivery_row(conn, delivery_id)
                    ),
                )
            row = await self._delivery_row(conn, delivery_id)
            await self._delivery_event(
                conn,
                delivery_row=row,
                kind=f"delivery_{status}",
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
                now=stamp,
            )
        return self._delivery_record(row)

    async def resolve_base(
        self,
        delivery_id: str,
        *,
        base_ref: str,
        base_head: str | None = None,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
    ) -> DeliveryRecord:
        """Record an operator-verified base branch (and, for a legacy run with no
        captured base commit, a derived ``base_head``), then return to
        ``pending`` so accept re-captures the baseline (§5.1)."""
        stamp = _now_iso()
        async with self._transaction() as conn:
            row = await self._delivery_row(conn, delivery_id)
            if not (row["status"] == "blocked" and row["reason_kind"] == "base_ambiguous"):
                raise TaskConflictError(
                    "delivery is not awaiting base resolution",
                    current=self._delivery_record(row),
                )
            new_base_head = base_head if base_head is not None else row["base_head"]
            cursor = await conn.execute(
                "UPDATE task_deliveries SET base_ref=?, base_head=?, status='pending', "
                "reason_kind=NULL, reason_detail=NULL, updated_at=? "
                "WHERE id=? AND status='blocked'",
                (base_ref, new_base_head, stamp, delivery_id),
            )
            if cursor.rowcount != 1:
                raise TaskConflictError(
                    "base resolution lost the CAS",
                    current=self._delivery_record(
                        await self._delivery_row(conn, delivery_id)
                    ),
                )
            row = await self._delivery_row(conn, delivery_id)
            await self._delivery_event(
                conn,
                delivery_row=row,
                kind="delivery_base_resolved",
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
                now=stamp,
            )
        return self._delivery_record(row)

    async def plan_op(
        self,
        delivery_id: str,
        *,
        kind: str,
        source_key: str,
        request: Mapping[str, Any] | None = None,
        actor_kind: str,
        actor_agent_id: str | None = None,
    ) -> DeliveryOpRecord:
        """Insert a planned op; a repeated ``source_key`` returns the existing
        row, never a second attempt (at-most-once, §3)."""
        if kind not in DELIVERY_OP_KINDS:
            raise TaskValidationError("invalid delivery op kind")
        if actor_kind not in {"user", "agent"}:
            raise TaskValidationError("invalid delivery actor kind")
        external = 1 if kind in DELIVERY_EXTERNAL_OP_KINDS else 0
        stamp = _now_iso()
        async with self._transaction() as conn:
            await self._delivery_row(conn, delivery_id)
            existing = await self._fetchone(
                conn,
                "SELECT * FROM task_delivery_ops WHERE source_key = ?",
                (source_key,),
            )
            if existing is not None:
                return self._delivery_op_record(existing)
            ident = _short_id()
            await conn.execute(
                "INSERT INTO task_delivery_ops (id, delivery_id, kind, source_key, "
                "external, state, request, actor_kind, actor_agent_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'planned', ?, ?, ?, ?)",
                (
                    ident,
                    delivery_id,
                    kind,
                    source_key,
                    external,
                    _json_object(request) or "{}",
                    actor_kind,
                    actor_agent_id,
                    stamp,
                ),
            )
            row = await self._delivery_op_row(conn, ident, delivery_id)
        return self._delivery_op_record(row)

    async def start_op(
        self,
        delivery_id: str,
        op_id: str,
        *,
        advance_delivering: bool = True,
        allowed_statuses: frozenset[str] = frozenset({"ready"}),
    ) -> tuple[DeliveryRecord, DeliveryOpRecord]:
        """CAS a planned op to running (and, for goal ops, the delivery to
        ``delivering``). The one-running partial index guarantees a single
        in-flight op per delivery (§12). ``allowed_statuses`` are the delivery
        states from which this op may legally begin — goal ops may re-act from a
        settled ``delivered``/``blocked``/``conflicted`` as an explicit new op
        (§4.1.1), teardown ops (``advance_delivering=False``) never change the
        delivery status."""
        stamp = _now_iso()
        async with self._transaction() as conn:
            delivery = await self._delivery_row(conn, delivery_id)
            op = await self._delivery_op_row(conn, op_id, delivery_id)
            if op["state"] != "planned":
                raise TaskConflictError(
                    "delivery op is not runnable",
                    current=self._delivery_record(delivery),
                )
            running = await self._fetchone(
                conn,
                "SELECT id FROM task_delivery_ops WHERE delivery_id = ? AND state='running'",
                (delivery_id,),
            )
            if running is not None:
                raise TaskConflictError(
                    "another delivery op is already running",
                    current=self._delivery_record(delivery),
                )
            if delivery["status"] not in allowed_statuses:
                raise TaskConflictError(
                    "delivery is not in a state that accepts this op",
                    current=self._delivery_record(delivery),
                )
            try:
                cursor = await conn.execute(
                    "UPDATE task_delivery_ops SET state='running', started_at=? "
                    "WHERE id=? AND state='planned'",
                    (stamp, op_id),
                )
            except aiosqlite.IntegrityError as exc:
                raise TaskConflictError(
                    "another delivery op is already running",
                    current=self._delivery_record(delivery),
                ) from exc
            if cursor.rowcount != 1:
                raise TaskConflictError(
                    "delivery op lost the start CAS",
                    current=self._delivery_record(delivery),
                )
            if advance_delivering:
                placeholders = ", ".join("?" for _ in allowed_statuses)
                await conn.execute(
                    "UPDATE task_deliveries SET status='delivering', updated_at=? "
                    f"WHERE id=? AND status IN ({placeholders})",
                    (stamp, delivery_id, *sorted(allowed_statuses)),
                )
            delivery = await self._delivery_row(conn, delivery_id)
            op = await self._delivery_op_row(conn, op_id, delivery_id)
            await self._delivery_event(
                conn,
                delivery_row=delivery,
                kind="delivery_op_started",
                actor_kind=op["actor_kind"],
                actor_agent_id=op["actor_agent_id"],
                op_row=op,
                now=stamp,
            )
        return self._delivery_record(delivery), self._delivery_op_record(op)

    async def finish_op(
        self,
        delivery_id: str,
        op_id: str,
        *,
        state: str,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
        delivery_status: str | None = None,
        delivery_fields: Mapping[str, Any] | None = None,
        reason_kind: str | None = None,
        reason_detail: str | None = None,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
    ) -> tuple[DeliveryRecord, DeliveryOpRecord]:
        """CAS a running op terminal and fold its effect into the delivery."""
        if state not in {"succeeded", "failed", "interrupted"}:
            raise TaskValidationError("invalid delivery op finish state")
        if delivery_status is not None and delivery_status not in DELIVERY_STATUSES:
            raise TaskValidationError("invalid delivery status")
        if reason_kind is not None and reason_kind not in DELIVERY_REASON_KINDS:
            raise TaskValidationError("invalid delivery reason kind")
        stamp = _now_iso()
        async with self._transaction() as conn:
            op = await self._delivery_op_row(conn, op_id, delivery_id)
            if op["state"] != "running":
                raise TaskConflictError("delivery op is not running")
            cursor = await conn.execute(
                "UPDATE task_delivery_ops SET state=?, result=?, error=?, finished_at=? "
                "WHERE id=? AND state='running'",
                (state, _json_object(result), error, stamp, op_id),
            )
            if cursor.rowcount != 1:
                raise TaskConflictError("delivery op lost the finish CAS")
            fields: dict[str, Any] = {
                "updated_at": stamp,
                "reason_kind": reason_kind,
                "reason_detail": reason_detail,
            }
            if delivery_status is not None:
                fields["status"] = delivery_status
            for key in (
                "pushed_ref", "pr_number", "pr_url", "pr_state", "merge_strategy",
                "retention", "attempt_head", "commits_ahead", "remote_name",
                "remote_url",
            ):
                if delivery_fields and delivery_fields.get(key) is not None:
                    fields[key] = delivery_fields[key]
            if delivery_fields and delivery_fields.get("diffstat") is not None:
                fields["diffstat"] = _json_object(delivery_fields["diffstat"])
            if delivery_fields and delivery_fields.get("dirty") is not None:
                fields["dirty"] = int(bool(delivery_fields["dirty"]))
            sets = ", ".join(f"{k} = ?" for k in fields)
            await conn.execute(
                f"UPDATE task_deliveries SET {sets} WHERE id = ?",
                (*fields.values(), delivery_id),
            )
            delivery = await self._delivery_row(conn, delivery_id)
            op = await self._delivery_op_row(conn, op_id, delivery_id)
            await self._delivery_event(
                conn,
                delivery_row=delivery,
                kind="delivery_op_finished",
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
                op_row=op,
                now=stamp,
            )
        return self._delivery_record(delivery), self._delivery_op_record(op)

    async def record_pr_reconcile(
        self, delivery_id: str, *, pr_number: int | None, pr_url: str | None, pr_state: str | None
    ) -> DeliveryRecord:
        """Fold an already-existing PR (found by a read-only reconcile) into a
        blocked(interrupted) delivery, settling it delivered — never a re-POST
        (§16, S3)."""
        stamp = _now_iso()
        async with self._transaction() as conn:
            row = await self._delivery_row(conn, delivery_id)
            if not (
                row["status"] == "blocked"
                and row["reason_kind"] == "interrupted"
                and row["pr_number"] is None
            ):
                return self._delivery_record(row)
            await conn.execute(
                "UPDATE task_deliveries SET pr_number=?, pr_url=?, pr_state=?, "
                "status='delivered', reason_kind=NULL, reason_detail=NULL, updated_at=? "
                "WHERE id=?",
                (pr_number, pr_url, pr_state, stamp, delivery_id),
            )
            row = await self._delivery_row(conn, delivery_id)
            await self._delivery_event(
                conn, delivery_row=row, kind="delivery_pr_reconciled",
                actor_kind="system", now=stamp,
            )
        return self._delivery_record(row)

    async def record_delivery_notification_unavailable(
        self, delivery_id: str, *, reason: str
    ) -> None:
        """One durable audit event when a terminal delivery's origin is gone."""
        stamp = _now_iso()
        async with self._transaction() as conn:
            row = await self._delivery_row(conn, delivery_id)
            task = await self._task_row(conn, row["task_id"])
            existing = await self._fetchall(
                conn,
                "SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                "AND kind='delivery_notification_unavailable'",
                (row["task_id"], row["run_id"]),
            )
            if any(
                (_load_object(event["payload"]) or {}).get("delivery_id")
                == delivery_id
                for event in existing
            ):
                return
            await self._event(
                conn,
                task=task,
                kind="delivery_notification_unavailable",
                actor_kind="system",
                run_id=row["run_id"],
                payload={"delivery_id": delivery_id, "reason": reason},
                now=stamp,
            )

    async def list_running_delivery_ops(self) -> list[DeliveryOpRecord]:
        async with self._lock:
            rows = await self._fetchall(
                self.conn,
                "SELECT * FROM task_delivery_ops WHERE state='running' ORDER BY created_at",
            )
        return [self._delivery_op_record(r) for r in rows]

    async def interrupt_running_delivery_ops(
        self, *, reason: str
    ) -> list[tuple[DeliveryRecord, DeliveryOpRecord]]:
        """Boot recovery: every op left running becomes ``interrupted`` and its
        delivery ``blocked(interrupted)``; never re-executed (§3, §16)."""
        stamp = _now_iso()
        out: list[tuple[DeliveryRecord, DeliveryOpRecord]] = []
        async with self._transaction() as conn:
            rows = await self._fetchall(
                conn, "SELECT * FROM task_delivery_ops WHERE state='running'"
            )
            for op in rows:
                detail = f"{reason} (op: {op['kind']})"
                await conn.execute(
                    "UPDATE task_delivery_ops SET state='interrupted', error=?, "
                    "finished_at=? WHERE id=? AND state='running'",
                    (detail, stamp, op["id"]),
                )
                await conn.execute(
                    "UPDATE task_deliveries SET status='blocked', "
                    "reason_kind='interrupted', reason_detail=?, updated_at=? WHERE id=?",
                    (detail, stamp, op["delivery_id"]),
                )
                delivery = await self._delivery_row(conn, op["delivery_id"])
                op_row = await self._delivery_op_row(conn, op["id"], op["delivery_id"])
                await self._delivery_event(
                    conn,
                    delivery_row=delivery,
                    kind="delivery_op_interrupted",
                    actor_kind="system",
                    op_row=op_row,
                    now=stamp,
                )
                out.append(
                    (self._delivery_record(delivery), self._delivery_op_record(op_row))
                )
        return out

    async def reset_preparing_deliveries(self) -> int:
        """Boot recovery: a delivery stuck ``preparing`` with no running op is
        reset to ``pending`` (S5). Baseline capture is idempotent, so re-accept
        recovers it."""
        stamp = _now_iso()
        count = 0
        async with self._transaction() as conn:
            rows = await self._fetchall(
                conn, "SELECT * FROM task_deliveries WHERE status='preparing'"
            )
            for d in rows:
                running = await self._fetchone(
                    conn,
                    "SELECT id FROM task_delivery_ops WHERE delivery_id=? AND state='running'",
                    (d["id"],),
                )
                if running is not None:
                    continue
                await conn.execute(
                    "UPDATE task_deliveries SET status='pending', updated_at=? "
                    "WHERE id=? AND status='preparing'",
                    (stamp, d["id"]),
                )
                row = await self._delivery_row(conn, d["id"])
                await self._delivery_event(
                    conn,
                    delivery_row=row,
                    kind="delivery_prepare_reset",
                    actor_kind="system",
                    now=stamp,
                )
                count += 1
        return count

    async def list_terminal_deliveries(
        self,
    ) -> list[tuple[TaskRecord, DeliveryRecord]]:
        """Every delivery in a terminal status, joined to its task, for boot
        outbox reconstruction (B2)."""
        async with self._lock:
            rows = await self._fetchall(
                self.conn,
                "SELECT * FROM task_deliveries WHERE status IN "
                "('delivered','conflicted','blocked','failed') ORDER BY updated_at",
            )
            out: list[tuple[TaskRecord, DeliveryRecord]] = []
            for d in rows:
                task_row = await self._fetchone(
                    self.conn, "SELECT * FROM tasks WHERE id = ?", (d["task_id"],)
                )
                if task_row is None:
                    continue
                out.append(
                    (TaskRecord.from_row(task_row), self._delivery_record(d))
                )
        return out


task_repository = TaskRepository()
