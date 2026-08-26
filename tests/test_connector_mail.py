"""MailConnector descriptor (mail-connector.md §4.1): registration, static
field/preset declarations, and `verify_static_credentials` wiring against
`server.mail_protocol` (mocked here — the real IMAP/SMTP round trip is
covered by tests/test_mail_protocol.py and the fake-server integration
test below)."""

from __future__ import annotations

import pytest

from server import mail_protocol
from server.connectors import mail as mail_connector
from server.connectors.base import StaticVerifyError
from server.connectors.registry import get_connector


def test_mail_is_registered():
    c = get_connector("mail")
    assert isinstance(c, mail_connector.MailConnector)
    assert c.auth_mode == "static"
    assert c.allows_multiple is True


def test_static_fields_cover_all_credential_pieces():
    keys = {f.key for f in mail_connector.MailConnector.static_fields}
    assert keys == {"email", "auth_code", "imap_host", "imap_port", "smtp_host", "smtp_port"}
    auth_field = next(f for f in mail_connector.MailConnector.static_fields if f.key == "auth_code")
    assert auth_field.secret is True


def test_presets_cover_qq_163_outlook_and_custom():
    keys = {p.key for p in mail_connector.MailConnector.static_presets}
    assert keys == {"qq", "163", "outlook", "custom"}
    qq = next(p for p in mail_connector.MailConnector.static_presets if p.key == "qq")
    assert qq.values["imap_host"] == "imap.qq.com"
    assert qq.values["smtp_port"] == "465"
    outlook = next(p for p in mail_connector.MailConnector.static_presets if p.key == "outlook")
    assert outlook.values["smtp_port"] == "587"  # STARTTLS port


@pytest.mark.asyncio
async def test_verify_static_credentials_success(monkeypatch):
    captured = {}

    def fake_verify(creds, **kw):
        captured["creds"] = creds

    monkeypatch.setattr(mail_protocol, "verify_credentials", fake_verify)
    c = mail_connector.MailConnector()
    ext_id, label = await c.verify_static_credentials(
        {
            "email": "me@qq.com",
            "auth_code": "code",
            "imap_host": "imap.qq.com",
            "imap_port": "993",
            "smtp_host": "smtp.qq.com",
            "smtp_port": "465",
        }
    )
    assert ext_id == "me@qq.com" and label == "me@qq.com"
    assert captured["creds"].email == "me@qq.com"


@pytest.mark.asyncio
async def test_verify_static_credentials_wraps_protocol_error(monkeypatch):
    def fake_verify(creds, **kw):
        raise mail_protocol.MailAuthError("IMAP login failed: bad creds")

    monkeypatch.setattr(mail_protocol, "verify_credentials", fake_verify)
    c = mail_connector.MailConnector()
    with pytest.raises(StaticVerifyError, match="IMAP login failed"):
        await c.verify_static_credentials(
            {
                "email": "me@qq.com",
                "auth_code": "wrong",
                "imap_host": "imap.qq.com",
                "imap_port": "993",
                "smtp_host": "smtp.qq.com",
                "smtp_port": "465",
            }
        )


@pytest.mark.asyncio
async def test_verify_static_credentials_rejects_malformed_port_as_form_error():
    """A bad port must surface as a StaticVerifyError (→ 400 form error),
    not an uncaught ValueError (→ 500) — the port string is user input from
    the install form, parsed before any network call happens."""
    c = mail_connector.MailConnector()
    with pytest.raises(StaticVerifyError, match="IMAP port"):
        await c.verify_static_credentials(
            {
                "email": "me@qq.com",
                "auth_code": "code",
                "imap_host": "imap.qq.com",
                "imap_port": "not-a-number",
                "smtp_host": "smtp.qq.com",
                "smtp_port": "465",
            }
        )


@pytest.mark.asyncio
async def test_verify_static_credentials_rejects_out_of_range_port():
    c = mail_connector.MailConnector()
    with pytest.raises(StaticVerifyError, match="between 1 and 65535"):
        await c.verify_static_credentials(
            {
                "email": "me@qq.com",
                "auth_code": "code",
                "imap_host": "imap.qq.com",
                "imap_port": "993",
                "smtp_host": "smtp.qq.com",
                "smtp_port": "70000",
            }
        )
