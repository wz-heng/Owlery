"""Mail connector MCP server (mail-connector.md §4.2): real IMAP/SMTP fake
servers for the read/search/read/send/reply tool logic (proves the JSON
token → MailCredentials → mail_protocol plumbing end-to-end); httpx mocked
only for the host-mediated attachment save/read calls (the MCP subprocess
has no direct filesystem access — mirrors bg.py's model).

The fake IMAP server runs its `asyncio.start_server` accept loop on the
SAME event loop pytest-asyncio drives the test coroutine on, so every call
into the (synchronous, imaplib-blocking) MCP tool functions must go through
`asyncio.to_thread` — otherwise the blocking call starves the loop the fake
server needs to accept/answer the connection and the SSL handshake times
out (see tests/test_mail_protocol.py, which established this pattern)."""

from __future__ import annotations

import asyncio
import base64
import json
from email.message import EmailMessage

import pytest

from server.mcp_servers.connectors import mail as mailmcp
from tests._fixtures.fake_imap_server import FakeImapServer
from tests._fixtures.fake_smtp_server import FakeSmtpServer
from tests._fixtures.mail_tls import generate as generate_tls

EMAIL = "agent@example.com"
AUTH_CODE = "s3cr3t-auth-code"


def _msg(subject: str, from_: str, body: str = "hello", attach: bytes | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_
    msg["To"] = EMAIL
    msg["Subject"] = subject
    msg["Date"] = "Mon, 1 Jan 2026 00:00:00 +0000"
    msg.set_content(body)
    if attach is not None:
        msg.add_attachment(attach, maintype="text", subtype="plain", filename="notes.txt")
    return msg.as_bytes()


@pytest.fixture
def tls(tmp_path):
    return generate_tls(tmp_path)


@pytest.fixture
def imap_server_factory(tls):
    async def _make(messages, auth_code=AUTH_CODE):
        s = FakeImapServer(
            email=EMAIL, auth_code=auth_code, messages=messages, ssl_context=tls.server_context()
        )
        await s.start()
        return s

    yield _make


@pytest.fixture
def smtp_server(tls):
    s = FakeSmtpServer(email=EMAIL, auth_code=AUTH_CODE, starttls=False, ssl_context=tls.server_context())
    s.start()
    yield s
    s.stop()


@pytest.fixture(autouse=True)
def _trust_test_ca(tls, monkeypatch):
    """mail_protocol calls `ssl.create_default_context()` with no args when
    the MCP server doesn't pass one (production never needs to — it's
    always the real CA store); tests point that at our throwaway CA.
    Captures the REAL function first — `tls.client_context()` itself calls
    `ssl.create_default_context`, so patching in place would recurse."""
    import ssl

    real_create_default_context = ssl.create_default_context
    monkeypatch.setattr(
        ssl,
        "create_default_context",
        lambda *a, **k: real_create_default_context(cafile=str(tls.certfile)),
    )


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("OWLERY_API_BASE", "http://host")
    monkeypatch.setenv("OWLERY_AUTH_TOKEN", "tok")
    monkeypatch.setenv("OWLERY_INSTALLATION_ID", "inst-1")
    monkeypatch.setenv("OWLERY_SESSION_ID", "sess-1")


def _set_token(monkeypatch, imap_port: int, smtp_port: int) -> None:
    blob = json.dumps(
        {
            "email": EMAIL,
            "auth_code": AUTH_CODE,
            "imap_host": "127.0.0.1",
            "imap_port": str(imap_port),
            "smtp_host": "127.0.0.1",
            "smtp_port": str(smtp_port),
        }
    )
    monkeypatch.setattr(mailmcp.ctx, "access_token", lambda: blob)


# --- read tools ----------------------------------------------------------


@pytest.mark.asyncio
async def test_list_recent_newest_first(env, imap_server_factory, monkeypatch):
    server = await imap_server_factory([_msg("First", "a@x.com"), _msg("Second", "b@x.com")])
    _set_token(monkeypatch, server.port, 0)
    out = json.loads(await asyncio.to_thread(mailmcp.list_recent, limit=5))
    assert [m["subject"] for m in out] == ["Second", "First"]
    await server.stop()


@pytest.mark.asyncio
async def test_search_server_side_ascii(env, imap_server_factory, monkeypatch):
    server = await imap_server_factory(
        [_msg("Invoice attached", "billing@corp.com"), _msg("Lunch?", "friend@x.com")]
    )
    _set_token(monkeypatch, server.port, 0)
    out = json.loads(await asyncio.to_thread(mailmcp.search, query="Invoice"))
    assert len(out) == 1 and out[0]["subject"] == "Invoice attached"
    await server.stop()


@pytest.mark.asyncio
async def test_search_non_ascii_falls_back_client_side(env, imap_server_factory, monkeypatch):
    server = await imap_server_factory(
        [_msg("你好邮件", "friend@qq.com"), _msg("Unrelated", "other@x.com")]
    )
    _set_token(monkeypatch, server.port, 0)
    out = json.loads(await asyncio.to_thread(mailmcp.search, query="你好"))
    assert len(out) == 1 and out[0]["subject"] == "你好邮件"
    await server.stop()


@pytest.mark.asyncio
async def test_read_full_message_with_attachment(env, imap_server_factory, monkeypatch):
    server = await imap_server_factory(
        [_msg("With attachment", "a@x.com", body="see attached", attach=b"file contents")]
    )
    _set_token(monkeypatch, server.port, 0)
    out = json.loads(await asyncio.to_thread(mailmcp.read, uid="1"))
    assert out["subject"] == "With attachment"
    assert "see attached" in out["body"]
    assert out["attachments"] == [
        {"index": 0, "filename": "notes.txt", "content_type": "text/plain", "size": len(b"file contents")}
    ]
    await server.stop()


@pytest.mark.asyncio
async def test_read_missing_uid_errors(env, imap_server_factory, monkeypatch):
    server = await imap_server_factory([_msg("Only one", "a@x.com")])
    _set_token(monkeypatch, server.port, 0)
    out = await asyncio.to_thread(mailmcp.read, uid="999")
    assert out.startswith("Error:")
    await server.stop()


@pytest.mark.asyncio
async def test_get_attachment_saves_via_host(env, imap_server_factory, monkeypatch):
    server = await imap_server_factory([_msg("With attachment", "a@x.com", attach=b"file contents")])
    _set_token(monkeypatch, server.port, 0)

    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"path": "mail-attachments/notes.txt", "size": 13}

    def fake_post(url, json=None, headers=None, timeout=None, trust_env=None):
        captured["url"], captured["json"] = url, json
        assert trust_env is False
        return FakeResp()

    monkeypatch.setattr(mailmcp.httpx, "post", fake_post)
    out = json.loads(
        await asyncio.to_thread(mailmcp.get_attachment, uid="1", attachment_index=0)
    )
    assert out == {"saved_path": "mail-attachments/notes.txt", "size": 13, "content_type": "text/plain"}
    assert captured["url"] == "http://host/api/sessions/sess-1/files/save"
    assert captured["json"]["relative_dir"] == "mail-attachments"
    assert captured["json"]["filename"] == "notes.txt"
    assert base64.b64decode(captured["json"]["content_base64"]) == b"file contents"
    await server.stop()


