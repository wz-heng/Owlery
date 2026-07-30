"""REST contract for Owlery's durable multi-agent Task Board."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Response,
    status,
)
from fastapi.responses import FileResponse
from pydantic import AliasChoices, BaseModel, Field, model_validator

from ..auth import verify_token
from ..task_board.models import (
    BLOCKED_KINDS,
    DeliveryConfirmationRequired,
    TaskBoardError,
    TaskCapacityError,
    TaskConflictError,
    TaskNotFoundError,
    TaskRecord,
    TaskValidationError,
)
from ..task_board.repository import task_repository

router = APIRouter(tags=["task-boards"])

_manager: Any = None


def set_manager(manager: Any) -> None:
    """Bind the runtime manager during application lifespan startup."""
    global _manager
    _manager = manager


def _get_manager() -> Any:
    if _manager is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "task dispatcher unavailable")
    return _manager


def _dict(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


async def _error(exc: TaskBoardError) -> HTTPException:
    if isinstance(exc, TaskNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, (TaskConflictError, TaskCapacityError)):
        code = status.HTTP_409_CONFLICT
    elif isinstance(exc, TaskValidationError):
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
    else:
        code = status.HTTP_500_INTERNAL_SERVER_ERROR
    detail: dict[str, Any] = {"code": exc.code, "message": str(exc)}
    if isinstance(exc, DeliveryConfirmationRequired):
        # A destructive delivery action refused pending a typed confirmation flag
        # (task-git-delivery.md §13); surface the exact flag and action.
        detail["confirmation"] = exc.confirmation
        detail["action"] = exc.action
    if exc.current is not None:
        # The browser upserts this authoritative snapshot into its board store
        # on a conflict, so a task must carry the same card-facing enrichment the
        # read/WS exits do — otherwise a rejected edit wipes the card's chip.
        current = (
            await _enriched(exc.current)
            if isinstance(exc.current, TaskRecord)
            else exc.current.to_dict()
        )
        detail["current"] = current
        # One-release compatibility with the independently-developed browser
        # client while generated OpenAPI aliases converge.
        detail["current_task"] = current
    return HTTPException(code, detail)


async def _run(call):
    try:
        return await call
    except TaskBoardError as exc:
        raise await _error(exc) from exc


async def _enriched(record: TaskRecord | Mapping[str, Any]) -> dict[str, Any]:
    """A task as a dict carrying the card-facing enrichment every read exit adds
    (task-card-status.md §3).

    Every mutation that returns a ``Task`` the browser upserts routes through
    here so its HTTP response matches the enriched WS payload published for the
    same change; a bare task would lose the timestamp-tie merge race and strip
    the card's delivery/run chip until reload.  Accepts either a ``TaskRecord``
    (most exits) or an already-serialized task ``Mapping`` (the manager's block
    / running-cancel paths hand back ``to_dict()``).

    Enrichment is a capability of the real repository — a lightweight test
    double without ``enrich_task`` (or a task that lost a concurrent delete
    before we could re-read it) degrades to the plain task, never a 500 or a
    masked conflict.
    """
    task_id = record["id"] if isinstance(record, Mapping) else record.id
    enrich = getattr(task_repository, "enrich_task", None)
    if enrich is None:
        return _dict(record)
    try:
        return await enrich(task_id)
    except TaskBoardError:
        return _dict(record)


async def _publish(task_id: str) -> None:
    await _get_manager().publish_task_update(task_id)


def _set_etag(response: Response, updated_at: str) -> None:
    response.headers["ETag"] = f'"{updated_at}"'


def _clean_etag(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip().removeprefix("W/").strip('"')


async def _check_precondition(
    item: Any, if_match: str | None, *, enrich: bool = False
) -> None:
    expected = _clean_etag(if_match)
    if expected is not None and expected != item.updated_at:
        # Same rule as _error's conflict snapshot: the browser upserts this, so a
        # task must be enriched or the stale-edit rejection strips the chip. Only
        # the task patch opts in; the board precondition stays a plain board.
        current = await _enriched(item) if enrich else item.to_dict()
        raise HTTPException(
            status.HTTP_412_PRECONDITION_FAILED,
            {
                "message": "stale task-board edit",
                "current": current,
                "current_task": current,
            },
        )


async def _actor(session_id: str | None) -> tuple[str, str | None, str | None]:
    """Derive agent authorship from trusted callback context, never JSON."""
    if not session_id:
        return "user", None, None
    from ..session_manager import session_manager

    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "caller session not found")
    return "agent", session.agent_id, session_id


async def _resolve_agent(name: str | None, agent_id: str | None) -> str | None:
    if name is not None and agent_id is not None:
        raise HTTPException(422, "provide agent_name or agent_id, not both")
    if agent_id is not None:
        from ..session_manager import session_manager

        agent = await session_manager.db.get_agent(agent_id)
        if agent is None or agent.get("archived"):
            raise HTTPException(404, "Agent not found")
        return agent_id
    if name is None:
        return None
    wanted = name.strip().casefold()
    if not wanted:
        return None
    from ..session_manager import session_manager

    agents = await session_manager.db.load_agents()
    matches = [a for a in agents if str(a.get("name") or "").casefold() == wanted]
    if not matches:
        raise HTTPException(404, f"No Agent named {name!r}")
    if len(matches) > 1:
        raise HTTPException(409, f"Ambiguous Agent name {name!r}")
    return str(matches[0]["id"])


def _public_artifact(value: Any) -> dict[str, Any]:
    """Never expose Owlery's host-side durable storage path to the browser."""
    item = _dict(value)
    item.pop("stored_path", None)
    if item.get("deleted_at") is None:
        item["download_url"] = (
            f"/api/tasks/{item['task_id']}/artifacts/{item['id']}"
        )
    else:
        item["download_url"] = None
    return item


class BoardCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    working_dir: str
    default_workspace_mode: Literal["shared", "copy", "git_worktree"] = "shared"
    max_running: int | None = Field(default=None, gt=0)
    max_running_per_agent: int | None = Field(default=None, gt=0)
    max_tree_depth: int = Field(default=8, ge=1, le=64)
    max_children_per_run: int = Field(default=32, ge=1, le=1000)
    max_open_tasks: int = Field(default=500, ge=1, le=100000)
    dispatch_enabled: bool = True
    git_delivery_remote: str = Field(default="origin", min_length=1, max_length=200)
    git_delivery_retention: Literal[
        "keep", "remove_worktree_keep_branch", "remove_all"
    ] = "keep"
    git_delivery_author_name: str = Field(
        default="Owlery Task", min_length=1, max_length=200
    )
    git_delivery_author_email: str = Field(
        default="owlery-tasks@localhost", min_length=1, max_length=320
    )
    git_delivery_default_draft_pr: bool = True
    git_delivery_default_merge: Literal["none", "fast_forward_only"] = "none"


class BoardPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    working_dir: str | None = None
    default_workspace_mode: Literal["shared", "copy", "git_worktree"] | None = None
    max_running: int | None = Field(default=None, gt=0)
    max_running_per_agent: int | None = Field(default=None, gt=0)
    max_tree_depth: int | None = Field(default=None, ge=1, le=64)
    max_children_per_run: int | None = Field(default=None, ge=1, le=1000)
    max_open_tasks: int | None = Field(default=None, ge=1, le=100000)
    git_delivery_remote: str | None = Field(
        default=None, min_length=1, max_length=200
    )
    git_delivery_retention: Literal[
        "keep", "remove_worktree_keep_branch", "remove_all"
    ] | None = None
    git_delivery_author_name: str | None = Field(
        default=None, min_length=1, max_length=200
    )
    git_delivery_author_email: str | None = Field(
        default=None, min_length=1, max_length=320
    )
    git_delivery_default_draft_pr: bool | None = None
    git_delivery_default_merge: Literal["none", "fast_forward_only"] | None = None
    updated_at: str | None = None


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    body: str = ""
    specified: bool = False
    parent_task_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("parent_task_id", "parent_id"),
    )
    assignee: str | None = None
    assignee_agent_id: str | None = None
    dependencies: list[str] = Field(default_factory=list, max_length=1000)
    priority: int = Field(default=0, ge=-1000, le=1000)
    idempotency_key: str | None = Field(default=None, max_length=500)
    scheduled_at: str | None = None
    workspace_mode: Literal["shared", "copy", "git_worktree"] | None = None
    working_dir_override: str | None = None

    @model_validator(mode="after")
    def validate_assignee(self):
        if self.assignee is not None and self.assignee_agent_id is not None:
            raise ValueError("provide assignee or assignee_agent_id, not both")
        if len(self.dependencies) != len(set(self.dependencies)):
            raise ValueError("dependencies must not contain duplicates")
        return self


class TaskPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    body: str | None = None
    priority: int | None = Field(default=None, ge=-1000, le=1000)
    scheduled_at: str | None = None
    workspace_mode: Literal["shared", "copy", "git_worktree"] | None = None
    working_dir_override: str | None = None
    parent_task_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("parent_task_id", "parent_id"),
    )
    updated_at: str | None = None


class AssignRequest(BaseModel):
    agent_name: str | None = None
    agent_id: str | None = None


class DependencyRequest(BaseModel):
    depends_on_task_id: str = Field(min_length=1)


