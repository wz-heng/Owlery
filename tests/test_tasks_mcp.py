"""Focused request-shape tests for the Task Board MCP stdio shim."""

from __future__ import annotations


def _call(tool: str, **kwargs):
    from server.mcp_servers import tasks as srv

    fn = getattr(srv, tool)
    try:
        return fn(**kwargs)
    except TypeError:
        return fn.fn(**kwargs)  # type: ignore[attr-defined]


def _base_env(monkeypatch, *, worker: bool = False) -> None:
    monkeypatch.setenv("OWLERY_API_BASE", "http://owlery")
    monkeypatch.setenv("OWLERY_AUTH_TOKEN", "secret")
    monkeypatch.setenv("OWLERY_SESSION_ID", "session-1")
    if worker:
        monkeypatch.setenv("OWLERY_TASK_ID", "task-1")
        monkeypatch.setenv("OWLERY_TASK_RUN_ID", "run-1")
    else:
        monkeypatch.delenv("OWLERY_TASK_ID", raising=False)
        monkeypatch.delenv("OWLERY_TASK_RUN_ID", raising=False)


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.text = str(self._payload)

    def json(self):
        return self._payload


def test_list_tasks_uses_trusted_session_and_no_proxy(monkeypatch):
    _base_env(monkeypatch)
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return _Response(payload=[])

    import httpx

    monkeypatch.setattr(httpx, "request", fake_request)
    out = _call("list_tasks", board_id="b1", status="ready", limit=12)
    assert out == "[]"
    assert captured["method"] == "GET"
    assert captured["url"] == "http://owlery/api/tasks"
    assert captured["params"] == {
        "board_id": "b1",
        "status": "ready",
        "limit": 12,
    }
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["headers"]["X-Owlery-Session-ID"] == "session-1"
    assert captured["trust_env"] is False


def test_worker_show_derives_identity_from_env(monkeypatch):
    _base_env(monkeypatch, worker=True)
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return _Response(payload={"id": "task-1"})

    import httpx

    monkeypatch.setattr(httpx, "request", fake_request)
    out = _call("show_task")
    assert "task-1" in out
    assert captured["url"].endswith("/api/task-worker/current")
    assert captured["headers"]["X-Owlery-Task-ID"] == "task-1"
    assert captured["headers"]["X-Owlery-Task-Run-ID"] == "run-1"


def test_worker_complete_never_accepts_model_identity(monkeypatch):
    _base_env(monkeypatch, worker=True)
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return _Response(payload={"state": "completed"})

    import httpx

    monkeypatch.setattr(httpx, "request", fake_request)
    out = _call(
        "complete_task",
        summary="Shipped",
        metadata={"tests": "green"},
        artifacts=[{"path": "report.md", "name": "Report"}],
    )
    assert "completed" in out
    assert captured["url"].endswith("/api/task-worker/current/complete")
    assert captured["json"] == {
        "summary": "Shipped",
        "metadata": {"tests": "green"},
        "artifacts": [{"path": "report.md", "name": "Report"}],
    }
    assert "task_id" not in captured["json"]
    assert "run_id" not in captured["json"]


def test_worker_only_terminal_tools_fail_closed(monkeypatch):
    _base_env(monkeypatch)
    assert "only inside" in _call("heartbeat_task").lower()
    assert "only inside" in _call("complete_task", summary="done").lower()
    assert "only inside" in _call("block_task", reason="need input").lower()


