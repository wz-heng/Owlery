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
    DELIVERED_OP_KINDS,
    DELIVERY_EXTERNAL_OP_KINDS,
    DELIVERY_OP_KINDS,
    DELIVERY_REASON_KINDS,
    DELIVERY_RETENTIONS,
    DELIVERY_STATUSES,
    DEPLOYMENT_STATES,
    RELEASE_OP_KINDS,
    RELEASE_STATES,
    SWITCH_OWNED_REASON_KINDS,
    WORKSPACE_MODES,
    ArtifactRecord,
    BoardRecord,
    CommentRecord,
    DeliveryOpRecord,
    DeliveryRecord,
    DependencyRecord,
    DeploymentRecord,
    DeployLockedError,
    EventRecord,
    ReleaseDeploymentRecord,
    ReleaseDeploymentOpRecord,
    RunRecord,
    TaskCapacityError,
    TaskConflictError,
    TaskNotFoundError,
    TaskRecord,
    TaskValidationError,
)
from ..model_routing import ModelBackendError, validate_model_for_backend

_DELIVERY_TERMINAL_STATUSES = frozenset(
    {"delivered", "conflicted", "blocked", "failed"}
)

# List-page payload cap (task-board-overhaul.md §3.5): a list item never
# carries the full `body` — only this many leading characters, plus the
# card-facing enrichment. Full text is always a `show`/single-task fetch.
TASK_BODY_EXCERPT_CHARS = 200


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

    async def _agent_backend(
        self, conn: aiosqlite.Connection, agent_id: str | None
    ) -> str | None:
        """The backend ('claude-code' | 'codex') the given agent runs on, or
        None when there's no agent. Used to validate a task's model override
        against the backend it would dispatch onto."""
        if not agent_id:
            return None
        row = await self._fetchone(
            conn, "SELECT backend FROM agents WHERE id = ?", (agent_id,)
        )
        return (row["backend"] if row else None) or "claude-code"

    async def _validate_task_model(
        self,
        conn: aiosqlite.Connection,
        *,
        assignee_agent_id: str | None,
        model: str | None,
    ) -> None:
        """Reject a task model that can't run on its assignee's backend
        (budget-model-routing.md §4.3). Only enforceable when the task has an
        assignee — an unassigned task is re-checked at dispatch."""
        if not model or not assignee_agent_id:
            return
        backend = await self._agent_backend(conn, assignee_agent_id)
        try:
            validate_model_for_backend(backend, model)
        except ModelBackendError as exc:
            raise TaskValidationError(str(exc)) from exc

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
        deploy_release_ref: str = "main",
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
        deploy_release_ref = deploy_release_ref.strip()
        if not deploy_release_ref or deploy_release_ref.startswith("-"):
            raise TaskValidationError("deploy release ref is required")
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
                    "allow_local_deploy,deploy_release_ref,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                        deploy_release_ref,
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
            "deploy_release_ref",
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
        if "deploy_release_ref" in updates:
            updates["deploy_release_ref"] = str(updates["deploy_release_ref"]).strip()
            if not updates["deploy_release_ref"] or updates["deploy_release_ref"].startswith("-"):
                raise TaskValidationError("deploy release ref is required")
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
        model: str | None = None,
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
            await self._validate_task_model(
                conn, assignee_agent_id=assignee_agent_id, model=model
            )
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
                    "working_dir_override,model,created_by_kind,created_by_agent_id,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                        model,
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

    @staticmethod
    def _task_filter_clauses(
        *,
        board_id: str | None,
        status: str | None,
        assignee_agent_id: str | None,
        parent_task_id: str | None,
        root_only: bool,
        include_archived: bool,
    ) -> tuple[list[str], list[Any]]:
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
        return clauses, args

    @classmethod
    def _list_tasks_sql(
        cls,
        *,
        board_id: str | None,
        status: str | None,
        assignee_agent_id: str | None,
        parent_task_id: str | None,
        root_only: bool,
        include_archived: bool,
        limit: int,
        offset: int = 0,
    ) -> tuple[str, list[Any]]:
        if limit < 1 or limit > 1000:
            raise TaskValidationError("limit must be between 1 and 1000")
        if offset < 0:
            raise TaskValidationError("offset must not be negative")
        clauses, args = cls._task_filter_clauses(
            board_id=board_id,
            status=status,
            assignee_agent_id=assignee_agent_id,
            parent_task_id=parent_task_id,
            root_only=root_only,
            include_archived=include_archived,
        )
        sql = "SELECT * FROM tasks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY priority DESC, created_at, id LIMIT ? OFFSET ?"
        args.append(limit)
        args.append(offset)
        return sql, args

    @classmethod
    def _count_tasks_sql(
        cls,
        *,
        board_id: str | None,
        status: str | None,
        assignee_agent_id: str | None,
        parent_task_id: str | None,
        root_only: bool,
        include_archived: bool,
    ) -> tuple[str, list[Any]]:
        clauses, args = cls._task_filter_clauses(
            board_id=board_id,
            status=status,
            assignee_agent_id=assignee_agent_id,
            parent_task_id=parent_task_id,
            root_only=root_only,
            include_archived=include_archived,
        )
        sql = "SELECT COUNT(*) AS n FROM tasks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return sql, args

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
        sql, args = self._list_tasks_sql(
            board_id=board_id,
            status=status,
            assignee_agent_id=assignee_agent_id,
            parent_task_id=parent_task_id,
            root_only=root_only,
            include_archived=include_archived,
            limit=limit,
        )
        async with self._lock:
            rows = await self._fetchall(self.conn, sql, args)
        return [TaskRecord.from_row(row) for row in rows]

    @staticmethod
    def _empty_enrichment() -> dict[str, Any]:
        """The card-facing derived fields every task carries, all absent."""
        return {
            "latest_run_state": None,
            "latest_heartbeat_at": None,
            "latest_run_workspace_mode": None,
            "child_count": 0,
            "dependency_count": 0,
            "delivery": None,
        }

    async def _enrichment_for(
        self, conn: aiosqlite.Connection, task_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """Aggregate card-facing derived fields for a batch of tasks.

        Three fixed queries regardless of batch size — never N+1.  ``list_tasks``,
        ``get_tree``, the single-task detail exit, and ``publish_task_update`` all
        route through here so a card renders identically no matter which exit
        delivered it.  Board lists cap at 1000 tasks, well under SQLite's bound
        parameter limit, so a single ``IN (...)`` per query suffices.
        """
        result = {tid: self._empty_enrichment() for tid in task_ids}
        if not task_ids:
            return result
        placeholders = ",".join("?" * len(task_ids))
        ids = list(task_ids)
        # Latest run per task (max attempt_no) LEFT JOINed to its delivery, if any.
        run_rows = await self._fetchall(
            conn,
            f"SELECT r.task_id AS task_id, r.state AS state, "
            f"r.last_heartbeat_at AS last_heartbeat_at, r.workspace_mode AS workspace_mode, "
            f"d.status AS d_status, d.dirty AS d_dirty, d.commits_ahead AS d_commits_ahead, "
            f"d.pushed_ref AS d_pushed_ref, d.pr_number AS d_pr_number, d.pr_state AS d_pr_state, "
            f"d.merge_strategy AS d_merge_strategy, d.reason_kind AS d_reason_kind "
            f"FROM task_runs r JOIN ("
            f"  SELECT task_id, MAX(attempt_no) AS max_attempt FROM task_runs "
            f"  WHERE task_id IN ({placeholders}) GROUP BY task_id"
            f") latest ON latest.task_id = r.task_id AND latest.max_attempt = r.attempt_no "
            f"LEFT JOIN task_deliveries d ON d.run_id = r.id",
            ids,
        )
        for row in run_rows:
            entry = result[row["task_id"]]
            entry["latest_run_state"] = row["state"]
            entry["latest_heartbeat_at"] = row["last_heartbeat_at"]
            entry["latest_run_workspace_mode"] = row["workspace_mode"]
            if row["d_status"] is not None:
                entry["delivery"] = {
                    "status": row["d_status"],
                    "dirty": bool(row["d_dirty"]),
                    "commits_ahead": row["d_commits_ahead"],
                    "pushed_ref": row["d_pushed_ref"],
                    "pr_number": row["d_pr_number"],
                    "pr_state": row["d_pr_state"],
                    "merge_strategy": row["d_merge_strategy"],
                    "reason_kind": row["d_reason_kind"],
                }
        child_rows = await self._fetchall(
            conn,
            f"SELECT parent_task_id AS pid, COUNT(*) AS n FROM tasks "
            f"WHERE parent_task_id IN ({placeholders}) GROUP BY parent_task_id",
            ids,
        )
        for row in child_rows:
            result[row["pid"]]["child_count"] = row["n"]
        dep_rows = await self._fetchall(
            conn,
            f"SELECT task_id AS tid, COUNT(*) AS n FROM task_dependencies "
            f"WHERE task_id IN ({placeholders}) GROUP BY task_id",
            ids,
        )
        for row in dep_rows:
            result[row["tid"]]["dependency_count"] = row["n"]
        return result

    async def list_tasks_enriched(
        self,
        *,
        board_id: str | None = None,
        status: str | None = None,
        assignee_agent_id: str | None = None,
        parent_task_id: str | None = None,
        root_only: bool = False,
        include_archived: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """``list_tasks`` plus card-facing derived fields, in one lock hold."""
        sql, args = self._list_tasks_sql(
            board_id=board_id,
            status=status,
            assignee_agent_id=assignee_agent_id,
            parent_task_id=parent_task_id,
            root_only=root_only,
            include_archived=include_archived,
            limit=limit,
        )
        async with self._lock:
            rows = await self._fetchall(self.conn, sql, args)
            records = [TaskRecord.from_row(row) for row in rows]
            enrichment = await self._enrichment_for(
                self.conn, [record.id for record in records]
            )
        return [{**record.to_dict(), **enrichment[record.id]} for record in records]

    async def list_tasks_summary_page(
        self,
        *,
        board_id: str | None = None,
        status: str | None = None,
        assignee_agent_id: str | None = None,
        parent_task_id: str | None = None,
        root_only: bool = False,
        include_archived: bool = False,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Paginated, summary-shaped page for MCP ``list`` / REST ``GET
        /api/tasks`` (task-board-overhaul.md §3.5).

        Same card-facing shape as ``list_tasks_enriched`` (enrichment fields
        included, so cards render identically) except the full ``body`` is
        replaced by ``body_excerpt`` — a list page must never balloon just
        because a handful of tasks carry a huge spec doc pasted into their
        body. Full text is always a ``show``/single-task fetch. Returns
        ``(items, total)`` so callers can page to exhaustion.
        """
        sql, args = self._list_tasks_sql(
            board_id=board_id,
            status=status,
            assignee_agent_id=assignee_agent_id,
            parent_task_id=parent_task_id,
            root_only=root_only,
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )
        count_sql, count_args = self._count_tasks_sql(
            board_id=board_id,
            status=status,
            assignee_agent_id=assignee_agent_id,
            parent_task_id=parent_task_id,
            root_only=root_only,
            include_archived=include_archived,
        )
        async with self._lock:
            rows = await self._fetchall(self.conn, sql, args)
            records = [TaskRecord.from_row(row) for row in rows]
            enrichment = await self._enrichment_for(
                self.conn, [record.id for record in records]
            )
            total_row = await self._fetchone(self.conn, count_sql, count_args)
        total = int(total_row["n"]) if total_row else 0
        items: list[dict[str, Any]] = []
        for record in records:
            data = record.to_dict()
            body = data.pop("body")
            data["body_excerpt"] = body[:TASK_BODY_EXCERPT_CHARS]
            items.append({**data, **enrichment[record.id]})
        return items, total

    async def enrich_task(self, task_id: str) -> dict[str, Any]:
        """A single task's dict with the same derived fields the list exits add."""
        async with self._lock:
            record = TaskRecord.from_row(await self._task_row(self.conn, task_id))
            enrichment = await self._enrichment_for(self.conn, [task_id])
        return {**record.to_dict(), **enrichment[task_id]}

    async def get_tree(
        self, board_id: str, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        """Return a stable nested tree; dependency edges remain separate."""
        tasks = await self.list_tasks_enriched(
            board_id=board_id, include_archived=include_archived, limit=1000
        )
        nodes = {task["id"]: {**task, "children": []} for task in tasks}
        roots: list[dict[str, Any]] = []
        for task in tasks:
            node = nodes[task["id"]]
            parent_id = task["parent_task_id"]
            if parent_id and parent_id in nodes:
                nodes[parent_id]["children"].append(node)
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
        model: str | None = None,
        set_model: bool = False,
        actor_kind: str = "user",
        actor_agent_id: str | None = None,
    ) -> TaskRecord:
        """Move a triage task to `todo`, optionally rewriting its body and/or
        its model override. `set_model` distinguishes "leave model untouched"
        (False) from "set model to `model`, possibly clearing it" (True), so a
        caller can explicitly null a previously-set model
        (budget-model-routing.md §4.2)."""
        stamp = _now_iso()
        async with self._transaction() as conn:
            task = await self._task_row(conn, task_id)
            if task["status"] != "triage":
                raise TaskConflictError("only triage tasks may be specified", current=TaskRecord.from_row(task))
            if set_model:
                await self._validate_task_model(
                    conn,
                    assignee_agent_id=task["assignee_agent_id"],
                    model=model,
                )
            # Assemble the SET clauses and their params in the same order.
            sets: list[str] = []
            params: list[Any] = []
            if body is not None:
                sets.append("body = ?")
                params.append(body)
            if set_model:
                sets.append("model = ?")
                params.append(model)
            sets.append("status = 'todo'")
            sets.append("updated_at = ?")
            params.append(stamp)
            await conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?",
                (*params, task_id),
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
            # Reassignment is a backend-changing write entry (§4.3): reject a new
            # assignee whose backend can't run the task's existing model up front,
            # rather than letting it fail only after the run is claimed at dispatch.
            await self._validate_task_model(
                conn, assignee_agent_id=agent_id, model=task["model"]
            )
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

    async def _release_row(
        self, conn: aiosqlite.Connection, release_id: str
    ) -> aiosqlite.Row:
        row = await self._fetchone(
            conn, "SELECT * FROM release_deployments WHERE id = ?", (release_id,)
        )
        if row is None:
            raise TaskNotFoundError(f"release deployment {release_id!r} not found")
        return row

    async def _release_op_row(
        self, conn: aiosqlite.Connection, op_id: str, release_id: str
    ) -> aiosqlite.Row:
        row = await self._fetchone(
            conn,
            "SELECT * FROM release_deployment_ops WHERE id = ? AND release_id = ?",
            (op_id, release_id),
        )
        if row is None:
            raise TaskNotFoundError(f"release op {op_id!r} not found")
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

    async def list_delivery_siblings(
        self, board_id: str, repository: str, *, exclude_delivery_id: str
    ) -> list[DeliveryRecord]:
        """Deliveries eligible for supersede comparison against
        ``exclude_delivery_id`` (task-board-overhaul.md §3.1): same board, same
        repository, a captured head. The comparison itself (git ancestry) is
        the coordinator's job, off this lock — this is a pure DB read."""
        async with self._lock:
            rows = await self._fetchall(
                self.conn,
                "SELECT d.* FROM task_deliveries d JOIN tasks t ON t.id = d.task_id "
                "WHERE t.board_id = ? AND d.repository = ? AND d.id != ? "
                "AND d.attempt_head IS NOT NULL",
                (board_id, repository, exclude_delivery_id),
            )
        return [self._delivery_record(r) for r in rows]

    async def set_superseded_by(
        self,
        delivery_id: str,
        target_delivery_id: str | None,
        *,
        actor_kind: str = "system",
        actor_agent_id: str | None = None,
    ) -> DeliveryRecord:
        """CAS the derived collapse pointer (task-board-overhaul.md §3.1). Not
        part of the delivery status machine — no status precondition beyond the
        row existing. A no-op write (pointer already at the requested value)
        skips the event so idempotent recomputes stay quiet."""
        stamp = _now_iso()
        async with self._transaction() as conn:
            row = await self._delivery_row(conn, delivery_id)
            if (row["superseded_by_delivery_id"] or None) == target_delivery_id:
                return self._delivery_record(row)
            await conn.execute(
                "UPDATE task_deliveries SET superseded_by_delivery_id = ?, "
                "updated_at = ? WHERE id = ?",
                (target_delivery_id, stamp, delivery_id),
            )
            row = await self._delivery_row(conn, delivery_id)
            await self._delivery_event(
                conn,
                delivery_row=row,
                kind="delivery_superseded"
                if target_delivery_id
                else "delivery_supersede_cleared",
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
                now=stamp,
            )
        return self._delivery_record(row)

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
                "remote_url", "deployed_sha", "deployed_slot",
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
        """Boot recovery: every running op becomes ``interrupted`` and its
        delivery ``blocked(interrupted)``; never re-executed (§3, §16).

        ``deploy_switch`` ops are deliberately excluded: a running one is not
        simply "unknown" — the detached switcher may still be finishing the flip,
        and its terminal state is reconciled from the write-ahead journal, never
        by a blind interrupt (docs/plans/local-deploy.md §8). They are owned
        entirely by ``reconcile_deploy_switch_ops`` (which runs first at boot)."""
        stamp = _now_iso()
        out: list[tuple[DeliveryRecord, DeliveryOpRecord]] = []
        async with self._transaction() as conn:
            rows = await self._fetchall(
                conn,
                "SELECT * FROM task_delivery_ops WHERE state='running' "
                "AND kind != 'deploy_switch'",
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

    # --- Local deploy: deployments (docs/plans/local-deploy.md §6) --------

    @staticmethod
    def _deployment_record(row: Mapping[str, Any]) -> DeploymentRecord:
        values = dict(row)
        values["journal"] = _load_object(values["journal"])
        return DeploymentRecord(**values)

    async def get_deployment(self, deployment_id: str) -> DeploymentRecord:
        async with self._lock:
            row = await self._fetchone(
                self.conn, "SELECT * FROM deployments WHERE id = ?", (deployment_id,)
            )
        if row is None:
            raise TaskNotFoundError(f"deployment {deployment_id!r} not found")
        return self._deployment_record(row)

    async def list_deployments(self) -> list[DeploymentRecord]:
        async with self._lock:
            rows = await self._fetchall(
                self.conn, "SELECT * FROM deployments ORDER BY created_at, id"
            )
        return [self._deployment_record(r) for r in rows]

    # --- Release-line deployments -----------------------------------------

    @staticmethod
    def _release_deployment_record(row: Mapping[str, Any]) -> ReleaseDeploymentRecord:
        return ReleaseDeploymentRecord(**dict(row))

    async def list_release_deployments(
        self, board_id: str
    ) -> list[ReleaseDeploymentRecord]:
        async with self._lock:
            rows = await self._fetchall(
                self.conn,
                "SELECT * FROM release_deployments WHERE board_id=? "
                "ORDER BY created_at DESC, id DESC",
                (board_id,),
            )
        return [self._release_deployment_record(row) for row in rows]

    async def get_release_deployment(self, release_id: str) -> ReleaseDeploymentRecord:
        async with self._lock:
            row = await self._fetchone(
                self.conn, "SELECT * FROM release_deployments WHERE id=?", (release_id,)
            )
        if row is None:
            raise TaskNotFoundError(f"release deployment {release_id!r} not found")
        return self._release_deployment_record(row)

    async def plan_release_deployment(
        self,
        *,
        board_id: str,
        source_ref: str,
        sha: str,
        source_repo: str,
        actor_kind: str,
        actor_agent_id: str | None,
    ) -> ReleaseDeploymentRecord:
        """Persist a release candidate after its remote ref has resolved.

        The caller must resolve ``source_ref`` at the configured remote first.
        This method never accepts a moving ref in place of ``sha``; the stored
        SHA is what subsequent stage/switch operations use.

        Re-staging after new commits land is a NEW release row (release-line-
        deploy.md §3.1): any prior release for this board still ``planned``/
        ``staged`` (never switched live) becomes ``superseded`` in the same
        transaction — it describes a candidate this new plan replaces, exactly
        like ``begin_deployment_staging`` supersedes a stale ``staged``
        deployment row for the same slot. A ``staging``/``switching``/``live``
        release, or one already terminal, is untouched — in particular,
        superseding an actively ``staging`` row here would silently orphan
        the release-level lock its own stage pipeline still holds.

        Also excluded: a ``planned`` release with a ``running`` op. Planning
        and staging are two separate calls (``release_stage`` plans+starts the
        op, THEN calls ``begin_release_staging``), so there is a window where
        a release is still ``planned`` but already has committed, in-flight
        work. Superseding it there would leave that running op permanently
        stuck (``begin_release_staging``'s CAS would then fail against a
        ``superseded`` row it never anticipated racing)."""
        stamp = _now_iso()
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        async with self._transaction() as conn:
            await self._board_row(conn, board_id)
            await conn.execute(
                "UPDATE release_deployments SET state='superseded', updated_at=? "
                "WHERE board_id=? AND state IN ('planned','staged') AND id NOT IN "
                "(SELECT release_id FROM release_deployment_ops WHERE state='running')",
                (stamp, board_id),
            )
            row = await self._fetchone(
                conn,
                "SELECT COUNT(*) AS n FROM release_deployments "
                "WHERE board_id=? AND version GLOB ?",
                (board_id, f"r{day}.*"),
            )
            version = f"r{day}.{int(row['n']) + 1:02d}"
            ident = _short_id()
            await conn.execute(
                "INSERT INTO release_deployments "
                "(id,board_id,version,source_ref,sha,source_repo,state,actor_kind,"
                "actor_agent_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?, 'planned', ?,?,?,?)",
                (ident, board_id, version, source_ref, sha, source_repo,
                 actor_kind, actor_agent_id, stamp, stamp),
            )
            fresh = await self._fetchone(
                conn, "SELECT * FROM release_deployments WHERE id=?", (ident,)
            )
        return self._release_deployment_record(fresh)

    @staticmethod
    def _release_op_record(row: Mapping[str, Any]) -> ReleaseDeploymentOpRecord:
        values = dict(row)
        values["request"] = _load_object(values["request"]) or {}
        values["result"] = _load_object(values["result"])
        return ReleaseDeploymentOpRecord(**values)

    async def plan_release_op(
        self,
        release_id: str,
        *,
        kind: str,
        request: Mapping[str, Any],
        actor_kind: str,
        actor_agent_id: str | None,
    ) -> ReleaseDeploymentOpRecord:
        if kind not in RELEASE_OP_KINDS:
            raise TaskValidationError("invalid release operation")
        if actor_kind not in ACTOR_KINDS:
            raise TaskValidationError("invalid release actor kind")
        stamp = _now_iso()
        async with self._transaction() as conn:
            await self._release_row(conn, release_id)
            ident = _short_id()
            await conn.execute(
                "INSERT INTO release_deployment_ops "
                "(id,release_id,kind,state,request,actor_kind,actor_agent_id,created_at) "
                "VALUES (?,?,?,'planned',?,?,?,?)",
                (ident, release_id, kind, _json_object(request), actor_kind,
                 actor_agent_id, stamp),
            )
            row = await self._release_op_row(conn, ident, release_id)
        return self._release_op_record(row)

    async def start_release_op(
        self, release_id: str, op_id: str
    ) -> ReleaseDeploymentOpRecord:
        """CAS a planned release op to ``running`` (release-line-deploy.md §3.2,
        mirroring ``start_op``/local-deploy.md §4). The
        ``release_deployment_ops_one_running`` partial index guarantees a single
        in-flight op per release."""
        stamp = _now_iso()
        async with self._transaction() as conn:
            op = await self._release_op_row(conn, op_id, release_id)
            if op["state"] != "planned":
                raise TaskConflictError("release op is not runnable")
            try:
                cursor = await conn.execute(
                    "UPDATE release_deployment_ops SET state='running', started_at=? "
                    "WHERE id=? AND state='planned'",
                    (stamp, op_id),
                )
            except aiosqlite.IntegrityError as exc:
                raise TaskConflictError(
                    "another release op is already running"
                ) from exc
            if cursor.rowcount != 1:
                raise TaskConflictError("release op lost the start CAS")
            row = await self._release_op_row(conn, op_id, release_id)
        return self._release_op_record(row)

    async def finish_release_op(
        self,
        release_id: str,
        op_id: str,
        *,
        state: str,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> ReleaseDeploymentOpRecord:
        """CAS a running release op terminal (release-line-deploy.md §3.2,
        mirroring ``finish_op``). Never touches ``release_deployments`` itself —
        callers fold the release-row transition in the same call that settles
        the op (``begin_release_staging``/``mark_release_*``/the switch
        finalizers below) so op and release state never disagree."""
        if state not in {"succeeded", "failed", "interrupted"}:
            raise TaskValidationError("invalid release op finish state")
        stamp = _now_iso()
        async with self._transaction() as conn:
            op = await self._release_op_row(conn, op_id, release_id)
            if op["state"] != "running":
                raise TaskConflictError("release op is not running")
            cursor = await conn.execute(
                "UPDATE release_deployment_ops SET state=?, result=?, error=?, "
                "finished_at=? WHERE id=? AND state='running'",
                (state, _json_object(result), error, stamp, op_id),
            )
            if cursor.rowcount != 1:
                raise TaskConflictError("release op lost the finish CAS")
            row = await self._release_op_row(conn, op_id, release_id)
        return self._release_op_record(row)

    async def get_live_release(self, board_id: str) -> ReleaseDeploymentRecord | None:
        """The single ``live`` release row for a board (release-line-deploy.md
        §3.1), or None before that board's first release switch — including
        while its live deployment still predates release-line-deploy (a
        pre-release ``deployments`` row with ``release_id`` NULL)."""
        async with self._lock:
            row = await self._fetchone(
                self.conn,
                "SELECT * FROM release_deployments WHERE board_id=? AND state='live' LIMIT 1",
                (board_id,),
            )
        return self._release_deployment_record(row) if row else None

    async def get_staged_release(self, board_id: str) -> ReleaseDeploymentRecord | None:
        """The board's current ``staged`` release candidate, if any — the
        switch target (mirrors ``get_staged_deployment_for_delivery``)."""
        async with self._lock:
            row = await self._fetchone(
                self.conn,
                "SELECT * FROM release_deployments WHERE board_id=? AND state='staged' "
                "ORDER BY created_at DESC LIMIT 1",
                (board_id,),
            )
        return self._release_deployment_record(row) if row else None

    async def begin_release_staging(
        self, release_id: str, *, slot: str, sha: str, source_repo: str
    ) -> tuple[ReleaseDeploymentRecord, DeploymentRecord]:
        """CAS a release row ``planned`` -> ``staging`` AND insert its
        ``deployments`` staging row, atomically (release-line-deploy.md
        §3.1/§3.2). Takes BOTH the release-level lock
        (``release_deployments_one_active``) and the slot-level global deploy
        lock (``deployments_one_active``, shared with the per-run path,
        local-deploy.md §4) in one transaction — a partial lock acquisition
        (one taken, the other contended) is never observable.

        Re-staging overwrites the idle slot, so any prior ``staged`` row for
        the SAME slot describes content about to be destroyed and becomes
        ``superseded`` first, exactly like ``begin_deployment_staging``."""
        stamp = _now_iso()
        async with self._transaction() as conn:
            try:
                cursor = await conn.execute(
                    "UPDATE release_deployments SET state='staging', updated_at=? "
                    "WHERE id=? AND state='planned'",
                    (stamp, release_id),
                )
            except aiosqlite.IntegrityError as exc:
                holder = await self._fetchone(
                    conn,
                    "SELECT * FROM release_deployments WHERE state IN ('staging','switching') "
                    "AND id != ? ORDER BY created_at LIMIT 1",
                    (release_id,),
                )
                if holder is None:
                    raise
                detail = (
                    f"another release is {holder['state']} (board {holder['board_id']}, "
                    f"sha {holder['sha'][:12]})"
                )
                raise DeployLockedError(f"deploy_locked: {detail}") from exc
            if cursor.rowcount != 1:
                raise TaskConflictError(
                    f"release {release_id!r} is not 'planned'; cannot stage"
                )

            await conn.execute(
                "UPDATE deployments SET state='superseded', updated_at=? "
                "WHERE slot=? AND state='staged'",
                (stamp, slot),
            )
            dep_id = _short_id()
            try:
                await conn.execute(
                    "INSERT INTO deployments (id, delivery_id, task_id, op_id, slot, "
                    "sha, source_repo, release_id, state, journal, created_at, updated_at) "
                    "VALUES (?, NULL, NULL, NULL, ?, ?, ?, ?, 'staging', NULL, ?, ?)",
                    (dep_id, slot, sha, source_repo, release_id, stamp, stamp),
                )
            except aiosqlite.IntegrityError as exc:
                holder = await self._fetchone(
                    conn,
                    "SELECT * FROM deployments WHERE state IN ('staging','switching') "
                    "ORDER BY created_at LIMIT 1",
                )
                if holder is None:
                    raise
                detail = (
                    f"another deploy is {holder['state']} (slot {holder['slot']}, "
                    f"sha {holder['sha'][:12]})"
                )
                raise DeployLockedError(f"deploy_locked: {detail}") from exc

            release_row = await self._release_row(conn, release_id)
            dep_row = await self._fetchone(
                conn, "SELECT * FROM deployments WHERE id=?", (dep_id,)
            )
        return self._release_deployment_record(release_row), self._deployment_record(dep_row)

    async def _settle_release_and_deployment(
        self,
        release_id: str,
        deployment_id: str,
        release_state: str,
        deployment_state: str,
        *,
        error: str | None = None,
    ) -> tuple[ReleaseDeploymentRecord, DeploymentRecord]:
        """Settle a release row and its born-together ``deployments`` row in one
        transaction — the release-line mirror of ``_settle_deployment``. Both
        rows entered ``staging`` together (``begin_release_staging``), so they
        must leave it together too."""
        if release_state not in RELEASE_STATES:
            raise TaskValidationError("invalid release state")
        if deployment_state not in DEPLOYMENT_STATES:
            raise TaskValidationError("invalid deployment state")
        stamp = _now_iso()
        async with self._transaction() as conn:
            rel_cursor = await conn.execute(
                "UPDATE release_deployments SET state=?, deployment_id=?, error=?, "
                "updated_at=? WHERE id=? AND state='staging'",
                (release_state, deployment_id, error, stamp, release_id),
            )
            if rel_cursor.rowcount != 1:
                raise TaskConflictError(f"release {release_id!r} is not 'staging'")
            dep_cursor = await conn.execute(
                "UPDATE deployments SET state=?, updated_at=? WHERE id=? AND state='staging'",
                (deployment_state, stamp, deployment_id),
            )
            if dep_cursor.rowcount != 1:
                raise TaskConflictError(f"deployment {deployment_id!r} is not 'staging'")
            release_row = await self._release_row(conn, release_id)
            dep_row = await self._fetchone(
                conn, "SELECT * FROM deployments WHERE id=?", (deployment_id,)
            )
        return (
            self._release_deployment_record(release_row),
            self._deployment_record(dep_row),
        )

    async def mark_release_staged(
        self, release_id: str, *, deployment_id: str
    ) -> tuple[ReleaseDeploymentRecord, DeploymentRecord]:
        """CAS a ``staging`` release row (and its deployment) to ``staged`` —
        the settled post-stage state, releasing both locks (mirrors
        ``mark_deployment_staged``)."""
        return await self._settle_release_and_deployment(
            release_id, deployment_id, "staged", "staged"
        )

    async def mark_release_failed(
        self, release_id: str, *, deployment_id: str, error: str | None = None
    ) -> tuple[ReleaseDeploymentRecord, DeploymentRecord]:
        """CAS a ``staging`` release row (and its deployment) to ``failed`` — a
        stage step failed, releasing both locks; the running instance was
        never touched (mirrors ``mark_deployment_failed``)."""
        return await self._settle_release_and_deployment(
            release_id, deployment_id, "failed", "failed", error=error
        )

    async def fail_planned_release(
        self, release_id: str, *, error: str
    ) -> ReleaseDeploymentRecord:
        """CAS a still-``planned`` release row to ``failed`` — used when
        ``begin_release_staging`` itself loses the lock race (``deploy_locked``)
        before either the release or a ``deployments`` row ever entered
        ``staging``, so there is no born-together deployment row to settle
        alongside it (unlike ``mark_release_failed``)."""
        stamp = _now_iso()
        async with self._transaction() as conn:
            cursor = await conn.execute(
                "UPDATE release_deployments SET state='failed', error=?, updated_at=? "
                "WHERE id=? AND state='planned'",
                (error, stamp, release_id),
            )
            if cursor.rowcount != 1:
                raise TaskConflictError(f"release {release_id!r} is not 'planned'")
            row = await self._release_row(conn, release_id)
        return self._release_deployment_record(row)

    async def fail_orphan_staging_releases(
        self, *, reason: str
    ) -> list[ReleaseDeploymentRecord]:
        """Boot recovery for the release-level lock (release-line-deploy.md
        §3.3, mirrors ``fail_orphan_staging_deployments``). A ``staging``
        release row exists only between ``begin_release_staging`` and its
        settle inside one live stage call, so any seen at boot is an orphan
        from a stage whose process died. Its born-together ``deployments``
        row is failed in the same sweep, releasing the slot-level lock too."""
        stamp = _now_iso()
        out: list[ReleaseDeploymentRecord] = []
        async with self._transaction() as conn:
            rows = await self._fetchall(
                conn, "SELECT * FROM release_deployments WHERE state='staging'"
            )
            for row in rows:
                await conn.execute(
                    "UPDATE release_deployments SET state='failed', error=?, updated_at=? "
                    "WHERE id=? AND state='staging'",
                    (reason, stamp, row["id"]),
                )
                await conn.execute(
                    "UPDATE deployments SET state='failed', updated_at=? "
                    "WHERE release_id=? AND state='staging'",
                    (stamp, row["id"]),
                )
                fresh = await self._fetchone(
                    conn, "SELECT * FROM release_deployments WHERE id=?", (row["id"],)
                )
                out.append(self._release_deployment_record(fresh))
        return out

    async def interrupt_running_release_stage_ops(
        self, *, reason: str
    ) -> list[ReleaseDeploymentOpRecord]:
        """Boot recovery: every running release ``stage`` op becomes
        ``interrupted`` (mirrors ``interrupt_running_delivery_ops``).
        ``switch``/``rollback`` ops are excluded — they are journal-
        reconciled by ``reconcile_release_switch_ops``, which runs first."""
        stamp = _now_iso()
        out: list[ReleaseDeploymentOpRecord] = []
        async with self._transaction() as conn:
            rows = await self._fetchall(
                conn,
                "SELECT * FROM release_deployment_ops WHERE state='running' AND kind='stage'",
            )
            for op in rows:
                detail = f"{reason} (op: {op['kind']})"
                await conn.execute(
                    "UPDATE release_deployment_ops SET state='interrupted', error=?, "
                    "finished_at=? WHERE id=? AND state='running'",
                    (detail, stamp, op["id"]),
                )
                row = await self._release_op_row(conn, op["id"], op["release_id"])
                out.append(self._release_op_record(row))
        return out

    async def list_running_release_switch_ops(self) -> list[ReleaseDeploymentOpRecord]:
        """Every ``switch``/``rollback`` release op left ``running`` in the DB —
        the boot reconciler's worklist (mirrors
        ``list_running_deploy_switch_ops``)."""
        async with self._lock:
            rows = await self._fetchall(
                self.conn,
                "SELECT * FROM release_deployment_ops WHERE state='running' "
                "AND kind IN ('switch','rollback') ORDER BY created_at",
            )
        return [self._release_op_record(r) for r in rows]

    async def list_running_release_ops(self) -> list[ReleaseDeploymentOpRecord]:
        """Every release op left ``running`` in the DB, any kind — the quiesce
        census worklist (release-line-deploy.md §3.3)."""
        async with self._lock:
            rows = await self._fetchall(
                self.conn,
                "SELECT * FROM release_deployment_ops WHERE state='running' "
                "ORDER BY created_at",
            )
        return [self._release_op_record(r) for r in rows]

    async def begin_release_switching(
        self,
        *,
        release_id: str | None,
        deployment_id: str,
        op_id: str,
        expected_release_state: str = "staged",
        expected_deployment_state: str = "staged",
    ) -> tuple[ReleaseDeploymentRecord | None, DeploymentRecord]:
        """CAS the release row (if any) and its ``deployments`` row to
        ``switching`` in one transaction, taking both the release-level and
        the slot-level global deploy locks (mirrors
        ``begin_deployment_switching``; release-line-deploy.md §3.1/§3.3).

        ``release_id`` is ``None`` for a rollback whose target predates
        release-line-deploy (a ``deployments`` row with no ``release_id``) —
        only the slot-level lock applies then, exactly like a pre-release
        rollback today. The normal path consumes ``staged``/``staged``; a
        rollback consumes ``superseded``/``superseded``. Either lock's unique
        index rejects a concurrent holder and raises ``DeployLockedError``
        naming it; any other integrity failure is a real bug and re-raises."""
        if expected_release_state not in {"staged", "superseded"}:
            raise TaskValidationError("invalid release switch source state")
        if expected_deployment_state not in {"staged", "superseded"}:
            raise TaskValidationError("invalid deployment switch source state")
        stamp = _now_iso()
        async with self._transaction() as conn:
            try:
                dep_cursor = await conn.execute(
                    "UPDATE deployments SET state='switching', op_id=?, updated_at=? "
                    "WHERE id=? AND state=?",
                    (op_id, stamp, deployment_id, expected_deployment_state),
                )
            except aiosqlite.IntegrityError as exc:
                holder = await self._fetchone(
                    conn,
                    "SELECT * FROM deployments WHERE state IN ('staging','switching') "
                    "AND id != ? ORDER BY created_at LIMIT 1",
                    (deployment_id,),
                )
                if holder is None:
                    raise
                detail = (
                    f"another deploy is {holder['state']} (slot {holder['slot']}, "
                    f"sha {holder['sha'][:12]})"
                )
                raise DeployLockedError(f"deploy_locked: {detail}") from exc
            if dep_cursor.rowcount != 1:
                raise TaskConflictError(
                    f"deployment {deployment_id!r} is not "
                    f"{expected_deployment_state!r}; cannot switch"
                )
            release_row = None
            if release_id is not None:
                try:
                    rel_cursor = await conn.execute(
                        "UPDATE release_deployments SET state='switching', updated_at=? "
                        "WHERE id=? AND state=?",
                        (stamp, release_id, expected_release_state),
                    )
                except aiosqlite.IntegrityError as exc:
                    holder = await self._fetchone(
                        conn,
                        "SELECT * FROM release_deployments WHERE state IN "
                        "('staging','switching') AND id != ? ORDER BY created_at LIMIT 1",
                        (release_id,),
                    )
                    if holder is None:
                        raise
                    detail = (
                        f"another release is {holder['state']} "
                        f"(board {holder['board_id']}, sha {holder['sha'][:12]})"
                    )
                    raise DeployLockedError(f"deploy_locked: {detail}") from exc
                if rel_cursor.rowcount != 1:
                    raise TaskConflictError(
                        f"release {release_id!r} is not "
                        f"{expected_release_state!r}; cannot switch"
                    )
                release_row = await self._release_row(conn, release_id)
            dep_row = await self._fetchone(
                conn, "SELECT * FROM deployments WHERE id=?", (deployment_id,)
            )
        return (
            self._release_deployment_record(release_row) if release_row else None,
            self._deployment_record(dep_row),
        )

    async def record_release_switch_journal_ref(
        self, op_id: str, *, journal_ref: str, detail: Mapping[str, Any] | None = None
    ) -> ReleaseDeploymentOpRecord:
        """Persist the switcher's journal reference onto the still-running
        release switch/rollback op (mirrors ``record_switch_journal_ref``)."""
        result: dict[str, Any] = {}
        if detail:
            result.update(detail)
        async with self._transaction() as conn:
            row = await self._fetchone(
                conn, "SELECT * FROM release_deployment_ops WHERE id=?", (op_id,)
            )
            if row is None:
                raise TaskNotFoundError(f"release op {op_id!r} not found")
            if row["state"] != "running":
                raise TaskConflictError("release switch op is not running")
            await conn.execute(
                "UPDATE release_deployment_ops SET journal_ref=?, result=? "
                "WHERE id=? AND state='running'",
                (journal_ref, _json_object(result) or "{}", op_id),
            )
            row = await self._fetchone(
                conn, "SELECT * FROM release_deployment_ops WHERE id=?", (op_id,)
            )
        return self._release_op_record(row)

    async def _finalize_release_switch(
        self,
        op_id: str,
        *,
        op_state: str,
        op_error: str | None,
        op_result: Mapping[str, Any] | None,
        deployment_state: str | None,
        make_live: bool = False,
        release_terminal_state: str | None = None,
        release_error: str | None = None,
        journal_excerpt: Mapping[str, Any] | None = None,
        target_slot: str | None = None,
        target_sha: str | None = None,
    ) -> tuple[ReleaseDeploymentOpRecord, DeploymentRecord | None, ReleaseDeploymentRecord | None]:
        """One-transaction terminal for a running release switch/rollback op —
        the release-line mirror of ``_finalize_switch`` (release-line-deploy.md
        §3.1/§3.3): CAS the op terminal, move the bound ``switching``
        deployment to its final state (superseding the prior ``live`` on a
        successful ``make_live``), settle the TARGET release row, and emit a
        board audit event.

        The target release is resolved from the ``deployments`` row's own
        ``release_id`` — the row this specific handoff attempt is flipping to
        — never from the op's ``release_id`` (which, for a rollback, is the
        release being ABANDONED, not the one this attempt targets: a failed
        rollback must revert the destination, not silently relabel the
        still-live source). It is ``None`` for a rollback whose target
        predates release-line-deploy (a ``deployments`` row with no
        ``release_id`` — §3.1 point 3): the deployment row alone then carries
        the outcome. On success the target is promoted to ``live`` and
        whichever release was previously ``live`` (regardless of which
        op/board) is superseded — generic, mirroring ``_finalize_switch``'s
        "retire the prior live row FIRST"."""
        stamp = _now_iso()
        async with self._transaction() as conn:
            op = await self._fetchone(
                conn, "SELECT * FROM release_deployment_ops WHERE id=?", (op_id,)
            )
            if op is None:
                raise TaskNotFoundError(f"release op {op_id!r} not found")
            if op["state"] != "running":
                raise TaskConflictError("release op is not running")
            cursor = await conn.execute(
                "UPDATE release_deployment_ops SET state=?, error=?, result=?, "
                "finished_at=? WHERE id=? AND state='running'",
                (op_state, op_error, _json_object(op_result), stamp, op_id),
            )
            if cursor.rowcount != 1:
                raise TaskConflictError("release op lost the finish CAS")

            deployment: DeploymentRecord | None = None
            dep_row = await self._fetchone(
                conn,
                "SELECT * FROM deployments WHERE op_id=? ORDER BY created_at DESC LIMIT 1",
                (op_id,),
            )
            if dep_row is None and target_slot is not None and target_sha is not None:
                dep_row = await self._fetchone(
                    conn,
                    "SELECT * FROM deployments WHERE slot=? AND sha=? "
                    "AND state IN ('staged', 'switching', 'superseded') "
                    "ORDER BY created_at DESC LIMIT 1",
                    (target_slot, target_sha),
                )
            target_release_id = dep_row["release_id"] if dep_row is not None else None
            if dep_row is not None and deployment_state is not None:
                if make_live:
                    await conn.execute(
                        "UPDATE deployments SET state='superseded', updated_at=? "
                        "WHERE state='live' AND id != ?",
                        (stamp, dep_row["id"]),
                    )
                await conn.execute(
                    "UPDATE deployments SET state=?, op_id=?, "
                    "journal=COALESCE(?, journal), updated_at=? WHERE id=?",
                    (deployment_state, op_id, _json_object(journal_excerpt), stamp,
                     dep_row["id"]),
                )
                fresh = await self._fetchone(
                    conn, "SELECT * FROM deployments WHERE id=?", (dep_row["id"],)
                )
                deployment = self._deployment_record(fresh)

            if make_live:
                # Whichever release is currently live is being replaced —
                # generic regardless of which op/board it belongs to, mirroring
                # `_finalize_switch`'s "retire the prior live row FIRST".
                await conn.execute(
                    "UPDATE release_deployments SET state='superseded', updated_at=? "
                    "WHERE state='live'",
                    (stamp,),
                )
                if target_release_id is not None:
                    await conn.execute(
                        "UPDATE release_deployments SET state='live', updated_at=? "
                        "WHERE id=?",
                        (stamp, target_release_id),
                    )
            elif release_terminal_state is not None and target_release_id is not None:
                fields: dict[str, Any] = {
                    "state": release_terminal_state, "updated_at": stamp,
                    "error": release_error,
                }
                sets = ", ".join(f"{k}=?" for k in fields)
                await conn.execute(
                    f"UPDATE release_deployments SET {sets} WHERE id=?",
                    (*fields.values(), target_release_id),
                )

            release_row = None
            if target_release_id is not None:
                release_row = await self._release_row(conn, target_release_id)
            op_row = await self._release_op_row(conn, op_id, op["release_id"])
            board_row = await self._board_row(
                conn, (release_row or await self._release_row(conn, op["release_id"]))["board_id"]
            )
            await self._board_event(
                conn,
                board=board_row,
                kind="release_op_finished",
                actor_kind="system",
                payload={
                    "release": (
                        self._release_deployment_record(release_row).to_dict()
                        if release_row else None
                    ),
                    "op": self._release_op_record(op_row).to_dict(),
                },
                now=stamp,
            )
        return (
            self._release_op_record(op_row),
            deployment,
            self._release_deployment_record(release_row) if release_row else None,
        )

    async def finalize_release_switched(
        self, op_id: str, *, deployed_sha: str, deployed_slot: str,
        journal_excerpt: Mapping[str, Any] | None = None,
    ) -> tuple[ReleaseDeploymentOpRecord, DeploymentRecord | None, ReleaseDeploymentRecord | None]:
        """§8-equivalent ``switched_ok``: op ``succeeded``, deployment ->
        ``live`` (prior live -> ``superseded``), release -> ``live`` (prior
        live release -> ``superseded``). Used for both a forward switch and a
        successful rollback — the release-level effect is identical, only the
        op ``kind`` differs (mirrors ``finalize_deploy_switched``)."""
        return await self._finalize_release_switch(
            op_id, op_state="succeeded", op_error=None,
            op_result={"deployed_sha": deployed_sha, "deployed_slot": deployed_slot},
            deployment_state="live", make_live=True,
            journal_excerpt=journal_excerpt,
            target_slot=deployed_slot, target_sha=deployed_sha,
        )

    async def finalize_release_rolled_back(
        self, op_id: str, *, reason: str,
        journal_excerpt: Mapping[str, Any] | None = None,
        target_slot: str | None = None, target_sha: str | None = None,
    ) -> tuple[ReleaseDeploymentOpRecord, DeploymentRecord | None, ReleaseDeploymentRecord | None]:
        """§8-equivalent ``rolled_back(reason)``: op ``failed``, deployment ->
        ``rolled_back``, the TARGET release (the one this attempt tried to
        make live) -> ``rolled_back`` (mirrors ``finalize_deploy_rolled_back``).
        The target is ``None`` for a pre-release-era target — the deployment
        row alone carries the outcome then."""
        return await self._finalize_release_switch(
            op_id, op_state="failed", op_error=reason, op_result={"reason": reason},
            deployment_state="rolled_back",
            release_terminal_state="rolled_back", release_error=reason,
            journal_excerpt=journal_excerpt,
            target_slot=target_slot, target_sha=target_sha,
        )

    async def finalize_release_rollback_incomplete(
        self, op_id: str, *, reason: str,
        journal_excerpt: Mapping[str, Any] | None = None,
        target_slot: str | None = None, target_sha: str | None = None,
    ) -> tuple[ReleaseDeploymentOpRecord, DeploymentRecord | None, ReleaseDeploymentRecord | None]:
        """§8-equivalent ``rollback_incomplete(stage)``: never auto-repaired
        (mirrors ``finalize_deploy_rollback_incomplete``)."""
        return await self._finalize_release_switch(
            op_id, op_state="failed", op_error=reason,
            op_result={"reason": reason, "rollback": "incomplete"},
            deployment_state="failed",
            release_terminal_state="failed", release_error=reason,
            journal_excerpt=journal_excerpt,
            target_slot=target_slot, target_sha=target_sha,
        )

    async def finalize_release_old_wont_die(
        self, op_id: str, *,
        reason: str = "old server did not exit in time",
        journal_excerpt: Mapping[str, Any] | None = None,
        target_slot: str | None = None, target_sha: str | None = None,
    ) -> tuple[ReleaseDeploymentOpRecord, DeploymentRecord | None, ReleaseDeploymentRecord | None]:
        """§8-equivalent ``old_wont_die``: the flip never happened; deployment
        reverts ``switching`` -> ``staged``, and the target release reverts
        to ``staged`` (still deployable, lock released) — mirrors
        ``finalize_deploy_old_wont_die``."""
        return await self._finalize_release_switch(
            op_id, op_state="failed", op_error=reason, op_result={"reason": reason},
            deployment_state="staged",
            release_terminal_state="staged", release_error=reason,
            journal_excerpt=journal_excerpt,
            target_slot=target_slot, target_sha=target_sha,
        )

    async def finalize_release_interrupted(
        self, op_id: str, *, reason: str,
        journal_excerpt: Mapping[str, Any] | None = None,
        target_slot: str | None = None, target_sha: str | None = None,
    ) -> tuple[ReleaseDeploymentOpRecord, DeploymentRecord | None, ReleaseDeploymentRecord | None]:
        """§8-equivalent ``handoff``-only / stale non-terminal: op
        ``interrupted``, deployment -> ``failed``, the target release ->
        ``failed`` — never an auto-repair (mirrors
        ``finalize_deploy_interrupted``)."""
        return await self._finalize_release_switch(
            op_id, op_state="interrupted", op_error=reason, op_result={"reason": reason},
            deployment_state="failed",
            release_terminal_state="failed", release_error=reason,
            journal_excerpt=journal_excerpt,
            target_slot=target_slot, target_sha=target_sha,
        )

    async def get_live_deployment(self) -> DeploymentRecord | None:
        """The single ``live`` deployment row — what the instance is running
        (docs/plans/local-deploy.md §6), or None before the first switch."""
        async with self._lock:
            row = await self._fetchone(
                self.conn, "SELECT * FROM deployments WHERE state='live' LIMIT 1"
            )
        return self._deployment_record(row) if row else None

    async def get_active_deployment(self) -> DeploymentRecord | None:
        """The single ``staging``/``switching`` row that holds the global deploy
        lock, or None. At most one exists (deployments_one_active, §4)."""
        async with self._lock:
            row = await self._fetchone(
                self.conn,
                "SELECT * FROM deployments WHERE state IN ('staging','switching') "
                "ORDER BY created_at LIMIT 1",
            )
        return self._deployment_record(row) if row else None

    async def get_rollback_target(
        self, live_deployment_id: str
    ) -> DeploymentRecord | None:
        """The newest superseded slot eligible to replace this live deployment.

        A local layout has two slots, so the most recently superseded deployment
        is the one a confirmed rollback may switch to.  Requiring a different
        slot prevents a malformed historical row from selecting the live tree
        itself.
        """
        async with self._lock:
            live = await self._fetchone(
                self.conn, "SELECT slot FROM deployments WHERE id=? AND state='live'",
                (live_deployment_id,),
            )
            if live is None:
                return None
            row = await self._fetchone(
                self.conn,
                "SELECT * FROM deployments WHERE state='superseded' AND slot != ? "
                "ORDER BY updated_at DESC, created_at DESC LIMIT 1",
                (live["slot"],),
            )
        return self._deployment_record(row) if row else None

    async def get_release_for_deployment(
        self, deployment_id: str
    ) -> ReleaseDeploymentRecord | None:
        """The release row a ``deployments`` row belongs to, or ``None`` for a
        pre-release-era row (``release_id`` NULL — release-line-deploy.md
        §3.1 point 3). Used to resolve a rollback target's release row."""
        async with self._lock:
            dep = await self._fetchone(
                self.conn, "SELECT release_id FROM deployments WHERE id=?", (deployment_id,)
            )
            if dep is None or dep["release_id"] is None:
                return None
            row = await self._fetchone(
                self.conn, "SELECT * FROM release_deployments WHERE id=?", (dep["release_id"],)
            )
        return self._release_deployment_record(row) if row else None

    async def begin_deployment_staging(
        self,
        *,
        delivery_id: str | None = None,
        task_id: str | None = None,
        op_id: str | None = None,
        slot: str,
        sha: str,
        source_repo: str,
        release_id: str | None = None,
    ) -> DeploymentRecord:
        """Take the global deploy lock by inserting a ``staging`` row (§4, §5).

        Re-staging overwrites the idle slot, so any prior ``staged`` row for the
        SAME slot describes content about to be destroyed and becomes
        ``superseded`` in the same transaction (§5). If another deploy already
        holds the lock the ``deployments_one_active`` unique index rejects the
        insert and this raises ``DeployLockedError`` naming the holder.

        ``release_id`` is set for a release-line stage (release-line-deploy.md
        §3.1): ``delivery_id``/``task_id`` are then NULL — the release row, not
        a delivery, is the audit trail. A per-run stage leaves ``release_id``
        NULL, unchanged from local-deploy.md §5."""
        stamp = _now_iso()
        async with self._transaction() as conn:
            await conn.execute(
                "UPDATE deployments SET state='superseded', updated_at=? "
                "WHERE slot=? AND state='staged'",
                (stamp, slot),
            )
            ident = _short_id()
            try:
                await conn.execute(
                    "INSERT INTO deployments (id, delivery_id, task_id, op_id, slot, "
                    "sha, source_repo, release_id, state, journal, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'staging', NULL, ?, ?)",
                    (ident, delivery_id, task_id, op_id, slot, sha, source_repo,
                     release_id, stamp, stamp),
                )
            except aiosqlite.IntegrityError as exc:
                holder = await self._fetchone(
                    conn,
                    "SELECT * FROM deployments WHERE state IN ('staging','switching') "
                    "ORDER BY created_at LIMIT 1",
                )
                # Only the global-lock index (one active row) is a deploy_locked;
                # any other integrity failure (a bad delivery_id FK, a missing
                # NOT NULL) is a real bug, not a lock, and must surface as itself.
                if holder is None:
                    raise
                detail = (
                    f"another deploy is {holder['state']} (slot {holder['slot']}, "
                    f"sha {holder['sha'][:12]}, task {holder['task_id']})"
                )
                raise DeployLockedError(f"deploy_locked: {detail}") from exc
            row = await self._fetchone(
                conn, "SELECT * FROM deployments WHERE id = ?", (ident,)
            )
        return self._deployment_record(row)

    async def fail_orphan_staging_deployments(
        self, *, reason: str
    ) -> list[DeploymentRecord]:
        """Boot recovery for the global deploy lock (docs/plans/local-deploy.md
        §5). A ``staging`` row exists only between ``begin_deployment_staging``
        and its settle, both inside one live ``deploy_stage`` call — so any
        ``staging`` row seen at boot is an orphan from a stage whose process
        died. Fail them to release ``deployments_one_active``; a stage never
        touches the running instance, so nothing else needs undoing. ``switching``
        rows are the switch op's journal-reconciled concern (§8) and are left
        untouched here."""
        stamp = _now_iso()
        out: list[DeploymentRecord] = []
        async with self._transaction() as conn:
            rows = await self._fetchall(
                conn, "SELECT * FROM deployments WHERE state='staging'"
            )
            for row in rows:
                journal = _json_object({"boot_recovery": reason})
                await conn.execute(
                    "UPDATE deployments SET state='failed', journal=?, updated_at=? "
                    "WHERE id=? AND state='staging'",
                    (journal, stamp, row["id"]),
                )
                fresh = await self._fetchone(
                    conn, "SELECT * FROM deployments WHERE id = ?", (row["id"],)
                )
                out.append(self._deployment_record(fresh))
        return out

    async def mark_deployment_staged(
        self, deployment_id: str, *, journal: Mapping[str, Any] | None = None
    ) -> DeploymentRecord:
        """CAS a ``staging`` row to ``staged`` — the settled post-stage state,
        which releases the global lock (§5, §6)."""
        return await self._settle_deployment(
            deployment_id, "staged", from_state="staging", journal=journal
        )

    async def mark_deployment_failed(
        self, deployment_id: str, *, journal: Mapping[str, Any] | None = None
    ) -> DeploymentRecord:
        """CAS a ``staging`` row to ``failed`` — a stage step failed, releasing
        the global lock; the running instance was never touched (§5)."""
        return await self._settle_deployment(
            deployment_id, "failed", from_state="staging", journal=journal
        )

    async def _settle_deployment(
        self,
        deployment_id: str,
        state: str,
        *,
        from_state: str,
        journal: Mapping[str, Any] | None,
    ) -> DeploymentRecord:
        if state not in DEPLOYMENT_STATES:
            raise TaskValidationError("invalid deployment state")
        stamp = _now_iso()
        async with self._transaction() as conn:
            cursor = await conn.execute(
                "UPDATE deployments SET state=?, journal=COALESCE(?, journal), "
                "updated_at=? WHERE id=? AND state=?",
                (state, _json_object(journal), stamp, deployment_id, from_state),
            )
            if cursor.rowcount != 1:
                raise TaskConflictError(
                    f"deployment {deployment_id!r} is not {from_state!r}"
                )
            row = await self._fetchone(
                conn, "SELECT * FROM deployments WHERE id = ?", (deployment_id,)
            )
        return self._deployment_record(row)

    # --- Local deploy: deploy_switch op + boot reconciliation (§7/§8) ------

    async def get_staged_deployment_for_delivery(
        self, delivery_id: str
    ) -> DeploymentRecord | None:
        """The `staged` deployment row for a delivery — the switch target
        (docs/plans/local-deploy.md §6). None if nothing is staged for it."""
        async with self._lock:
            row = await self._fetchone(
                self.conn,
                "SELECT * FROM deployments WHERE delivery_id=? AND state='staged' "
                "ORDER BY created_at DESC LIMIT 1",
                (delivery_id,),
            )
        return self._deployment_record(row) if row else None

    async def list_running_deploy_switch_ops(self) -> list[DeliveryOpRecord]:
        """Every `deploy_switch` op left `running` in the DB — the boot
        reconciler's worklist (§8). Empty in the common boot (no open deploy),
        so the reconciler reads no journal at all: the cheap fast path."""
        async with self._lock:
            rows = await self._fetchall(
                self.conn,
                "SELECT * FROM task_delivery_ops WHERE state='running' "
                "AND kind='deploy_switch' ORDER BY created_at",
            )
        return [self._delivery_op_record(r) for r in rows]

    async def begin_deployment_switching(
        self, *, deployment_id: str, op_id: str, expected_state: str = "staged"
    ) -> DeploymentRecord:
        """CAS a switch target to `switching`, binding it to the switch op
        and taking the global deploy lock (docs/plans/local-deploy.md §6/§7.2).

        `switching` is a `deployments_one_active` state, so a concurrent
        staging/switching deploy makes the unique index reject this and raise
        `DeployLockedError` naming the holder — the same lock the stage op
        contends for. The normal path consumes `staged`; the only other allowed
        source is `superseded`, for an explicitly confirmed rollback (§10/§12).
        Any other source is a lost race (`TaskConflictError`)."""
        if expected_state not in {"staged", "superseded"}:
            raise TaskValidationError("invalid deploy switch source state")
        stamp = _now_iso()
        async with self._transaction() as conn:
            try:
                cursor = await conn.execute(
                    "UPDATE deployments SET state='switching', op_id=?, updated_at=? "
                    "WHERE id=? AND state=?",
                    (op_id, stamp, deployment_id, expected_state),
                )
            except aiosqlite.IntegrityError as exc:
                holder = await self._fetchone(
                    conn,
                    "SELECT * FROM deployments WHERE state IN ('staging','switching') "
                    "AND id != ? ORDER BY created_at LIMIT 1",
                    (deployment_id,),
                )
                if holder is None:
                    raise
                detail = (
                    f"another deploy is {holder['state']} (slot {holder['slot']}, "
                    f"sha {holder['sha'][:12]}, task {holder['task_id']})"
                )
                raise DeployLockedError(f"deploy_locked: {detail}") from exc
            if cursor.rowcount != 1:
                raise TaskConflictError(
                    f"deployment {deployment_id!r} is not {expected_state!r}; cannot switch"
                )
            row = await self._fetchone(
                conn, "SELECT * FROM deployments WHERE id=?", (deployment_id,)
            )
        return self._deployment_record(row)

    async def record_switch_journal_ref(
        self, op_id: str, *, journal_ref: str, detail: Mapping[str, Any] | None = None
    ) -> DeliveryOpRecord:
        """Persist the switcher's journal reference onto the still-running switch
        op (§7.2 step 3), result-partial. A crash between here and the switcher's
        first journal line is then reconcilable — the op points at exactly which
        journal + op id the boot reconciler must read."""
        result: dict[str, Any] = {"journal_ref": journal_ref}
        if detail:
            result.update(detail)
        async with self._transaction() as conn:
            row = await self._fetchone(
                conn, "SELECT * FROM task_delivery_ops WHERE id=?", (op_id,)
            )
            if row is None:
                raise TaskNotFoundError(f"delivery op {op_id!r} not found")
            if row["state"] != "running":
                raise TaskConflictError("switch op is not running")
            await conn.execute(
                "UPDATE task_delivery_ops SET result=? WHERE id=? AND state='running'",
                (_json_object(result), op_id),
            )
            row = await self._fetchone(
                conn, "SELECT * FROM task_delivery_ops WHERE id=?", (op_id,)
            )
        return self._delivery_op_record(row)

    async def _switch_success_status(
        self, conn: aiosqlite.Connection, delivery_id: str
    ) -> tuple[str, str | None, str | None, bool]:
        """The `(status, reason_kind, reason_detail, set_reason)` a successful
        `switched_ok` settles the delivery to: only a blocker THIS switch
        machinery itself produced — a member of `SWITCH_OWNED_REASON_KINDS`
        (docs/plans/local-deploy.md §9's switch-op reason kinds) — is
        cleared, landing `delivered` if a
        push/PR/merge op ever succeeded on this delivery, else `ready`. Any
        other blocked/conflicted status (a real git conflict, `deploy_locked`,
        `op_failed`, …) is a delivery-pipeline concern the switch never
        resolves, so it is returned unchanged."""
        row = await self._delivery_row(conn, delivery_id)
        prior_status, prior_reason_kind = row["status"], row["reason_kind"]
        if not (
            prior_status in {"blocked", "conflicted"}
            and prior_reason_kind in SWITCH_OWNED_REASON_KINDS
        ):
            return (prior_status, prior_reason_kind, row["reason_detail"], False)
        op_rows = await self._fetchall(
            conn,
            "SELECT state, kind FROM task_delivery_ops WHERE delivery_id=?",
            (delivery_id,),
        )
        was_delivered = any(
            r["state"] == "succeeded" and r["kind"] in DELIVERED_OP_KINDS
            for r in op_rows
        )
        return ("delivered" if was_delivered else "ready", None, None, True)

    async def _finalize_switch(
        self,
        op_id: str,
        *,
        op_state: str,
        op_error: str | None,
        op_result: Mapping[str, Any] | None,
        deployment_state: str | None,
        make_live: bool = False,
        delivery_status: str | None,
        set_reason: bool,
        reason_kind: str | None = None,
        reason_detail: str | None = None,
        deployed_sha: str | None = None,
        deployed_slot: str | None = None,
        journal_excerpt: Mapping[str, Any] | None = None,
        target_slot: str | None = None,
        target_sha: str | None = None,
        clear_switch_owned_blocker: bool = False,
    ) -> tuple[DeliveryRecord, DeploymentRecord | None, DeliveryOpRecord]:
        """One-transaction terminal for a running `deploy_switch` op (§8): CAS the
        op terminal, move the bound `switching` deployment to its final state
        (superseding the prior `live` on a successful `make_live`), fold the
        delivery, and emit the shared audit event. All boot-reconciliation rows
        of the §8 table go through here so op/deployment/delivery never disagree.

        `clear_switch_owned_blocker` (only `finalize_deploy_switched` sets it):
        an explicit, final `switched_ok` retry must clear a blocker that an
        EARLIER switch attempt on this same delivery produced — `not_idle`,
        `health_failed`, `old_wont_die` (docs/plans/local-deploy.md §9) — the
        same "succeed back to ready/delivered" contract §4 already gives a
        green `deploy_stage` (`_stage_success_status`). It must NEVER clear a
        real git block/conflict (`conflict`, `deploy_locked`, `op_failed`, …);
        those reasons are left untouched by construction (they are simply not
        in `SWITCH_OWNED_REASON_KINDS`).

        `target_slot`/`target_sha` (the journal handoff's `to_slot`/`new_sha`) are
        a restricted fallback locator for the bound deployment row. The primary
        lookup is by `op_id`, but that binding lives on the deployment row itself
        (written by `begin_deployment_switching`, §7.2 step 3) — a row a rollback
        can revert to its pre-handoff `staged` state by restoring the pre-switch
        DB snapshot over it (§7.4). That revert does NOT clear `op_id` to NULL:
        it restores whatever op_id the row held before THIS switch (typically the
        earlier `deploy_stage` op — staging→staged never clears it, §5/§6), which
        is simply the wrong op for this lookup. A crash between the journal
        `handoff` line and the CAS can also leave the row genuinely un-bound in
        the first place. Either way the op-id lookup then finds nothing, so we
        fall back to this delivery's still-eligible `staged`/`switching`/`superseded`
        row for the exact
        slot+sha this op targeted — never a terminal row, never a different
        switch attempt."""
        stamp = _now_iso()
        async with self._transaction() as conn:
            op = await self._fetchone(
                conn, "SELECT * FROM task_delivery_ops WHERE id=?", (op_id,)
            )
            if op is None:
                raise TaskNotFoundError(f"delivery op {op_id!r} not found")
            if op["state"] != "running":
                raise TaskConflictError("switch op is not running")
            cursor = await conn.execute(
                "UPDATE task_delivery_ops SET state=?, error=?, result=?, finished_at=? "
                "WHERE id=? AND state='running'",
                (op_state, op_error, _json_object(op_result), stamp, op_id),
            )
            if cursor.rowcount != 1:
                raise TaskConflictError("switch op lost the finish CAS")

            if clear_switch_owned_blocker:
                delivery_status, reason_kind, reason_detail, set_reason = (
                    await self._switch_success_status(conn, op["delivery_id"])
                )

            deployment: DeploymentRecord | None = None
            dep_row = await self._fetchone(
                conn,
                "SELECT * FROM deployments WHERE op_id=? ORDER BY created_at DESC LIMIT 1",
                (op_id,),
            )
            if dep_row is None and target_slot is not None and target_sha is not None:
                dep_row = await self._fetchone(
                    conn,
                    "SELECT * FROM deployments WHERE delivery_id=? AND slot=? "
                    "AND sha=? AND state IN ('staged', 'switching', 'superseded') "
                    "ORDER BY created_at DESC LIMIT 1",
                    (op["delivery_id"], target_slot, target_sha),
                )
            if dep_row is not None and deployment_state is not None:
                if make_live:
                    # Retire the prior live row FIRST, so `deployments_one_live`
                    # never sees two live rows mid-transaction (§6).
                    await conn.execute(
                        "UPDATE deployments SET state='superseded', updated_at=? "
                        "WHERE state='live' AND id != ?",
                        (stamp, dep_row["id"]),
                    )
                await conn.execute(
                    "UPDATE deployments SET state=?, op_id=?, "
                    "journal=COALESCE(?, journal), updated_at=? WHERE id=?",
                    (deployment_state, op_id, _json_object(journal_excerpt), stamp,
                     dep_row["id"]),
                )
                fresh = await self._fetchone(
                    conn, "SELECT * FROM deployments WHERE id=?", (dep_row["id"],)
                )
                deployment = self._deployment_record(fresh)

            fields: dict[str, Any] = {"updated_at": stamp}
            if delivery_status is not None:
                fields["status"] = delivery_status
            if set_reason:
                fields["reason_kind"] = reason_kind
                fields["reason_detail"] = reason_detail
            if deployed_sha is not None:
                fields["deployed_sha"] = deployed_sha
            if deployed_slot is not None:
                fields["deployed_slot"] = deployed_slot
            sets = ", ".join(f"{k}=?" for k in fields)
            await conn.execute(
                f"UPDATE task_deliveries SET {sets} WHERE id=?",
                (*fields.values(), op["delivery_id"]),
            )
            delivery_row = await self._delivery_row(conn, op["delivery_id"])
            op_row = await self._delivery_op_row(conn, op_id, op["delivery_id"])
            await self._delivery_event(
                conn, delivery_row=delivery_row, kind="delivery_op_finished",
                actor_kind="system", op_row=op_row, now=stamp,
            )
        return (
            self._delivery_record(delivery_row),
            deployment,
            self._delivery_op_record(op_row),
        )

    async def finalize_deploy_switched(
        self, op_id: str, *, deployed_sha: str, deployed_slot: str,
        journal_excerpt: Mapping[str, Any] | None = None,
    ) -> tuple[DeliveryRecord, DeploymentRecord | None, DeliveryOpRecord]:
        """§8 `switched_ok`: op `succeeded`, deployment → `live` (prior live →
        `superseded`), delivery folds `deployed_sha`/`deployed_slot`. A settled
        git status (ready/delivered) is left as-is, as is a real git block/
        conflict; a blocker THIS switch machinery produced on an earlier
        attempt (`not_idle`/`health_failed`/`old_wont_die`) is cleared — a
        successful, explicit retry undoes it, same as a green `deploy_stage`
        undoes `stage_failed`/`deploy_locked` (§4, §9)."""
        return await self._finalize_switch(
            op_id, op_state="succeeded", op_error=None,
            op_result={"deployed_sha": deployed_sha, "deployed_slot": deployed_slot},
            deployment_state="live", make_live=True,
            delivery_status=None, set_reason=False,
            deployed_sha=deployed_sha, deployed_slot=deployed_slot,
            journal_excerpt=journal_excerpt,
            target_slot=deployed_slot, target_sha=deployed_sha,
            clear_switch_owned_blocker=True,
        )

    async def finalize_deploy_rolled_back(
        self, op_id: str, *, reason: str,
        journal_excerpt: Mapping[str, Any] | None = None,
        target_slot: str | None = None, target_sha: str | None = None,
    ) -> tuple[DeliveryRecord, DeploymentRecord | None, DeliveryOpRecord]:
        """§8 `rolled_back(reason)`: op `failed(health_failed)`, deployment →
        `rolled_back`, delivery `blocked(health_failed)` with the journal detail."""
        return await self._finalize_switch(
            op_id, op_state="failed", op_error=reason, op_result={"reason": reason},
            deployment_state="rolled_back",
            delivery_status="blocked", set_reason=True,
            reason_kind="health_failed", reason_detail=reason,
            journal_excerpt=journal_excerpt,
            target_slot=target_slot, target_sha=target_sha,
        )

    async def finalize_deploy_rollback_incomplete(
        self, op_id: str, *, reason: str,
        journal_excerpt: Mapping[str, Any] | None = None,
        target_slot: str | None = None, target_sha: str | None = None,
    ) -> tuple[DeliveryRecord, DeploymentRecord | None, DeliveryOpRecord]:
        """§8 `rollback_incomplete(stage)`: a rollback that could not be proven
        total. op `failed(health_failed)`, deployment → `failed`, delivery
        `blocked` — never auto-repaired; `current` + `switcher.log` are evidence."""
        return await self._finalize_switch(
            op_id, op_state="failed", op_error=reason,
            op_result={"reason": reason, "rollback": "incomplete"},
            deployment_state="failed",
            delivery_status="blocked", set_reason=True,
            reason_kind="health_failed", reason_detail=reason,
            journal_excerpt=journal_excerpt,
            target_slot=target_slot, target_sha=target_sha,
        )

    async def finalize_deploy_old_wont_die(
        self, op_id: str, *, reason: str = "old server did not exit in time",
        journal_excerpt: Mapping[str, Any] | None = None,
        target_slot: str | None = None, target_sha: str | None = None,
    ) -> tuple[DeliveryRecord, DeploymentRecord | None, DeliveryOpRecord]:
        """§8 `old_wont_die`: the flip never happened. op `failed(old_wont_die)`,
        deployment reverts `switching` → `staged` (still deployable, lock
        released), delivery `blocked(old_wont_die)`."""
        return await self._finalize_switch(
            op_id, op_state="failed", op_error=reason, op_result={"reason": reason},
            deployment_state="staged",
            delivery_status="blocked", set_reason=True,
            reason_kind="old_wont_die", reason_detail=reason,
            journal_excerpt=journal_excerpt,
            target_slot=target_slot, target_sha=target_sha,
        )

    async def finalize_deploy_interrupted(
        self, op_id: str, *, reason: str,
        journal_excerpt: Mapping[str, Any] | None = None,
        target_slot: str | None = None, target_sha: str | None = None,
    ) -> tuple[DeliveryRecord, DeploymentRecord | None, DeliveryOpRecord]:
        """§8 `handoff`-only / stale non-terminal: op `interrupted`, deployment →
        `failed`, delivery `blocked(interrupted)`. A human inspects `current`, the
        journal, and `switcher.log` — never an auto-repair."""
        return await self._finalize_switch(
            op_id, op_state="interrupted", op_error=reason, op_result={"reason": reason},
            deployment_state="failed",
            delivery_status="blocked", set_reason=True,
            reason_kind="interrupted", reason_detail=reason,
            journal_excerpt=journal_excerpt,
            target_slot=target_slot, target_sha=target_sha,
        )


task_repository = TaskRepository()