class CommentRequest(BaseModel):
    body: str = Field(min_length=1)


class SpecifyRequest(BaseModel):
    body: str | None = None


class BlockRequest(BaseModel):
    reason: str = Field(min_length=1)
    kind: str = "input"
    summary: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_kind(self):
        if self.kind not in BLOCKED_KINDS:
            raise ValueError(f"kind must be one of {sorted(BLOCKED_KINDS)}")
        return self


class UnblockRequest(BaseModel):
    comment: str | None = None


class CancelRequest(BaseModel):
    reason: str | None = None


class HeartbeatRequest(BaseModel):
    note: str | None = None


class ArtifactDeclaration(BaseModel):
    path: str = Field(min_length=1)
    name: str | None = None
    mime_type: str | None = None


class CompleteRequest(BaseModel):
    summary: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactDeclaration] = Field(default_factory=list, max_length=100)


class WorkerCreate(TaskCreate):
    working_dir_override: None = None
    workspace_mode: None = None


def _worker_headers(
    session_id: str = Header(..., alias="X-Owlery-Session-ID"),
    task_id: str = Header(..., alias="X-Owlery-Task-ID"),
    run_id: str = Header(..., alias="X-Owlery-Task-Run-ID"),
) -> tuple[str, str, str]:
    return task_id, run_id, session_id


# ------------------------------------------------------------------ boards


@router.get("/api/task-boards")
async def list_boards(
    include_archived: bool = Query(False), _: str = Depends(verify_token)
):
    rows = await _run(task_repository.list_boards(include_archived=include_archived))
    return [_dict(row) for row in rows]


@router.post("/api/task-boards", status_code=status.HTTP_201_CREATED)
async def create_board(req: BoardCreate, _: str = Depends(verify_token)):
    row = await _run(task_repository.create_board(**req.model_dump()))
    await _get_manager().publish_board_update(row.id)
    return _dict(row)


@router.get("/api/task-boards/{board_id}")
async def get_board(board_id: str, response: Response, _: str = Depends(verify_token)):
    row = await _run(task_repository.get_board(board_id))
    _set_etag(response, row.updated_at)
    return _dict(row)


@router.patch("/api/task-boards/{board_id}")
async def patch_board(
    board_id: str,
    req: BoardPatch,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
    _: str = Depends(verify_token),
):
    current = await _run(task_repository.get_board(board_id))
    await _check_precondition(current, if_match)
    updates = req.model_dump(exclude_unset=True)
    body_version = updates.pop("updated_at", None)
    expected = _clean_etag(if_match) or body_version
    if if_match is None and body_version is not None:
        await _check_precondition(current, body_version)
    row = await _run(
        task_repository.update_board(
            board_id,
            expected_updated_at=expected,
            **updates,
        )
    )
    await _get_manager().publish_board_update(board_id)
    _set_etag(response, row.updated_at)
    return _dict(row)


@router.post("/api/task-boards/{board_id}/archive")
async def archive_board(board_id: str, _: str = Depends(verify_token)):
    row = await _run(task_repository.archive_board(board_id, archived=True))
    await _get_manager().publish_board_update(board_id)
    return _dict(row)


@router.post("/api/task-boards/{board_id}/unarchive")
async def unarchive_board(board_id: str, _: str = Depends(verify_token)):
    row = await _run(task_repository.archive_board(board_id, archived=False))
    await _get_manager().publish_board_update(board_id)
    return _dict(row)


@router.get("/api/task-boards/{board_id}/dispatcher")
async def dispatcher_status(board_id: str, _: str = Depends(verify_token)):
    await _run(task_repository.get_board(board_id))
    return await _run(_get_manager().dispatcher_status(board_id))


async def _set_dispatch(board_id: str, enabled: bool) -> dict[str, Any]:
    board = await _run(task_repository.set_dispatch_enabled(board_id, enabled))
    await _get_manager().publish_board_update(board_id)
    if enabled:
        await _get_manager().wake_dispatcher()
    state = await _run(_get_manager().dispatcher_status(board_id))
    return {"board": board.to_dict(), **state}


@router.post("/api/task-boards/{board_id}/dispatcher/pause")
async def pause_dispatcher(board_id: str, _: str = Depends(verify_token)):
    return await _set_dispatch(board_id, False)


@router.post("/api/task-boards/{board_id}/dispatcher/resume")
async def resume_dispatcher(board_id: str, _: str = Depends(verify_token)):
    return await _set_dispatch(board_id, True)


@router.get("/api/task-boards/{board_id}/events")
async def list_board_events(
    board_id: str,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=1000),
    _: str = Depends(verify_token),
):
    rows = await _run(
        task_repository.list_board_events(board_id, after_seq=after_seq, limit=limit)
    )
    return [_dict(row) for row in rows]