def test_partial_worker_env_is_misconfigured(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("OWLERY_TASK_ID", "task-1")
    monkeypatch.delenv("OWLERY_TASK_RUN_ID", raising=False)
    out = _call("list_tasks")
    assert "misconfigured" in out.lower()


def test_orchestrator_create_requires_board(monkeypatch):
    _base_env(monkeypatch)
    out = _call("create_task", title="Implement it")
    assert "board_id" in out


def test_worker_create_uses_worker_endpoint_and_inherits_board(monkeypatch):
    _base_env(monkeypatch, worker=True)
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return _Response(status_code=201, payload={"id": "child-1"})

    import httpx

    monkeypatch.setattr(httpx, "request", fake_request)
    out = _call(
        "create_task",
        title="Child",
        assignee="Snape",
        dependencies=["dep-1"],
    )
    assert "child-1" in out
    assert captured["url"].endswith("/api/task-worker/current/tasks")
    assert "board_id" not in captured["json"]


def test_worker_cannot_use_orchestrator_only_tools(monkeypatch):
    _base_env(monkeypatch, worker=True)
    assert "not available" in _call("list_tasks").lower()
    assert "not available" in _call("assign_task", task_id="x").lower()
    assert "not available" in _call("cancel_task", task_id="x").lower()
    assert "only its current task" in _call("show_task", task_id="other").lower()


def test_worker_link_goes_through_scoped_callback(monkeypatch):
    _base_env(monkeypatch, worker=True)
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return _Response(status_code=201, payload={"ok": True})

    import httpx

    monkeypatch.setattr(httpx, "request", fake_request)
    _call(
        "link_tasks",
        task_id="same-board-child",
        depends_on_task_id="dependency",
    )
    assert captured["url"].endswith("/api/task-worker/current/dependencies")
    assert captured["params"] == {"subject_task_id": "same-board-child"}


def test_conflict_reason_is_visible(monkeypatch):
    _base_env(monkeypatch)

    import httpx

    monkeypatch.setattr(
        httpx,
        "request",
        lambda *args, **kwargs: _Response(
            status_code=409, payload={"detail": "dependency cycle rejected"}
        ),
    )
    out = _call(
        "link_tasks", task_id="a", depends_on_task_id="b"
    )
    assert "409" in out and "cycle" in out.lower()


def test_worker_delivery_request_is_scoped_to_trusted_run(monkeypatch):
    _base_env(monkeypatch, worker=True)
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return _Response(payload={"requested": True})

    import httpx

    monkeypatch.setattr(httpx, "request", fake_request)
    out = _call("request_delivery", note="Please open a PR")
    assert "requested" in out
    assert captured["url"].endswith(
        "/api/task-worker/current/delivery/request"
    )
    assert captured["json"] == {"note": "Please open a PR"}
    assert captured["headers"]["X-Owlery-Task-ID"] == "task-1"
    assert captured["headers"]["X-Owlery-Task-Run-ID"] == "run-1"


def test_orchestrator_delivery_forwards_typed_confirmations(monkeypatch):
    _base_env(monkeypatch)
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return _Response(payload={"status": "ready"})

    import httpx

    monkeypatch.setattr(httpx, "request", fake_request)
    confirmations = {"allow_foreign_remote": True}
    assert "ready" in _call(
        "deliver",
        task_id="task-1",
        run_id="run-1",
        action="accept",
        base_ref="main",
        confirmations=confirmations,
    )
    assert "ready" in _call(
        "deliver",
        task_id="task-1",
        run_id="run-1",
        action="pull_request",
        connector_installation_id="github-1",
        draft=True,
        confirmations=confirmations,
    )
    assert "ready" in _call(
        "deliver",
        task_id="task-1",
        run_id="run-1",
        action="merge",
        merge_strategy="fast_forward_only",
        confirmations=confirmations,
    )

    assert calls[0]["json"] == {
        "base_ref": "main",
        "confirmations": confirmations,
    }
    assert calls[1]["json"] == {
        "connector_installation_id": "github-1",
        "draft": True,
        "confirmations": confirmations,
    }
    assert calls[2]["json"] == {
        "merge_strategy": "fast_forward_only",
        "confirmations": confirmations,
    }


def test_delivery_tools_keep_worker_and_orchestrator_boundaries(monkeypatch):
    _base_env(monkeypatch)
    assert "only inside" in _call("request_delivery").lower()
    _base_env(monkeypatch, worker=True)
    assert "not available" in _call(
        "delivery_status", task_id="task-1", run_id="run-1"
    ).lower()
    assert "not available" in _call(
        "deliver", task_id="task-1", run_id="run-1", action="push"
    ).lower()
