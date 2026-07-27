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

# Git delivery closure (task-git-delivery.md §4, §11).
DELIVERY_STATUSES = frozenset(
    {"pending", "preparing", "ready", "delivering",
     "delivered", "conflicted", "blocked", "failed"}
)
DELIVERY_OP_KINDS = frozenset(
    {"commit", "push", "pull_request", "merge", "branch_delete", "worktree_remove"}
)
# Which op kinds mutate state outside Owlery's own database (§3, §4.2).
DELIVERY_EXTERNAL_OP_KINDS = frozenset({"push", "pull_request", "merge"})
DELIVERY_OP_STATES = frozenset(
    {"planned", "running", "succeeded", "failed", "interrupted"}
)
DELIVERY_REASON_KINDS = frozenset(
    {"no_remote", "no_connector", "ambiguous_connector", "base_ambiguous",
     "conflict", "destructive", "interrupted", "op_failed", "push_auth_failed",
     "workspace_gone_no_effect"}
)
DELIVERY_RETENTIONS = frozenset(
    {"keep", "remove_worktree_keep_branch", "remove_all"}
)
DELIVERY_MERGE_STRATEGIES = frozenset({"fast_forward_only", "no_conflict_merge"})


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
    git_delivery_remote: str
    git_delivery_retention: str
    git_delivery_author_name: str
    git_delivery_author_email: str
    git_delivery_default_draft_pr: bool
    git_delivery_default_merge: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> BoardRecord:
        values = dict(row)
        values["dispatch_enabled"] = bool(values["dispatch_enabled"])
        values["archived"] = bool(values["archived"])
        values["git_delivery_default_draft_pr"] = bool(
            values["git_delivery_default_draft_pr"]
        )
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


@dataclass(frozen=True, slots=True)
class DeliveryRecord(Record):
    id: str
    task_id: str
    run_id: str
    status: str
    repository: str
    base_ref: str | None
    base_head: str | None
    attempt_branch: str
    attempt_head: str | None
    dirty: bool
    commits_ahead: int | None
    diffstat: dict[str, Any] | None
    remote_name: str | None
    remote_url: str | None
    pushed_ref: str | None
    pr_number: int | None
    pr_url: str | None
    pr_state: str | None
    merge_strategy: str | None
    retention: str | None
    reason_kind: str | None
    reason_detail: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class DeliveryOpRecord(Record):
    id: str
    delivery_id: str
    kind: str
    source_key: str
    external: bool
    state: str
    request: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    actor_kind: str
    actor_agent_id: str | None
    started_at: str | None
    finished_at: str | None
    created_at: str


class DeliveryConfirmationRequired(TaskConflictError):
    """A destructive delivery action needs an explicit typed confirmation flag.

    Surfaces as HTTP 409 with the flag name so the UI can render a typed
    confirmation (task-git-delivery.md §13). ``current`` carries the delivery
    row so the browser can reconcile without a refetch.
    """

    code = "requires_confirmation"

    def __init__(
        self,
        message: str,
        *,
        confirmation: str,
        action: str,
        current: DeliveryRecord | None = None,
    ) -> None:
        # TaskBoardError.current is typed for TaskRecord; a delivery row is also
        # a Record with to_dict(), so the router serializes it the same way.
        super().__init__(message, current=current)  # type: ignore[arg-type]
        self.confirmation = confirmation
        self.action = action