@router.get("/api/task-boards/{board_id}/tree")
async def get_task_tree(
    board_id: str,
    include_archived: bool = Query(False),
    _: str = Depends(verify_token),
):
    return await _run(
        task_repository.get_tree(board_id, include_archived=include_archived)
    )


# ------------------------------------------------------------------- tasks


@router.get("/api/tasks")
async def list_all_tasks(
    board_id: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    assignee: str | None = None,
    parent_id: str | None = None,
    include_archived: bool = False,
    limit: int = Query(200, ge=1, le=1000),
    _: str = Depends(verify_token),
):
    assignee_id = await _resolve_agent(assignee, None) if assignee else None
    return await _run(
        task_repository.list_tasks_enriched(
            board_id=board_id,
            status=status_filter,
            assignee_agent_id=assignee_id,
            parent_task_id=parent_id,
            include_archived=include_archived,
            limit=limit,
        )
    )


@router.get("/api/task-boards/{board_id}/tasks")
async def list_board_tasks(
    board_id: str,
    status_filter: str | None = Query(default=None, alias="status"),
    assignee: str | None = None,
    parent_id: str | None = None,
    include_archived: bool = False,
    limit: int = Query(200, ge=1, le=1000),
    _: str = Depends(verify_token),
):
    return await list_all_tasks(
        board_id, status_filter, assignee, parent_id, include_archived, limit, _
    )


@router.post("/api/task-boards/{board_id}/tasks")
async def create_task(
    board_id: str,
    req: TaskCreate,
    response: Response,
    caller_session_id: str | None = Header(default=None, alias="X-Owlery-Session-ID"),
    _: str = Depends(verify_token),
):
    actor_kind, actor_agent_id, origin_session_id = await _actor(caller_session_id)
    assignee_id = await _resolve_agent(req.assignee, req.assignee_agent_id)
    payload = req.model_dump(
        exclude={
            "assignee",
            "assignee_agent_id",
            "dependencies",
            "specified",
            "parent_task_id",
        }
    )
    result = await _run(
        task_repository.create_task_result(
            board_id=board_id,
            parent_task_id=req.parent_task_id,
            assignee_agent_id=assignee_id,
            status="todo" if req.specified else "triage",
            origin_session_id=origin_session_id,
            created_by_kind=actor_kind,
            created_by_agent_id=actor_agent_id,
            dependencies=req.dependencies,
            **payload,
        )
    )
    row, created = result
    await _publish(row.id)
    await _get_manager().wake_dispatcher()
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    response.headers["Location"] = f"/api/tasks/{row.id}"
    # An idempotency-key hit returns a pre-existing task that may already carry
    # run/delivery enrichment, so enrich here too rather than wipe it (§3).
    return await _enriched(row)


async def _task_detail(task_id: str) -> dict[str, Any]:
    # The detail exit carries the same derived fields as the list/tree/WS exits
    # so opening a task in the drawer never strips the card's delivery/run chip
    # when the browser merges the detail back into its board snapshot.
    task = await _run(task_repository.enrich_task(task_id))
    dependencies = await _run(task_repository.list_dependencies(task_id))
    comments = await _run(task_repository.list_comments(task_id))
    runs = await _run(task_repository.list_runs(task_id))
    artifacts = await _run(task_repository.list_artifacts(task_id))
    dependent_links = await _run(task_repository.list_dependents(task_id))
    dependency_tasks = [
        await _run(task_repository.get_task(link.depends_on_task_id))
        for link in dependencies
    ]
    dependent_tasks = [
        await _run(task_repository.get_task(link.task_id))
        for link in dependent_links
    ]
    children = await _run(
        task_repository.list_tasks(
            board_id=task["board_id"],
            parent_task_id=task["id"],
            include_archived=True,
            limit=1000,
        )
    )
    return {
        **task,
        "dependencies": [_dict(row) for row in dependency_tasks],
        "dependents": [_dict(row) for row in dependent_tasks],
        "children": [_dict(row) for row in children],
        "comments": [_dict(row) for row in comments],
        "runs": [_dict(row) for row in runs],
        "artifacts": [_public_artifact(row) for row in artifacts],
    }


@router.get("/api/tasks/{task_id}")
async def get_task(task_id: str, response: Response, _: str = Depends(verify_token)):
    detail = await _task_detail(task_id)
    _set_etag(response, detail["updated_at"])
    return detail


