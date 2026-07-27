"""MCP stdio server for Owlery's durable Task Board.

The server is a loopback HTTP client.  Normal agent sessions receive the
orchestrator tools; dispatcher-created worker sessions additionally receive
trusted ``OWLERY_TASK_ID`` and ``OWLERY_TASK_RUN_ID`` environment variables.
Those identities are never accepted from model arguments.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s tasks-mcp %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP("owlery-tasks")


def _required_env(name: str) -> str | None:
    value = os.environ.get(name)
    if not value:
        logger.error("Required env var %s not set", name)
    return value


def _context() -> tuple[str, dict[str, str]] | None:
    api = _required_env("OWLERY_API_BASE")
    token = _required_env("OWLERY_AUTH_TOKEN")
    session_id = _required_env("OWLERY_SESSION_ID")
    if not (api and token and session_id):
        return None
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Owlery-Session-ID": session_id,
    }
    task_id = os.environ.get("OWLERY_TASK_ID")
    run_id = os.environ.get("OWLERY_TASK_RUN_ID")
    if bool(task_id) != bool(run_id):
        logger.error("OWLERY_TASK_ID and OWLERY_TASK_RUN_ID must be set together")
        return None
    if task_id and run_id:
        headers["X-Owlery-Task-ID"] = task_id
        headers["X-Owlery-Task-Run-ID"] = run_id
    return api.rstrip("/"), headers


def _worker_identity() -> tuple[str, str] | None:
    task_id = os.environ.get("OWLERY_TASK_ID")
    run_id = os.environ.get("OWLERY_TASK_RUN_ID")
    if task_id and run_id:
        return task_id, run_id
    return None


def _call(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    ctx = _context()
    if ctx is None:
        return 0, "tasks server is misconfigured (required env vars missing)"
    api, headers = ctx
    try:
        response = httpx.request(
            method,
            f"{api}{path}",
            json=body,
            params=params,
            headers=headers,
            timeout=15.0,
            trust_env=False,
        )
    except httpx.HTTPError as exc:
        return 0, f"failed to reach Owlery: {exc}"
    try:
        payload: Any = response.json()
    except (ValueError, json.JSONDecodeError):
        payload = response.text[:1000]
    return response.status_code, payload


def _render(status: int, payload: Any, *, action: str) -> str:
    if status == 0:
        return f"Error: {payload}"
    if status >= 400:
        if isinstance(payload, dict):
            detail = payload.get("detail", payload)
        else:
            detail = payload
        return f"Error: cannot {action} ({status}): {detail}"
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _worker_only(action: str) -> str | None:
    if _worker_identity() is None:
        return f"Error: `{action}` is available only inside a Task Board worker run."
    return None


def _orchestrator_only(action: str) -> str | None:
    if _worker_identity() is not None:
        return (
            f"Error: `{action}` is not available inside a Task Board worker run; "
            "workers may mutate only their current run and same-board child graph."
        )
    return None


@mcp.tool(name="list")
def list_tasks(
    board_id: str | None = None,
    status: str | None = None,
    assignee: str | None = None,
    parent_id: str | None = None,
    limit: int = 50,
) -> str:
    """List durable tasks. Filters are optional; limit is capped server-side."""
    error = _orchestrator_only("list")
    if error:
        return error
    params = {
        key: value
        for key, value in {
            "board_id": board_id,
            "status": status,
            "assignee": assignee,
            "parent_id": parent_id,
            "limit": limit,
        }.items()
        if value is not None
    }
    code, data = _call("GET", "/api/tasks", params=params)
    return _render(code, data, action="list tasks")


@mcp.tool(name="show")
def show_task(task_id: str | None = None) -> str:
    """Show a task with dependencies, comments, runs, and artifacts.

    A worker may omit ``task_id`` to inspect its own trusted task. Normal
    sessions must provide one.
    """
    worker = _worker_identity()
    if worker is not None:
        if task_id is not None and task_id.strip() != worker[0]:
            return "Error: a worker may show only its current task."
        path = "/api/task-worker/current"
    elif task_id and task_id.strip():
        path = f"/api/tasks/{task_id.strip()}"
    else:
        return "Error: `task_id` is required outside a Task Board worker run."
    code, data = _call("GET", path)
    return _render(code, data, action="show task")


@mcp.tool(name="create")
def create_task(
    title: str,
    assignee: str | None = None,
    body: str | None = None,
    board_id: str | None = None,
    parent_id: str | None = None,
    dependencies: list[str] | None = None,
    priority: int = 0,
    idempotency_key: str | None = None,
    scheduled_at: str | None = None,
) -> str:
    """Create a durable task.

    Worker-created tasks inherit the current board and workspace policy; a
    worker-supplied ``board_id`` is ignored by the server. Normal sessions must
    name a board.
    """
    if not (title or "").strip():
        return "Error: `title` must be non-empty."
    worker = _worker_identity()
    if worker is None and not (board_id or "").strip():
        return "Error: `board_id` is required outside a Task Board worker run."
    body_data: dict[str, Any] = {
        "title": title.strip(),
        "body": body or "",
        "assignee": assignee,
        "parent_id": parent_id,
        "dependencies": dependencies or [],
        "priority": priority,
        "idempotency_key": idempotency_key,
        "scheduled_at": scheduled_at,
    }
    if worker is None:
        path = f"/api/task-boards/{board_id}/tasks"
    else:
        path = "/api/task-worker/current/tasks"
    code, data = _call("POST", path, body=body_data)
    return _render(code, data, action="create task")


@mcp.tool(name="comment")
def comment_task(body: str, task_id: str | None = None) -> str:
    """Add a durable comment to a task or, in worker mode, the current task."""
    if not (body or "").strip():
        return "Error: `body` must be non-empty."
    worker = _worker_identity()
    if worker is not None:
        if task_id is not None and task_id.strip() != worker[0]:
            return "Error: a worker may comment only on its current task."
        path = "/api/task-worker/current/comments"
    elif task_id and task_id.strip():
        path = f"/api/tasks/{task_id.strip()}/comments"
    else:
        return "Error: `task_id` is required outside a Task Board worker run."
    code, data = _call("POST", path, body={"body": body.strip()})
    return _render(code, data, action="comment on task")


@mcp.tool(name="heartbeat")
def heartbeat_task(note: str | None = None) -> str:
    """Record worker progress and extend the current run lease."""
    error = _worker_only("heartbeat")
    if error:
        return error
    code, data = _call(
        "POST", "/api/task-worker/current/heartbeat", body={"note": note}
    )
    return _render(code, data, action="heartbeat task")


@mcp.tool(name="complete")
def complete_task(
    summary: str,
    metadata: dict[str, Any] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> str:
    """Complete the owning run with a structured handoff and artifacts."""
    error = _worker_only("complete")
    if error:
        return error
    if not (summary or "").strip():
        return "Error: `summary` must be non-empty."
    code, data = _call(
        "POST",
        "/api/task-worker/current/complete",
        body={
            "summary": summary.strip(),
            "metadata": metadata or {},
            "artifacts": artifacts or [],
        },
    )
    return _render(code, data, action="complete task")


@mcp.tool(name="block")
def block_task(
    reason: str,
    kind: str = "input",
    summary: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Stop the owning run and block the task for explicit human action."""
    error = _worker_only("block")
    if error:
        return error
    if not (reason or "").strip():
        return "Error: `reason` must be non-empty."
    code, data = _call(
        "POST",
        "/api/task-worker/current/block",
        body={
            "reason": reason.strip(),
            "kind": kind,
            "summary": summary,
            "metadata": metadata or {},
        },
    )
    return _render(code, data, action="block task")


