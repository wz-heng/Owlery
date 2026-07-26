"""Typed snapshots and repository errors for the Task Board.

The repository deliberately returns immutable dataclasses instead of exposing
``aiosqlite.Row`` objects.  API/MCP layers can serialize ``to_dict()`` while
dispatcher code gets stable attribute access and no accidental write-through.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


TASK_STATUSES = frozenset({"triage", "todo", "ready", "running", "blocked", "done"})
RUN_STATES = frozenset(
    {"running", "completed", "blocked", "failed", "cancelled", "interrupted"}
)
BLOCKED_KINDS = frozenset(
    {"input", "capability", "failure", "protocol", "cancelled", "interrupted"}
)
WORKSPACE_MODES = frozenset({"shared", "copy", "git_worktree"})
ACTOR_KINDS = frozenset({"user", "agent", "schedule", "api", "system"})


class TaskBoardError(RuntimeError):
    """Base class for stable repository errors consumed by REST and MCP."""

    code = "task_board_error"

    def __init__(self, message: str, *, current: TaskRecord | None = None) -> None:
        super().__init__(message)
        self.current = current


class TaskNotFoundError(TaskBoardError):
    code = "not_found"


class TaskConflictError(TaskBoardError):
    code = "conflict"


class TaskValidationError(TaskBoardError):
    code = "validation"


class TaskCapacityError(TaskConflictError):
    code = "capacity"


@dataclass(frozen=True, slots=True)
class Record:
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BoardRecord(Record):
    id: str
    name: str
    description: str
    working_dir: str
    default_workspace_mode: str
    max_running: int | None
    max_running_per_agent: int | None
    max_tree_depth: int
    max_children_per_run: int
    max_open_tasks: int
    dispatch_enabled: bool
    archived: bool
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> BoardRecord:
        values = dict(row)
        values["dispatch_enabled"] = bool(values["dispatch_enabled"])
        values["archived"] = bool(values["archived"])
        return cls(**values)


@dataclass(frozen=True, slots=True)
class TaskRecord(Record):
    id: str
    board_id: str
    parent_task_id: str | None
    title: str
    body: str
    status: str
    assignee_agent_id: str | None
    priority: int
    origin_session_id: str | None
    idempotency_key: str | None
    scheduled_at: str | None
    workspace_mode: str | None
    working_dir_override: str | None
    current_run_id: str | None
    blocked_kind: str | None
    blocked_reason: str | None
    result_summary: str | None
    archived: bool
    created_by_kind: str
    created_by_agent_id: str | None
    created_at: str
    updated_at: str
    completed_at: str | None
    archived_at: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> TaskRecord:
        values = dict(row)
        values["archived"] = bool(values["archived"])
        return cls(**values)


@dataclass(frozen=True, slots=True)
class RunRecord(Record):
    id: str
    task_id: str
    attempt_no: int
    agent_id: str | None
    session_id: str | None
    state: str
    summary: str | None
    metadata: dict[str, Any] | None
    error: str | None
    workspace_mode: str
    workspace_path: str
    claimed_at: str
    started_at: str | None
    last_heartbeat_at: str | None
    lease_expires_at: str | None
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class CommentRecord(Record):
    id: str
    task_id: str
    run_id: str | None
    author_kind: str
    author_agent_id: str | None
    body: str
    created_at: str


@dataclass(frozen=True, slots=True)
class EventRecord(Record):
    seq: int
    board_id: str
    task_id: str | None
    run_id: str | None
    kind: str
    actor_kind: str
    actor_agent_id: str | None
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class ArtifactRecord(Record):
    id: str
    task_id: str
    run_id: str
    name: str
    stored_path: str
    source_path: str
    mime_type: str | None
    size: int
    sha256: str
    created_at: str
    deleted_at: str | None


@dataclass(frozen=True, slots=True)
class DependencyRecord(Record):
    task_id: str
    depends_on_task_id: str
    created_by_kind: str
    created_by_agent_id: str | None
    created_at: str