@router.patch("/api/tasks/{task_id}")
async def patch_task(
    task_id: str,
    req: TaskPatch,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
    _: str = Depends(verify_token),
):
    current = await _run(task_repository.get_task(task_id))
    await _check_precondition(current, if_match, enrich=True)
    updates = req.model_dump(exclude_unset=True)
    body_version = updates.pop("updated_at", None)
    expected = _clean_etag(if_match) or body_version
    if if_match is None and body_version is not None:
        await _check_precondition(current, body_version, enrich=True)
    row = await _run(
        task_repository.update_task(
            task_id,
            expected_updated_at=expected,
            **updates,
        )
    )
    await _publish(task_id)
    enriched = await _enriched(row)
    _set_etag(response, enriched["updated_at"])
    return enriched


@router.post("/api/tasks/{task_id}/assign")
async def assign_task(
    task_id: str,
    req: AssignRequest,
    caller_session_id: str | None = Header(default=None, alias="X-Owlery-Session-ID"),
    _: str = Depends(verify_token),
):
    actor_kind, actor_agent_id, _origin = await _actor(caller_session_id)
    agent_id = await _resolve_agent(req.agent_name, req.agent_id)
    row = await _run(
        task_repository.assign_task(
            task_id,
            agent_id,
            actor_kind=actor_kind,
            actor_agent_id=actor_agent_id,
        )
    )
    await _publish(task_id)
    await _get_manager().wake_dispatcher()
    return await _enriched(row)


@router.post("/api/tasks/{task_id}/dependencies", status_code=201)
async def add_dependency(
    task_id: str,
    req: DependencyRequest,
    caller_session_id: str | None = Header(default=None, alias="X-Owlery-Session-ID"),
    _: str = Depends(verify_token),
):
    actor_kind, actor_agent_id, _origin = await _actor(caller_session_id)
    row = await _run(
        task_repository.add_dependency(
            task_id,
            req.depends_on_task_id,
            created_by_kind=actor_kind,
            created_by_agent_id=actor_agent_id,
        )
    )
    await _publish(task_id)
    return await _enriched(row)


@router.delete("/api/tasks/{task_id}/dependencies/{dependency_id}")
async def remove_dependency(
    task_id: str,
    dependency_id: str,
    caller_session_id: str | None = Header(default=None, alias="X-Owlery-Session-ID"),
    _: str = Depends(verify_token),
):
    actor_kind, actor_agent_id, _origin = await _actor(caller_session_id)
    row = await _run(
        task_repository.remove_dependency(
            task_id,
            dependency_id,
            actor_kind=actor_kind,
            actor_agent_id=actor_agent_id,
        )
    )
    await _publish(task_id)
    await _get_manager().wake_dispatcher()
    return await _enriched(row)


@router.post("/api/tasks/{task_id}/comments", status_code=201)
async def add_comment(
    task_id: str,
    req: CommentRequest,
    caller_session_id: str | None = Header(default=None, alias="X-Owlery-Session-ID"),
    _: str = Depends(verify_token),
):
    actor_kind, actor_agent_id, _origin = await _actor(caller_session_id)
    row = await _run(
        task_repository.add_comment(
            task_id,
            body=req.body,
            author_kind=actor_kind,
            author_agent_id=actor_agent_id,
        )
    )
    await _publish(task_id)
    return row.to_dict()


@router.post("/api/tasks/{task_id}/triage")
async def triage_task(
    task_id: str,
    caller_session_id: str | None = Header(default=None, alias="X-Owlery-Session-ID"),
    _: str = Depends(verify_token),
):
    actor_kind, actor_agent_id, _origin = await _actor(caller_session_id)
    row = await _run(
        task_repository.triage_task(
            task_id,
            actor_kind=actor_kind,
            actor_agent_id=actor_agent_id,
        )
    )
    await _publish(task_id)
    return await _enriched(row)


@router.post("/api/tasks/{task_id}/specify")
async def specify_task(
    task_id: str,
    req: SpecifyRequest,
    caller_session_id: str | None = Header(default=None, alias="X-Owlery-Session-ID"),
    _: str = Depends(verify_token),
):
    actor_kind, actor_agent_id, _origin = await _actor(caller_session_id)
    row = await _run(
        task_repository.specify_task(
            task_id,
            body=req.body,
            actor_kind=actor_kind,
            actor_agent_id=actor_agent_id,
        )
    )
    await _publish(task_id)
    await _get_manager().wake_dispatcher()
    return await _enriched(row)


@router.post("/api/tasks/{task_id}/ready")
async def ready_task(
    task_id: str,
    caller_session_id: str | None = Header(default=None, alias="X-Owlery-Session-ID"),
    _: str = Depends(verify_token),
):
    actor_kind, actor_agent_id, _origin = await _actor(caller_session_id)
    row = await _run(
        task_repository.ready_task(
            task_id,
            actor_kind=actor_kind,
            actor_agent_id=actor_agent_id,
        )
    )
    await _publish(task_id)
    await _get_manager().wake_dispatcher()
    return await _enriched(row)