# --- auth / unavailable ---------------------------------------------------


@pytest.mark.asyncio
async def test_token_unavailable_returns_error_for_every_tool(env, monkeypatch):
    monkeypatch.setattr(mailmcp.ctx, "access_token", lambda: None)
    assert mailmcp.list_recent().startswith("Error:")
    assert mailmcp.search(query="x").startswith("Error:")
    assert mailmcp.read(uid="1").startswith("Error:")
    assert mailmcp.get_attachment(uid="1", attachment_index=0).startswith("Error:")
    assert mailmcp.send(to="a@x.com", subject="s", body="b").startswith("Error:")
    assert mailmcp.reply(uid="1", body="b").startswith("Error:")


@pytest.mark.asyncio
async def test_bad_auth_code_marks_reconnect(env, imap_server_factory, monkeypatch):
    server = await imap_server_factory([_msg("Hi", "a@x.com")])
    recon: list[str] = []
    monkeypatch.setattr(
        mailmcp.ctx, "mark_needs_reconnect", lambda code="invalid_grant": recon.append(code)
    )
    blob = json.dumps(
        {
            "email": EMAIL,
            "auth_code": "totally-wrong",
            "imap_host": "127.0.0.1",
            "imap_port": str(server.port),
            "smtp_host": "127.0.0.1",
            "smtp_port": "0",
        }
    )
    monkeypatch.setattr(mailmcp.ctx, "access_token", lambda: blob)
    out = await asyncio.to_thread(mailmcp.list_recent)
    assert "reconnect" in out.lower()
    assert recon == ["invalid_grant"]
    await server.stop()


