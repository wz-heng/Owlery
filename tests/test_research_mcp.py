"""Unit tests for the `research` MCP stdio server (`mcp__research__deep_research`).

Mirrors the inline MCP tests in tests/test_bg_tasks.py and
tests/test_ask_agent_mcp.py: invoke the FastMCP-wrapped tool function
directly with httpx mocked, so we don't need a live FastAPI to verify
request shape.
"""

from __future__ import annotations


def _call(tool: str, **kwargs):
    from server.mcp_servers import research as srv

    fn = getattr(srv, tool)
    try:
        return fn(**kwargs)
    except TypeError:
        return fn.fn(**kwargs)  # type: ignore[attr-defined]


def test_deep_research_misconfigured(monkeypatch):
    monkeypatch.delenv("OWLERY_API_BASE", raising=False)
    monkeypatch.delenv("OWLERY_SESSION_ID", raising=False)
    monkeypatch.delenv("OWLERY_AUTH_TOKEN", raising=False)
    out = _call("deep_research", question="what car should I buy?")
    assert "misconfigured" in out.lower()


def test_deep_research_rejects_empty_question(monkeypatch):
    monkeypatch.setenv("OWLERY_API_BASE", "http://x")
    monkeypatch.setenv("OWLERY_SESSION_ID", "s")
    monkeypatch.setenv("OWLERY_AUTH_TOKEN", "t")
    out = _call("deep_research", question="   ")
    assert "non-empty" in out.lower()


def test_deep_research_success(monkeypatch):
    """The callback POST goes straight to the loopback Owlery API,
    bypassing any system proxy (e.g. Clash) that might otherwise
    hijack the request and hand back a bogus response."""
    monkeypatch.setenv("OWLERY_API_BASE", "http://x")
    monkeypatch.setenv("OWLERY_SESSION_ID", "s")
    monkeypatch.setenv("OWLERY_AUTH_TOKEN", "t")
    captured: dict = {}

    class R:
        status_code = 201

        def json(self):
            return {"id": "r1"}

    def fake_post(url, json=None, headers=None, timeout=None, trust_env=None):  # noqa: ARG001
        captured["url"] = url
        captured["body"] = json
        captured["trust_env"] = trust_env
        return R()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    out = _call("deep_research", question="what car should I buy?")
    assert captured["url"].endswith("/api/sessions/s/research")
    assert captured["body"] == {"question": "what car should I buy?"}
    assert captured["trust_env"] is False
    assert "r1" in out


def test_deep_research_409_no_web_tools(monkeypatch):
    monkeypatch.setenv("OWLERY_API_BASE", "http://x")
    monkeypatch.setenv("OWLERY_SESSION_ID", "s")
    monkeypatch.setenv("OWLERY_AUTH_TOKEN", "t")

    class R:
        status_code = 409
        text = "no web tools on this backend"

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: R())
    out = _call("deep_research", question="q")
    assert "isn't available" in out.lower()


def test_deep_research_http_error(monkeypatch):
    monkeypatch.setenv("OWLERY_API_BASE", "http://x")
    monkeypatch.setenv("OWLERY_SESSION_ID", "s")
    monkeypatch.setenv("OWLERY_AUTH_TOKEN", "t")

    import httpx

    def boom(*a, **k):  # noqa: ARG001
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx, "post", boom)
    out = _call("deep_research", question="q")
    assert "failed to reach Owlery" in out
