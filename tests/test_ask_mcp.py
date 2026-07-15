"""Unit tests for the `ask` MCP stdio server (`mcp__ask__user`).

Mirrors the inline MCP tests in tests/test_bg_tasks.py and
tests/test_ask_agent_mcp.py: invoke the FastMCP-wrapped tool function
directly with httpx mocked, so we don't need a live FastAPI to verify
request shape.
"""

from __future__ import annotations


def _call(tool: str, **kwargs):
    from server.mcp_servers import ask as srv

    fn = getattr(srv, tool)
    try:
        return fn(**kwargs)
    except TypeError:
        return fn.fn(**kwargs)  # type: ignore[attr-defined]


_Q = [{"question": "pick one", "options": [{"label": "A"}, {"label": "B"}]}]


def test_ask_user_misconfigured(monkeypatch):
    monkeypatch.delenv("OWLERY_API_BASE", raising=False)
    monkeypatch.delenv("OWLERY_SESSION_ID", raising=False)
    monkeypatch.delenv("OWLERY_AUTH_TOKEN", raising=False)
    out = _call("ask_user", questions=_Q)
    assert "misconfigured" in out.lower()


def test_ask_user_rejects_empty_questions(monkeypatch):
    monkeypatch.setenv("OWLERY_API_BASE", "http://x")
    monkeypatch.setenv("OWLERY_SESSION_ID", "s")
    monkeypatch.setenv("OWLERY_AUTH_TOKEN", "t")
    out = _call("ask_user", questions=[])
    assert "non-empty" in out.lower()


def test_ask_user_success_round_trip(monkeypatch):
    """Create-question POST and long-poll GET both go straight to the
    loopback Owlery API, bypassing any system proxy (e.g. Clash) that
    might otherwise hijack the request and hand back a bogus response."""
    monkeypatch.setenv("OWLERY_API_BASE", "http://x")
    monkeypatch.setenv("OWLERY_SESSION_ID", "s")
    monkeypatch.setenv("OWLERY_AUTH_TOKEN", "t")
    captured: dict = {}

    class CreateResp:
        status_code = 201

        def json(self):
            return {"question_id": "q1"}

    class AnswerResp:
        status_code = 200

        def json(self):
            return {"answer": "Q: pick one\nA: A"}

    def fake_post(url, json=None, headers=None, timeout=None, trust_env=None):  # noqa: ARG001
        captured["post_url"] = url
        captured["post_trust_env"] = trust_env
        return CreateResp()

    def fake_get(
        url, params=None, headers=None, timeout=None, trust_env=None
    ):  # noqa: ARG001
        captured["get_url"] = url
        captured["get_trust_env"] = trust_env
        return AnswerResp()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)

    out = _call("ask_user", questions=_Q)
    assert captured["post_url"].endswith("/api/sessions/s/questions")
    assert captured["get_url"].endswith("/api/sessions/s/questions/q1/answer")
    assert captured["post_trust_env"] is False
    assert captured["get_trust_env"] is False
    assert "pick one" in out


def test_ask_user_poll_404_when_session_gone(monkeypatch):
    monkeypatch.setenv("OWLERY_API_BASE", "http://x")
    monkeypatch.setenv("OWLERY_SESSION_ID", "s")
    monkeypatch.setenv("OWLERY_AUTH_TOKEN", "t")

    class CreateResp:
        status_code = 201

        def json(self):
            return {"question_id": "q1"}

    class GoneResp:
        status_code = 404
        text = ""

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: CreateResp())
    monkeypatch.setattr(httpx, "get", lambda *a, **k: GoneResp())
    out = _call("ask_user", questions=_Q)
    assert "disappeared" in out.lower()


def test_ask_user_create_http_error(monkeypatch):
    monkeypatch.setenv("OWLERY_API_BASE", "http://x")
    monkeypatch.setenv("OWLERY_SESSION_ID", "s")
    monkeypatch.setenv("OWLERY_AUTH_TOKEN", "t")

    import httpx

    def boom(*a, **k):  # noqa: ARG001
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx, "post", boom)
    out = _call("ask_user", questions=_Q)
    assert "failed to reach Owlery" in out