# --- write tools -----------------------------------------------------------
# aiosmtpd's Controller runs its own background thread, so these calls don't
# need asyncio.to_thread — kept anyway for symmetry / in case a test also
# touches the (event-loop-bound) fake IMAP server.


@pytest.mark.asyncio
async def test_send_plain_message(env, smtp_server, monkeypatch):
    _set_token(monkeypatch, 0, smtp_server.port)
    out = json.loads(await asyncio.to_thread(mailmcp.send, to="bob@x.com", subject="Hi", body="hello there"))
    assert out == {"sent": True, "to": "bob@x.com", "cc": None, "subject": "Hi"}
    assert len(smtp_server.sent) == 1
    sent = smtp_server.sent[0]
    assert sent["mail_from"] == EMAIL
    assert sent["rcpt_tos"] == ["bob@x.com"]
    assert b"hello there" in sent["content"]


@pytest.mark.asyncio
async def test_send_with_cc_and_attachment_from_workdir(env, smtp_server, monkeypatch):
    _set_token(monkeypatch, 0, smtp_server.port)

    class FakeResp:
        status_code = 200
        content = b"attachment bytes"

    def fake_get(url, params=None, headers=None, timeout=None, trust_env=None):
        assert params == {"path": "report.txt"}
        assert trust_env is False
        return FakeResp()

    monkeypatch.setattr(mailmcp.httpx, "get", fake_get)
    out = json.loads(
        await asyncio.to_thread(
            mailmcp.send,
            to="bob@x.com",
            subject="Report",
            body="see attached",
            cc="carol@x.com",
            attachment_paths=["report.txt"],
        )
    )
    assert out["cc"] == "carol@x.com"
    sent = smtp_server.sent[0]
    assert sent["rcpt_tos"] == ["bob@x.com", "carol@x.com"]
    # MIME-encoded (base64) in the raw envelope, not the literal bytes.
    assert base64.b64encode(b"attachment bytes") in sent["content"]
    assert b"report.txt" in sent["content"]


@pytest.mark.asyncio
async def test_reply_threads_and_sends(env, imap_server_factory, smtp_server, monkeypatch):
    original = _msg("Question", "alice@example.com", body="are you free?")
    server = await imap_server_factory([original])
    _set_token(monkeypatch, server.port, smtp_server.port)

    out = json.loads(await asyncio.to_thread(mailmcp.reply, uid="1", body="yes, free after 3pm"))
    assert out["to"] == "alice@example.com"
    assert out["subject"] == "Re: Question"
    sent = smtp_server.sent[0]
    assert sent["rcpt_tos"] == ["alice@example.com"]
    assert b"yes, free after 3pm" in sent["content"]
    assert b"Re: Question" in sent["content"]
    await server.stop()


@pytest.mark.asyncio
async def test_reply_does_not_double_prefix_subject(env, imap_server_factory, smtp_server, monkeypatch):
    original = _msg("Re: Already a reply", "alice@example.com")
    server = await imap_server_factory([original])
    _set_token(monkeypatch, server.port, smtp_server.port)

    out = json.loads(await asyncio.to_thread(mailmcp.reply, uid="1", body="ok"))
    assert out["subject"] == "Re: Already a reply"
    await server.stop()
