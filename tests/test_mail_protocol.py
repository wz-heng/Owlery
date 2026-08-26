"""Tests for `server.mail_protocol` — IMAP LOGIN + RFC 2971 ID, SMTP AUTH
(implicit TLS and STARTTLS), and message decoding (mail-connector.md §4.4 /
§4.5). Uses real local fake IMAP/SMTP servers over real TLS (self-signed
test cert from `tests/_fixtures/mail_tls.py`) — no real mailbox involved.
"""

from __future__ import annotations

import asyncio
from email.message import EmailMessage

import pytest

from server import mail_protocol
from tests._fixtures.fake_imap_server import FakeImapServer
from tests._fixtures.fake_smtp_server import FakeSmtpServer
from tests._fixtures.mail_tls import generate as generate_tls

EMAIL = "agent@example.com"
AUTH_CODE = "s3cr3t-auth-code"


def _msg(subject: str, from_: str, body: str = "hello", charset: str = "utf-8") -> bytes:
    msg = EmailMessage()
    msg["From"] = from_
    msg["To"] = EMAIL
    msg["Subject"] = subject
    msg["Date"] = "Mon, 1 Jan 2026 00:00:00 +0000"
    msg.set_content(body, charset=charset)
    return msg.as_bytes()


@pytest.fixture
def tls(tmp_path):
    return generate_tls(tmp_path)


@pytest.fixture
def creds():
    def _make(imap_port: int, smtp_port: int) -> mail_protocol.MailCredentials:
        return mail_protocol.MailCredentials(
            email=EMAIL,
            auth_code=AUTH_CODE,
            imap_host="127.0.0.1",
            imap_port=imap_port,
            smtp_host="127.0.0.1",
            smtp_port=smtp_port,
        )

    return _make


@pytest.fixture
async def imap_server(tls):
    server = FakeImapServer(
        email=EMAIL,
        auth_code=AUTH_CODE,
        messages=[_msg("Hello there", "alice@example.com")],
        ssl_context=tls.server_context(),
    )
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
def smtp_ssl_server(tls):
    server = FakeSmtpServer(
        email=EMAIL, auth_code=AUTH_CODE, starttls=False, ssl_context=tls.server_context()
    )
    server.start()
    yield server
    server.stop()


@pytest.fixture
def smtp_starttls_server(tls):
    server = FakeSmtpServer(
        email=EMAIL, auth_code=AUTH_CODE, starttls=True, ssl_context=tls.server_context()
    )
    server.start()
    yield server
    server.stop()


# --- IMAP ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_imap_connect_login_and_id(imap_server, tls, creds):
    c = creds(imap_server.port, 0)

    def run():
        conn = mail_protocol.imap_connect(c, ssl_context=tls.client_context())
        try:
            typ, _ = conn.select("INBOX", readonly=True)
            assert typ == "OK"
        finally:
            conn.logout()

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_default_ssl_context_trusts_extra_ca_via_env(imap_server, tls, creds, monkeypatch):
    """OWLERY_MAIL_CA_FILE ADDS a trusted CA (self-hosted mail servers on a
    private CA) rather than replacing the system store — verified here by
    connecting with no explicit ssl_context at all, only the env var."""
    monkeypatch.setenv("OWLERY_MAIL_CA_FILE", str(tls.certfile))
    c = creds(imap_server.port, 0)

    def run():
        conn = mail_protocol.imap_connect(c)  # no ssl_context kwarg
        conn.logout()

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_imap_select_rejected_without_id_workaround(tls):
    """Proves the fixture faithfully reproduces QQ/163's "Unsafe Login":
    if a client skipped the RFC 2971 ID step, SELECT would be rejected."""
    import imaplib

    server = FakeImapServer(
        email=EMAIL, auth_code=AUTH_CODE, messages=[], ssl_context=tls.server_context()
    )
    await server.start()
    try:

        def run():
            conn = imaplib.IMAP4_SSL(
                "127.0.0.1", server.port, ssl_context=tls.client_context(), timeout=5
            )
            conn.login(EMAIL, AUTH_CODE)
            typ, data = conn.select("INBOX")
            assert typ == "NO"
            assert b"Unsafe Login" in data[0]
            conn.logout()

        await asyncio.to_thread(run)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_imap_wrong_auth_code_raises_mail_auth_error(imap_server, tls, creds):
    bad = mail_protocol.MailCredentials(
        email=EMAIL,
        auth_code="wrong",
        imap_host="127.0.0.1",
        imap_port=imap_server.port,
        smtp_host="127.0.0.1",
        smtp_port=0,
    )

    def run():
        with pytest.raises(mail_protocol.MailAuthError, match="password"):
            mail_protocol.imap_connect(bad, ssl_context=tls.client_context())

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_imap_connect_unreachable_host_raises_connection_error(tls, creds):
    c = creds(1, 0)  # port 1: nothing listening

    def run():
        with pytest.raises(mail_protocol.MailConnectionError):
            mail_protocol.imap_connect(c, ssl_context=tls.client_context())

    await asyncio.to_thread(run)


# --- SMTP --------------------------------------------------------------


