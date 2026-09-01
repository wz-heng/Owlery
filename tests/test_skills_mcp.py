"""Unit tests for the `skills` MCP stdio server (`mcp__skills__propose` etc.).

Mirrors tests/test_research_mcp.py: invoke the FastMCP-wrapped tool function
directly with httpx mocked, so we don't need a live FastAPI to verify
request shape.
"""

from __future__ import annotations


def _call(tool: str, **kwargs):
    from server.mcp_servers import skills as srv

    fn = getattr(srv, tool)
    try:
        return fn(**kwargs)
    except TypeError:
        return fn.fn(**kwargs)  # type: ignore[attr-defined]


def _set_env(monkeypatch, *, task_id: str | None = None, run_id: str | None = None):
    monkeypatch.setenv("OWLERY_API_BASE", "http://x")
    monkeypatch.setenv("OWLERY_SESSION_ID", "s")
    monkeypatch.setenv("OWLERY_AUTH_TOKEN", "t")
    monkeypatch.delenv("OWLERY_TASK_ID", raising=False)
    monkeypatch.delenv("OWLERY_TASK_RUN_ID", raising=False)
    if task_id:
        monkeypatch.setenv("OWLERY_TASK_ID", task_id)
    if run_id:
        monkeypatch.setenv("OWLERY_TASK_RUN_ID", run_id)


def test_propose_misconfigured(monkeypatch):
    monkeypatch.delenv("OWLERY_API_BASE", raising=False)
    monkeypatch.delenv("OWLERY_SESSION_ID", raising=False)
    monkeypatch.delenv("OWLERY_AUTH_TOKEN", raising=False)
    out = _call(
        "propose", slug="s", title="t", description="d",
        body_markdown="body", rationale="r",
    )
    assert "misconfigured" in out.lower()


def test_propose_posts_to_session_scoped_route(monkeypatch):
    _set_env(monkeypatch)
    captured: dict = {}

    class R:
        status_code = 201

        def json(self):
            return {"id": "cand-1", "status": "pending"}

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None, trust_env=None):  # noqa: ARG001
        captured["method"] = method
        captured["url"] = url
        captured["body"] = json
        captured["trust_env"] = trust_env
        return R()

    import httpx

    monkeypatch.setattr(httpx, "request", fake_request)
    out = _call(
        "propose", slug="hermes-pr-flow", title="t", description="d",
        body_markdown="body", rationale="r",
    )
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/api/sessions/s/skills/candidates")
    assert captured["body"]["slug"] == "hermes-pr-flow"
    assert "task_id" not in captured["body"]
    assert captured["trust_env"] is False
    assert "cand-1" in out


def test_propose_includes_task_context_inside_a_worker_run(monkeypatch):
    _set_env(monkeypatch, task_id="task-1", run_id="run-1")
    captured: dict = {}

    class R:
        status_code = 201

        def json(self):
            return {"id": "cand-1"}

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None, trust_env=None):  # noqa: ARG001
        captured["body"] = json
        return R()

    import httpx

    monkeypatch.setattr(httpx, "request", fake_request)
    _call(
        "propose", slug="s", title="t", description="d",
        body_markdown="body", rationale="r",
    )
    assert captured["body"]["task_id"] == "task-1"
    assert captured["body"]["run_id"] == "run-1"


def test_propose_surfaces_a_validation_error(monkeypatch):
    _set_env(monkeypatch)

    class R:
        status_code = 422

        def json(self):
            return {"detail": {"code": "validation", "message": "slug must be kebab-case"}}

    import httpx

    monkeypatch.setattr(httpx, "request", lambda *a, **k: R())
    out = _call(
        "propose", slug="Not Kebab", title="t", description="d",
        body_markdown="body", rationale="r",
    )
    assert "Error" in out
    assert "422" in out


def test_list_pending_filters_by_status(monkeypatch):
    _set_env(monkeypatch)
    captured: dict = {}

    class R:
        status_code = 200

        def json(self):
            return []

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None, trust_env=None):  # noqa: ARG001
        captured["url"] = url
        captured["params"] = params
        return R()

    import httpx

    monkeypatch.setattr(httpx, "request", fake_request)
    _call("list_pending")
    assert captured["url"].endswith("/api/skills/candidates")
    assert captured["params"] == {"status": "pending"}


def test_diff_gets_candidate_by_id(monkeypatch):
    _set_env(monkeypatch)
    captured: dict = {}

    class R:
        status_code = 200

        def json(self):
            return {"candidate": {"id": "cand-1"}, "diff": "+hi\n"}

    def fake_request(method, url, json=None, params=None, headers=None, timeout=None, trust_env=None):  # noqa: ARG001
        captured["url"] = url
        return R()

    import httpx

    monkeypatch.setattr(httpx, "request", fake_request)
    out = _call("diff", candidate_id="cand-1")
    assert captured["url"].endswith("/api/skills/candidates/cand-1")
    assert "+hi" in out