@router.post("/api/tasks/{task_id}/block")
async def block_task(
    task_id: str, req: BlockRequest, _: str = Depends(verify_token)
):
    row = await _run(
        _get_manager().block_active_task(
            task_id,
            reason=req.reason,
            kind=req.kind,
            summary=req.summary,
            metadata=req.metadata,
        )
    )
    return await _enriched(row)


@router.post("/api/tasks/{task_id}/unblock")
async def unblock_task(
    task_id: str,
    req: UnblockRequest,
    caller_session_id: str | None = Header(default=None, alias="X-Owlery-Session-ID"),
    _: str = Depends(verify_token),
):
    actor_kind, actor_agent_id, _origin = await _actor(caller_session_id)
    row = await _run(
        task_repository.unblock_task(
            task_id,
            comment=req.comment,
            actor_kind=actor_kind,
            actor_agent_id=actor_agent_id,
        )
    )
    await _publish(task_id)
    await _get_manager().wake_dispatcher()
    return await _enriched(row)


@router.post("/api/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    req: CancelRequest,
    caller_session_id: str | None = Header(default=None, alias="X-Owlery-Session-ID"),
    _: str = Depends(verify_token),
):
    actor_kind, actor_agent_id, _origin = await _actor(caller_session_id)
    current = await _run(task_repository.get_task(task_id))
    if current.status == "running":
        cancelled = await _run(
            _get_manager().cancel_task(task_id, req.reason or "cancelled")
        )
        return await _enriched(cancelled)
    row = await _run(
            task_repository.cancel_task(
                task_id,
                reason=req.reason,
                actor_kind=actor_kind,
                actor_agent_id=actor_agent_id,
            )
        )
    await _publish(task_id)
    return await _enriched(row)


@router.post("/api/tasks/{task_id}/archive")
async def archive_task(
    task_id: str,
    caller_session_id: str | None = Header(default=None, alias="X-Owlery-Session-ID"),
    _: str = Depends(verify_token),
):
    actor_kind, actor_agent_id, _origin = await _actor(caller_session_id)
    row = await _run(
        task_repository.archive_task(
            task_id,
            archived=True,
            actor_kind=actor_kind,
            actor_agent_id=actor_agent_id,
        )
    )
    await _publish(task_id)
    return await _enriched(row)


@router.post("/api/tasks/{task_id}/unarchive")
async def unarchive_task(
    task_id: str,
    caller_session_id: str | None = Header(default=None, alias="X-Owlery-Session-ID"),
    _: str = Depends(verify_token),
):
    actor_kind, actor_agent_id, _origin = await _actor(caller_session_id)
    row = await _run(
        task_repository.archive_task(
            task_id,
            archived=False,
            actor_kind=actor_kind,
            actor_agent_id=actor_agent_id,
        )
    )
    await _publish(task_id)
    return await _enriched(row)


@router.get("/api/tasks/{task_id}/runs")
async def list_runs(task_id: str, _: str = Depends(verify_token)):
    return [
        _dict(row) for row in await _run(task_repository.list_runs(task_id))
    ]


@router.get("/api/tasks/{task_id}/events")
async def list_task_events(
    task_id: str,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=1000),
    _: str = Depends(verify_token),
):
    rows = await _run(
        task_repository.list_task_events(task_id, after_seq=after_seq, limit=limit)
    )
    return [_dict(row) for row in rows]


@router.get("/api/tasks/{task_id}/artifacts")
async def list_artifacts(task_id: str, _: str = Depends(verify_token)):
    rows = await _run(task_repository.list_artifacts(task_id))
    return [_public_artifact(row) for row in rows]


@router.get("/api/tasks/{task_id}/artifacts/{artifact_id}")
async def download_artifact(
    task_id: str, artifact_id: str, _: str = Depends(verify_token)
):
    artifact = await _run(task_repository.get_artifact(task_id, artifact_id))
    if artifact.deleted_at is not None:
        raise HTTPException(404, "artifact not found")
    path = Path(artifact.stored_path)
    if not path.is_file():
        raise HTTPException(410, "artifact file is no longer available")
    return FileResponse(path, filename=artifact.name, media_type=artifact.mime_type)


@router.delete("/api/tasks/{task_id}/artifacts/{artifact_id}")
async def delete_artifact(
    task_id: str, artifact_id: str, _: str = Depends(verify_token)
):
    return _dict(
        await _run(_get_manager().delete_artifact(task_id, artifact_id))
    )


# ---------------------------------------------------------- worker callbacks