@mcp.tool(name="triage")
def triage_task(task_id: str) -> str:
    """Return a specified, non-running task to triage."""
    error = _orchestrator_only("triage")
    if error:
        return error
    code, data = _call("POST", f"/api/tasks/{task_id}/triage", body={})
    return _render(code, data, action="triage task")


@mcp.tool(name="specify")
def specify_task(task_id: str, body: str | None = None) -> str:
    """Mark a triaged task specified and recompute its eligibility."""
    error = _orchestrator_only("specify")
    if error:
        return error
    code, data = _call(
        "POST", f"/api/tasks/{task_id}/specify", body={"body": body}
    )
    return _render(code, data, action="specify task")


@mcp.tool(name="assign")
def assign_task(task_id: str, agent_name: str | None = None) -> str:
    """Assign by case-insensitive Agent name, or clear with null."""
    error = _orchestrator_only("assign")
    if error:
        return error
    code, data = _call(
        "POST", f"/api/tasks/{task_id}/assign", body={"agent_name": agent_name}
    )
    return _render(code, data, action="assign task")


@mcp.tool(name="link")
def link_tasks(depends_on_task_id: str, task_id: str | None = None) -> str:
    """Make a task depend on another task; cycles and cross-board links fail."""
    worker = _worker_identity()
    params = None
    if worker is not None:
        subject = (task_id or worker[0]).strip()
        path = "/api/task-worker/current/dependencies"
        params = {"subject_task_id": subject}
    elif task_id and task_id.strip():
        path = f"/api/tasks/{task_id.strip()}/dependencies"
    else:
        return "Error: `task_id` is required outside a Task Board worker run."
    code, data = _call(
        "POST",
        path,
        body={"depends_on_task_id": depends_on_task_id},
        params=params,
    )
    return _render(code, data, action="link tasks")