@pytest.mark.asyncio
async def test_smtp_ssl_auth_succeeds(smtp_ssl_server, tls, creds):
    c = creds(0, smtp_ssl_server.port)

    def run():
        smtp = mail_protocol.smtp_connect(c, ssl_context=tls.client_context())
        smtp.quit()

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_smtp_starttls_auth_succeeds(smtp_starttls_server, tls, creds, monkeypatch):
    """Exercises `smtp_connect`'s actual STARTTLS-vs-implicit-TLS branch
    (real port 587 can't be bound in a test), not just STARTTLS-in-general:
    the fake STARTTLS server's listener is plaintext until upgraded, so if
    `smtp_connect` took the wrong (direct-SSL) branch here the handshake
    would fail outright."""
    # smtp_connect's branch is `creds.smtp_port == _STARTTLS_SMTP_PORT`;
    # point that threshold at the fixture's ephemeral port instead of the
    # real (unbindable-in-tests) 587.
    monkeypatch.setattr(mail_protocol, "_STARTTLS_SMTP_PORT", smtp_starttls_server.port)
    c = creds(0, smtp_starttls_server.port)

    def run():
        smtp = mail_protocol.smtp_connect(c, ssl_context=tls.client_context())
        smtp.quit()

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_smtp_wrong_auth_code_raises_mail_auth_error(smtp_ssl_server, tls, creds):
    c = mail_protocol.MailCredentials(
        email=EMAIL,
        auth_code="wrong",
        imap_host="127.0.0.1",
        imap_port=0,
        smtp_host="127.0.0.1",
        smtp_port=smtp_ssl_server.port,
    )

    def run():
        with pytest.raises(mail_protocol.MailAuthError, match="password"):
            mail_protocol.smtp_connect(c, ssl_context=tls.client_context())

    await asyncio.to_thread(run)


# --- verify_credentials (both legs) ----------------------------------------


@pytest.mark.asyncio
async def test_verify_credentials_success(imap_server, smtp_ssl_server, tls, creds):
    c = creds(imap_server.port, smtp_ssl_server.port)

    def run():
        mail_protocol.verify_credentials(
            c, imap_ssl_context=tls.client_context(), smtp_ssl_context=tls.client_context()
        )

    await asyncio.to_thread(run)


@pytest.mark.asyncio
async def test_verify_credentials_bad_smtp_fails(imap_server, smtp_ssl_server, tls):
    c = mail_protocol.MailCredentials(
        email=EMAIL,
        auth_code=AUTH_CODE,
        imap_host="127.0.0.1",
        imap_port=imap_server.port,
        smtp_host="127.0.0.1",
        smtp_port=1,  # nothing listening
    )

    def run():
        with pytest.raises(mail_protocol.MailProtocolError):
            mail_protocol.verify_credentials(
                c, imap_ssl_context=tls.client_context(), smtp_ssl_context=tls.client_context()
            )

    await asyncio.to_thread(run)


# --- message decoding --------------------------------------------------


def test_parse_message_basic():
    raw = _msg("Hi", "alice@example.com", body="plain body")
    parsed = mail_protocol.parse_message("1", raw)
    assert parsed.subject == "Hi"
    assert parsed.from_ == "alice@example.com"
    assert "plain body" in parsed.text_body
    assert parsed.attachments == []


def test_parse_message_gb18030_body_fallback():
    msg = EmailMessage()
    msg["From"] = "someone@qq.com"
    msg["To"] = EMAIL
    msg["Subject"] = "Chinese body"
    body = "你好,这是一封测试邮件".encode("gb18030")
    msg.add_header("Content-Type", "text/plain", charset="gb18030")
    msg.set_payload(body)
    raw = bytes(msg)
    parsed = mail_protocol.parse_message("1", raw)
    assert "你好" in parsed.text_body


def test_parse_message_rfc2047_subject():
    raw = _msg("=?gb2312?B?xOO6ww==?=", "someone@163.com")
    parsed = mail_protocol.parse_message("1", raw)
    # decoded (not left as a raw encoded-word)
    assert "=?gb2312?" not in parsed.subject


def test_parse_message_html_body_stripped_to_text():
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = EMAIL
    msg["Subject"] = "HTML mail"
    msg.set_content("<html><body><p>Hello <b>world</b></p></body></html>", subtype="html")
    raw = msg.as_bytes()
    parsed = mail_protocol.parse_message("1", raw)
    assert "<p>" not in parsed.text_body
    assert "Hello" in parsed.text_body and "world" in parsed.text_body


def test_parse_message_attachments_metadata():
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = EMAIL
    msg["Subject"] = "With attachment"
    msg.set_content("see attached")
    msg.add_attachment(b"file bytes here", maintype="text", subtype="plain", filename="notes.txt")
    raw = msg.as_bytes()
    parsed = mail_protocol.parse_message("1", raw)
    assert len(parsed.attachments) == 1
    a = parsed.attachments[0]
    assert a.filename == "notes.txt"
    assert a.size == len(b"file bytes here")


def test_extract_attachment_returns_bytes():
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = EMAIL
    msg["Subject"] = "With attachment"
    msg.set_content("see attached")
    msg.add_attachment(b"payload", maintype="application", subtype="octet-stream", filename="a.bin")
    raw = msg.as_bytes()
    content, filename, content_type = mail_protocol.extract_attachment(raw, 0)
    assert content == b"payload"
    assert filename == "a.bin"
    assert content_type == "application/octet-stream"


def test_extract_attachment_missing_index_raises():
    raw = _msg("No attachments", "alice@example.com")
    with pytest.raises(mail_protocol.MailProtocolError):
        mail_protocol.extract_attachment(raw, 0)


def test_html_to_text_strips_script_and_style():
    html = "<style>.x{}</style><script>alert(1)</script><p>Visible</p>"
    text = mail_protocol.html_to_text(html)
    assert "Visible" in text
    assert "alert" not in text
    assert ".x{}" not in text