@router.get("/api/task-worker/current")
async def worker_show(
    identity: tuple[str, str, str] = Depends(_worker_headers),
    _: str = Depends(verify_token),
):
    return await _run(_get_manager().worker_snapshot(*identity))


@router.post("/api/task-worker/current/comments", status_code=201)
async def worker_comment(
    req: CommentRequest,
    identity: tuple[str, str, str] = Depends(_worker_headers),
    _: str = Depends(verify_token),
):
    task_id, run_id, session_id = identity
    await _run(_get_manager().worker_snapshot(task_id, run_id, session_id))
    task = await _run(task_repository.get_task(task_id))
    row = await _run(
        task_repository.add_comment(
            task_id,
            body=req.body,
            run_id=run_id,
            author_kind="agent",
            author_agent_id=task.assignee_agent_id,
        )
    )
    await _publish(task_id)
    return row.to_dict()


@router.post("/api/task-worker/current/heartbeat")
async def worker_heartbeat(
    req: HeartbeatRequest,
    identity: tuple[str, str, str] = Depends(_worker_headers),
    _: str = Depends(verify_token),
):
    task_id, run_id, session_id = identity
    return _dict(
        await _run(
            _get_manager().heartbeat_worker(
                task_id, run_id, session_id, note=req.note
            )
        )
    )


@router.post("/api/task-worker/current/complete")
async def worker_complete(
    req: CompleteRequest,
    identity: tuple[str, str, str] = Depends(_worker_headers),
    _: str = Depends(verify_token),
):
    task_id, run_id, session_id = identity
    return _dict(
        await _run(
            _get_manager().complete_worker(
                task_id,
                run_id,
                session_id,
                summary=req.summary,
                metadata=req.metadata,
                artifacts=[item.model_dump() for item in req.artifacts],
            )
        )
    )


@router.post("/api/task-worker/current/block")
async def worker_block(
    req: BlockRequest,
    identity: tuple[str, str, str] = Depends(_worker_headers),
    _: str = Depends(verify_token),
):
    task_id, run_id, session_id = identity
    return _dict(
        await _run(
            _get_manager().block_worker(
                task_id,
                run_id,
                session_id,
                reason=req.reason,
                kind=req.kind,
                summary=req.summary,
                metadata=req.metadata,
            )
        )
    )


@router.post("/api/task-worker/current/tasks", status_code=201)
async def worker_create_task(
    req: WorkerCreate,
    identity: tuple[str, str, str] = Depends(_worker_headers),
    _: str = Depends(verify_token),
):
    task_id, run_id, session_id = identity
    assignee_id = await _resolve_agent(req.assignee, req.assignee_agent_id)
    payload = req.model_dump(
        exclude={
            "assignee",
            "assignee_agent_id",
            "specified",
            "parent_task_id",
            "workspace_mode",
            "working_dir_override",
        }
    )
    row = await _run(
        _get_manager().create_worker_task(
            task_id,
            run_id,
            session_id,
            parent_task_id=req.parent_task_id,
            assignee_agent_id=assignee_id,
            status="todo" if req.specified else "triage",
            **payload,
        )
    )
    return _dict(row)


@router.post("/api/task-worker/current/dependencies", status_code=201)
async def worker_link_dependency(
    req: DependencyRequest,
    subject_task_id: str | None = Query(default=None),
    identity: tuple[str, str, str] = Depends(_worker_headers),
    _: str = Depends(verify_token),
):
    task_id, run_id, session_id = identity
    row = await _run(
        _get_manager().link_worker_dependency(
            task_id,
            run_id,
            session_id,
            subject_task_id=subject_task_id or task_id,
            depends_on_task_id=req.depends_on_task_id,
        )
    )
    return _dict(row)


# --------------------------------------------------------------- git delivery


class DeliveryAcceptRequest(BaseModel):
    base_ref: str | None = None


class DeliveryPushRequest(BaseModel):
    confirmations: dict[str, bool] = Field(default_factory=dict)


class DeliveryPrRequest(BaseModel):
    connector_installation_id: str | None = None
    draft: bool | None = None


class DeliveryMergeRequest(BaseModel):
    merge_strategy: Literal["fast_forward_only", "no_conflict_merge"] | None = None


class DeliveryTeardownRequest(BaseModel):
    retention: Literal["keep", "remove_worktree_keep_branch", "remove_all"] | None = None
    confirmations: dict[str, bool] = Field(default_factory=dict)


class WorkerDeliveryRequest(BaseModel):
    note: str | None = None


async def _delivery_actor(session_id: str | None) -> tuple[str, str | None]:
    kind, agent_id, _ = await _actor(session_id)
    return kind, agent_id