@mcp.tool(name="unlink")
def unlink_tasks(task_id: str, depends_on_task_id: str) -> str:
    """Remove a dependency link. Orchestrator-only."""
    error = _orchestrator_only("unlink")
    if error:
        return error
    code, data = _call(
        "DELETE", f"/api/tasks/{task_id}/dependencies/{depends_on_task_id}"
    )
    return _render(code, data, action="unlink tasks")


@mcp.tool(name="unblock")
def unblock_task(task_id: str, comment: str | None = None) -> str:
    """Explicitly unblock a task; eligibility is recomputed, never auto-run."""
    error = _orchestrator_only("unblock")
    if error:
        return error
    code, data = _call(
        "POST", f"/api/tasks/{task_id}/unblock", body={"comment": comment}
    )
    return _render(code, data, action="unblock task")


@mcp.tool(name="cancel")
def cancel_task(task_id: str, reason: str | None = None) -> str:
    """Cancel a non-running task or ask the manager to stop its owning run."""
    error = _orchestrator_only("cancel")
    if error:
        return error
    code, data = _call(
        "POST", f"/api/tasks/{task_id}/cancel", body={"reason": reason}
    )
    return _render(code, data, action="cancel task")


@mcp.tool(name="request_delivery")
def request_delivery(note: str | None = None) -> str:
    """Ask Owlery to deliver THIS worker run's Git branch. Records intent only —
    no push/PR/merge happens here; a human or orchestrator triggers those."""
    error = _worker_only("request_delivery")
    if error:
        return error
    code, data = _call(
        "POST", "/api/task-worker/current/delivery/request", body={"note": note}
    )
    return _render(code, data, action="request delivery")


@mcp.tool(name="delivery_status")
def delivery_status(task_id: str, run_id: str) -> str:
    """Show the Git delivery (and its op log) for a completed git_worktree run."""
    error = _orchestrator_only("delivery_status")
    if error:
        return error
    code, data = _call("GET", f"/api/tasks/{task_id}/runs/{run_id}/delivery")
    return _render(code, data, action="get delivery")


@mcp.tool(name="deliver")
def deliver(
    task_id: str,
    run_id: str,
    action: str,
    confirmations: dict | None = None,
    connector_installation_id: str | None = None,
    merge_strategy: str | None = None,
    draft: bool | None = None,
    base_ref: str | None = None,
) -> str:
    """Run one Git delivery action: accept | commit | push | pull_request | merge.

    Destructive actions require an explicit `confirmations` flag (e.g.
    {"allow_force_push": true}); external effects run only in the trusted server."""
    error = _orchestrator_only("deliver")
    if error:
        return error
    base = f"/api/tasks/{task_id}/runs/{run_id}/delivery"
    if action == "accept":
        code, data = _call(
            "POST",
            f"{base}/accept",
            body={
                "base_ref": base_ref,
                "confirmations": confirmations or {},
            },
        )
    elif action == "commit":
        code, data = _call(
            "POST",
            f"{base}/commit",
            body={"confirmations": confirmations or {}},
        )
    elif action == "push":
        code, data = _call("POST", f"{base}/push", body={"confirmations": confirmations or {}})
    elif action == "pull_request":
        code, data = _call(
            "POST", f"{base}/pull-request",
            body={
                "connector_installation_id": connector_installation_id,
                "draft": draft,
                "confirmations": confirmations or {},
            },
        )
    elif action == "merge":
        code, data = _call(
            "POST",
            f"{base}/merge",
            body={
                "merge_strategy": merge_strategy,
                "confirmations": confirmations or {},
            },
        )
    else:
        return (
            f"Error: unknown delivery action {action!r} "
            "(accept | commit | push | pull_request | merge)."
        )
    return _render(code, data, action=f"deliver {action}")


@mcp.tool(name="delivery_teardown")
def delivery_teardown(
    task_id: str,
    run_id: str,
    retention: str | None = None,
    confirmations: dict | None = None,
) -> str:
    """Tear down a delivery's worktree/branch per the retention policy."""
    error = _orchestrator_only("delivery_teardown")
    if error:
        return error
    code, data = _call(
        "POST", f"/api/tasks/{task_id}/runs/{run_id}/delivery/teardown",
        body={"retention": retention, "confirmations": confirmations or {}},
    )
    return _render(code, data, action="tear down delivery")


if __name__ == "__main__":
    mcp.run()
