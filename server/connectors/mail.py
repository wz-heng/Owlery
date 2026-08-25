"""Generic IMAP/SMTP mail connector (docs/plans/mail-connector.md).

Static-credential kind: no OAuth, no provider REST API — a real IMAP LOGIN +
SMTP AUTH against the fields the user typed is the verification (§4.1). QQ,
163, and Outlook are front-end presets over this one `mail` kind (host/port
prefills); the framework and DB only know 'mail'. The matching MCP server
lives in `server/mcp_servers/connectors/mail.py`.
"""

from __future__ import annotations

import asyncio

from .. import mail_protocol
from .base import ConnectorBase, StaticCredentialField, StaticCredentialPreset, StaticVerifyError
from .registry import register

_FIELDS = (
    StaticCredentialField(
        key="email",
        label="Email address",
        placeholder="you@qq.com",
    ),
    StaticCredentialField(
        key="auth_code",
        label="Authorization code",
        secret=True,
        help_text=(
            "Not your account password — generate this in the mailbox's "
            "settings under IMAP/SMTP (for QQ Mail: 设置 → 账户 → 开启 "
            "IMAP/SMTP服务)."
        ),
    ),
    StaticCredentialField(key="imap_host", label="IMAP host", default="imap.qq.com"),
    StaticCredentialField(key="imap_port", label="IMAP port", default="993"),
    StaticCredentialField(key="smtp_host", label="SMTP host", default="smtp.qq.com"),
    StaticCredentialField(key="smtp_port", label="SMTP port", default="465"),
)

_PRESETS = (
    StaticCredentialPreset(
        key="qq",
        label="QQ Mail",
        values={
            "imap_host": "imap.qq.com",
            "imap_port": "993",
            "smtp_host": "smtp.qq.com",
            "smtp_port": "465",
        },
    ),
    StaticCredentialPreset(
        key="163",
        label="163 Mail",
        values={
            "imap_host": "imap.163.com",
            "imap_port": "993",
            "smtp_host": "smtp.163.com",
            "smtp_port": "465",
        },
    ),
    StaticCredentialPreset(
        key="outlook",
        label="Outlook",
        values={
            "imap_host": "outlook.office365.com",
            "imap_port": "993",
            "smtp_host": "smtp.office365.com",
            "smtp_port": "587",
        },
    ),
    StaticCredentialPreset(key="custom", label="Custom", values={}),
)


class MailConnector(ConnectorBase):
    kind = "mail"
    display_name = "Mail (IMAP/SMTP)"
    category = "email"
    allows_multiple = True
    auth_mode = "static"
    static_fields = _FIELDS
    static_presets = _PRESETS
    tools = ("list_recent", "search", "read", "get_attachment", "send", "reply")
    blurb_intro = (
        "Read and send email on the linked mailbox via IMAP/SMTP: list "
        "recent INBOX messages, best-effort search, read a full message "
        "(attachments listed by metadata — fetch one on demand with "
        "get_attachment), and send or reply. Sending is an outbound action "
        "— tell the user what you're about to send before calling send or "
        "reply."
    )

    async def verify_static_credentials(self, fields: dict[str, str]) -> tuple[str, str]:
        creds = mail_protocol.MailCredentials.from_fields(fields)
        try:
            await asyncio.to_thread(mail_protocol.verify_credentials, creds)
        except mail_protocol.MailProtocolError as e:
            raise StaticVerifyError(str(e)) from e
        return creds.email, creds.email


register(MailConnector())
