"""Unit tests for the `tbexec` MCP stdio server (mcp__tbexec__run/done).

Mirrors tests/test_ask_mcp.py: invoke the FastMCP-wrapped tool function
directly with httpx mocked, so we don't need a live FastAPI to verify
request shape.
"""

from __future__ import annotations


def _call(tool: str, **kwargs):
    from server.mcp_servers import tbexec as srv

    fn = getattr(srv, tool)
    try:
        return fn(**kwargs)
    except TypeError:
        return fn.fn(**kwargs)  # type: ignore[attr-defined]


def test_tbexec_run_misconfigured(monkeypatch):
    monkeypatch.delenv("OWLERY_API_BASE", raising=False)
    monkeypatch.delenv("OWLERY_SESSION_ID", raising=False)
    monkeypatch.delenv("OWLERY_AUTH_TOKEN", raising=False)
    out = _call("tbexec_run", command="ls")
    assert "misconfigured" in out.lower()


def test_tbexec_run_rejects_empty_command(monkeypatch):
    monkeypatch.setenv("OWLERY_API_BASE", "http://x")
    monkeypatch.setenv("OWLERY_SESSION_ID", "s")
    monkeypatch.setenv("OWLERY_AUTH_TOKEN", "t")
    out = _call("tbexec_run", command="   ")
    assert "non-empty" in out.lower()


def test_tbexec_run_posts_exec_decision_and_clamps_timeout(monkeypatch):
    monkeypatch.setenv("OWLERY_API_BASE", "http://x")
    monkeypatch.setenv("OWLERY_SESSION_ID", "s")
    monkeypatch.setenv("OWLERY_AUTH_TOKEN", "t")
    captured: dict = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"message": "Recorded."}

    def fake_post(url, json=None, headers=None, timeout=None, trust_env=None):  # noqa: ARG001
        captured["url"] = url
        captured["json"] = json
        captured["trust_env"] = trust_env
        return Resp()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    out = _call("tbexec_run", command="ls -la", timeout=99999)
    assert captured["url"].endswith("/api/sessions/s/tbexec/decision")
    assert captured["json"] == {"kind": "exec", "command": "ls -la", "timeout": 300}
    assert captured["trust_env"] is False
    assert out == "Recorded."


def test_tbexec_done_posts_finish_decision(monkeypatch):
    monkeypatch.setenv("OWLERY_API_BASE", "http://x")
    monkeypatch.setenv("OWLERY_SESSION_ID", "s")
    monkeypatch.setenv("OWLERY_AUTH_TOKEN", "t")
    captured: dict = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"message": "Recorded. The task is finished."}

    def fake_post(url, json=None, headers=None, timeout=None, trust_env=None):  # noqa: ARG001
        captured["json"] = json
        return Resp()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    out = _call("tbexec_done", output="all good")
    assert captured["json"] == {"kind": "finish", "output": "all good"}
    assert "finished" in out.lower()


def test_tbexec_conflict_maps_to_409(monkeypatch):
    monkeypatch.setenv("OWLERY_API_BASE", "http://x")
    monkeypatch.setenv("OWLERY_SESSION_ID", "s")
    monkeypatch.setenv("OWLERY_AUTH_TOKEN", "t")

    class Resp:
        status_code = 409
        text = "already recorded"

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: Resp())  # noqa: ARG005

    out = _call("tbexec_run", command="ls")
    assert "already recorded" in out.lower() or "exactly one" in out.lower()


def test_tbexec_post_http_error(monkeypatch):
    monkeypatch.setenv("OWLERY_API_BASE", "http://x")
    monkeypatch.setenv("OWLERY_SESSION_ID", "s")
    monkeypatch.setenv("OWLERY_AUTH_TOKEN", "t")

    import httpx

    def boom(*a, **k):  # noqa: ARG001
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx, "post", boom)
    out = _call("tbexec_run", command="ls")
    assert "failed to reach Owlery" in out