async def _delivery_for(task_id: str, run_id: str):
    delivery = await task_repository.get_delivery_by_run(run_id)
    if delivery is None or delivery.task_id != task_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"code": "not_found", "message": "no delivery for this run"},
        )
    return delivery


@router.get("/api/tasks/{task_id}/runs/{run_id}/delivery")
async def get_delivery(task_id: str, run_id: str, _: str = Depends(verify_token)):
    delivery = await _delivery_for(task_id, run_id)
    ops = await task_repository.list_delivery_ops(delivery.id)
    return {"delivery": delivery.to_dict(), "ops": [o.to_dict() for o in ops]}


@router.get("/api/tasks/{task_id}/runs/{run_id}/delivery/ops")
async def list_delivery_ops(task_id: str, run_id: str, _: str = Depends(verify_token)):
    delivery = await _delivery_for(task_id, run_id)
    return [o.to_dict() for o in await task_repository.list_delivery_ops(delivery.id)]


@router.post("/api/tasks/{task_id}/runs/{run_id}/delivery/accept")
async def delivery_accept(
    task_id: str,
    run_id: str,
    req: DeliveryAcceptRequest,
    caller_session_id: str | None = Header(default=None, alias="X-Owlery-Session-ID"),
    _: str = Depends(verify_token),
):
    actor_kind, actor_agent_id = await _delivery_actor(caller_session_id)
    return _dict(
        await _run(
            _get_manager().deliver_accept(
                task_id, run_id, base_ref=req.base_ref,
                actor_kind=actor_kind, actor_agent_id=actor_agent_id,
            )
        )
    )


async def _delivery_op(task_id, run_id, session_id, **kwargs):
    actor_kind, actor_agent_id = await _delivery_actor(session_id)
    return _dict(
        await _run(
            _get_manager().deliver_run_op(
                task_id, run_id, actor_kind=actor_kind,
                actor_agent_id=actor_agent_id, **kwargs,
            )
        )
    )


@router.post("/api/tasks/{task_id}/runs/{run_id}/delivery/commit")
async def delivery_commit(
    task_id: str, run_id: str,
    caller_session_id: str | None = Header(default=None, alias="X-Owlery-Session-ID"),
    _: str = Depends(verify_token),
):
    return await _delivery_op(task_id, run_id, caller_session_id, kind="commit")


@router.post("/api/tasks/{task_id}/runs/{run_id}/delivery/push")
async def delivery_push(
    task_id: str, run_id: str, req: DeliveryPushRequest,
    caller_session_id: str | None = Header(default=None, alias="X-Owlery-Session-ID"),
    _: str = Depends(verify_token),
):
    return await _delivery_op(
        task_id, run_id, caller_session_id, kind="push", confirmations=req.confirmations
    )


@router.post("/api/tasks/{task_id}/runs/{run_id}/delivery/pull-request")
async def delivery_pull_request(
    task_id: str, run_id: str, req: DeliveryPrRequest,
    caller_session_id: str | None = Header(default=None, alias="X-Owlery-Session-ID"),
    _: str = Depends(verify_token),
):
    return await _delivery_op(
        task_id, run_id, caller_session_id, kind="pull_request",
        connector_installation_id=req.connector_installation_id, draft=req.draft,
    )


@router.post("/api/tasks/{task_id}/runs/{run_id}/delivery/merge")
async def delivery_merge(
    task_id: str, run_id: str, req: DeliveryMergeRequest,
    caller_session_id: str | None = Header(default=None, alias="X-Owlery-Session-ID"),
    _: str = Depends(verify_token),
):
    return await _delivery_op(
        task_id, run_id, caller_session_id, kind="merge", merge_strategy=req.merge_strategy
    )


@router.post("/api/tasks/{task_id}/runs/{run_id}/delivery/teardown")
async def delivery_teardown(
    task_id: str, run_id: str, req: DeliveryTeardownRequest,
    caller_session_id: str | None = Header(default=None, alias="X-Owlery-Session-ID"),
    _: str = Depends(verify_token),
):
    actor_kind, actor_agent_id = await _delivery_actor(caller_session_id)
    return _dict(
        await _run(
            _get_manager().deliver_teardown(
                task_id, run_id, retention=req.retention,
                confirmations=req.confirmations,
                actor_kind=actor_kind, actor_agent_id=actor_agent_id,
            )
        )
    )


@router.post("/api/task-worker/current/delivery/request")
async def worker_request_delivery(
    req: WorkerDeliveryRequest,
    identity: tuple[str, str, str] = Depends(_worker_headers),
    _: str = Depends(verify_token),
):
    task_id, run_id, session_id = identity
    return await _run(
        _get_manager().request_delivery(task_id, run_id, session_id, note=req.note)
    )
