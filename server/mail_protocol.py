"""Shared IMAP/SMTP plumbing for the `mail` connector (docs/plans/mail-connector.md).

Used by both `server/connectors/mail.py` (install-time verification — real
IMAP LOGIN + SMTP AUTH, §4.1) and `server/mcp_servers/connectors/mail.py`
(runtime tool calls). Kept out of both so neither has to re-implement the
QQ/163 "Unsafe Login" workaround, charset fallback, or connection setup.

Known traps this module exists to handle (§4.4):
  - QQ/163 IMAP requires an RFC 2971 `ID` command right after LOGIN, before
    any other command, or they reject with "Unsafe Login". `imaplib` has no
    high-level API for it; `IMAP4.xatom` sends the raw extension command.
  - The static credential is an authorization code, not the account
    password — auth failures should say so.
  - Chinese mail is often RFC 2047 header-encoded with a GB18030/GBK body
    charset; decoding falls back through GB18030 when the declared charset
    fails or is absent.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.message import Message as _EmailMessageBase
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0
# 587 is the universal STARTTLS submission port; everything else (465 for
# QQ/163, 993-style ports some custom setups reuse for SMTP) gets implicit
# TLS. This keeps the install form to host+port with no separate
# encryption-mode field while still covering Outlook's STARTTLS-on-587
# requirement (§4.4).
_STARTTLS_SMTP_PORT = 587

_IMAP_ID_ARGS = '("name" "Owlery" "version" "1.0" "vendor" "Owlery")'

# Optional extra trusted CA for self-hosted mail servers on a private CA
# (and the only hook the e2e fixture uses to point the connector at a local
# fake server without weakening default verification) — ADDED to, never
# replacing, the system trust store.
_EXTRA_CA_ENV = "OWLERY_MAIL_CA_FILE"


def _default_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    extra_ca = os.environ.get(_EXTRA_CA_ENV)
    if extra_ca:
        ctx.load_verify_locations(cafile=extra_ca)
    return ctx


class MailProtocolError(Exception):
    """Base for mail connector failures; the message is safe to show the
    user verbatim (it's built from the server's own error text)."""


class MailConnectionError(MailProtocolError):
    """Could not reach the IMAP/SMTP host at all (DNS, TCP, TLS handshake)."""


class MailAuthError(MailProtocolError):
    """Connected, but LOGIN/AUTH was rejected."""


@dataclass(frozen=True)
class MailCredentials:
    email: str
    auth_code: str
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int

    @classmethod
    def from_fields(cls, fields: dict[str, str]) -> "MailCredentials":
        return cls(
            email=fields["email"],
            auth_code=fields["auth_code"],
            imap_host=fields["imap_host"],
            imap_port=int(fields["imap_port"]),
            smtp_host=fields["smtp_host"],
            smtp_port=int(fields["smtp_port"]),
        )


def _send_imap_id(conn: imaplib.IMAP4) -> None:
    """Best-effort RFC 2971 ID. Servers that don't support it reply NO/BAD,
    which we ignore — only QQ/163-style servers actually require it."""
    try:
        conn.xatom("ID", _IMAP_ID_ARGS)
    except Exception:
        logger.debug("IMAP ID command failed or unsupported", exc_info=True)


def imap_connect(
    creds: MailCredentials,
    *,
    ssl_context: ssl.SSLContext | None = None,
    timeout: float = _TIMEOUT,
) -> imaplib.IMAP4_SSL:
    """Connect, LOGIN, and send the RFC 2971 ID workaround. Raises
    MailConnectionError / MailAuthError on failure; caller must `.logout()`
    the returned connection."""
    ctx = ssl_context or _default_ssl_context()
    try:
        conn = imaplib.IMAP4_SSL(
            creds.imap_host, creds.imap_port, ssl_context=ctx, timeout=timeout
        )
    except (OSError, ssl.SSLError) as e:
        raise MailConnectionError(
            f"Could not connect to IMAP {creds.imap_host}:{creds.imap_port}: {e}"
        ) from e
    try:
        conn.login(creds.email, creds.auth_code)
    except imaplib.IMAP4.error as e:
        try:
            conn.logout()
        except Exception:
            pass
        raise MailAuthError(
            f"IMAP login failed: {e}. Make sure you're using the mailbox's "
            "authorization code, not your account password."
        ) from e
    _send_imap_id(conn)
    return conn


def smtp_connect(
    creds: MailCredentials,
    *,
    ssl_context: ssl.SSLContext | None = None,
    timeout: float = _TIMEOUT,
) -> smtplib.SMTP:
    """Connect and AUTH (implicit TLS, or STARTTLS on port 587). Raises
    MailConnectionError / MailAuthError on failure; caller must `.quit()`
    the returned connection."""
    ctx = ssl_context or _default_ssl_context()
    use_starttls = creds.smtp_port == _STARTTLS_SMTP_PORT
    try:
        if use_starttls:
            smtp: smtplib.SMTP = smtplib.SMTP(
                creds.smtp_host, creds.smtp_port, timeout=timeout
            )
            smtp.starttls(context=ctx)
        else:
            smtp = smtplib.SMTP_SSL(
                creds.smtp_host, creds.smtp_port, timeout=timeout, context=ctx
            )
    except (OSError, ssl.SSLError, smtplib.SMTPException) as e:
        raise MailConnectionError(
            f"Could not connect to SMTP {creds.smtp_host}:{creds.smtp_port}: {e}"
        ) from e
    try:
        smtp.login(creds.email, creds.auth_code)
    except smtplib.SMTPException as e:
        try:
            smtp.quit()
        except Exception:
            pass
        raise MailAuthError(
            f"SMTP login failed: {e}. Make sure you're using the mailbox's "
            "authorization code, not your account password."
        ) from e
    return smtp


def verify_credentials(
    creds: MailCredentials,
    *,
    imap_ssl_context: ssl.SSLContext | None = None,
    smtp_ssl_context: ssl.SSLContext | None = None,
) -> None:
    """Real IMAP LOGIN + SMTP AUTH against the live service
    (mail-connector.md §4.1 — install is verify-then-persist). Raises
    MailProtocolError on any failure; returns None on success."""
    conn = imap_connect(creds, ssl_context=imap_ssl_context)
    try:
        typ, _data = conn.select("INBOX", readonly=True)
        if typ != "OK":
            raise MailAuthError(
                "IMAP login succeeded but selecting INBOX failed — check "
                "that IMAP access is enabled for this mailbox."
            )
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    smtp = smtp_connect(creds, ssl_context=smtp_ssl_context)
    try:
        smtp.quit()
    except Exception:
        pass


# --- message decoding --------------------------------------------------


def _decode_bytes(payload: bytes, charset: str | None) -> str:
    """Decode with the declared charset, falling back through GB18030 (a
    GBK/GB2312 superset) then UTF-8-with-replacement — Chinese providers
    routinely mislabel or omit the charset (§4.4)."""
    candidates = [c for c in (charset, "utf-8", "gb18030") if c]
    for enc in candidates:
        try:
            return payload.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


class _HTMLTextExtractor(HTMLParser):
    _BLOCK_TAGS = {
        "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "table", "blockquote",
    }
    _SKIP_TAGS = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self._BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.chunks.append(data)


def html_to_text(html: str) -> str:
    """Strip an HTML email body down to readable plain text (no rich
    rendering — §4.5 'not doing HTML composition' applies to reading too:
    we just need something the model can read)."""
    parser = _HTMLTextExtractor()
    parser.feed(html)
    parser.close()
    lines = [ln.strip() for ln in "".join(parser.chunks).splitlines()]
    out: list[str] = []
    blank = False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


@dataclass(frozen=True)
class AttachmentMeta:
    index: int
    filename: str
    content_type: str
    size: int


@dataclass(frozen=True)
class ParsedMessage:
    uid: str
    subject: str
    from_: str
    to: str
    cc: str
    date: str
    message_id: str
    in_reply_to: str
    text_body: str
    attachments: list[AttachmentMeta] = field(default_factory=list)


def _header(msg: _EmailMessageBase, name: str) -> str:
    try:
        value = msg.get(name)
    except Exception:
        return ""
    return str(value) if value is not None else ""


def _body_text(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain",))
    kind = "plain"
    if part is None:
        part = msg.get_body(preferencelist=("html",))
        kind = "html"
    if part is None:
        return ""
    raw = part.get_payload(decode=True) or b""
    text = _decode_bytes(raw, part.get_content_charset())
    return html_to_text(text) if kind == "html" else text


def _attachment_metas(msg: EmailMessage) -> list[AttachmentMeta]:
    metas = []
    for i, part in enumerate(msg.iter_attachments()):
        raw = part.get_payload(decode=True) or b""
        metas.append(
            AttachmentMeta(
                index=i,
                filename=part.get_filename() or f"attachment-{i}",
                content_type=part.get_content_type(),
                size=len(raw),
            )
        )
    return metas


def parse_message(uid: str, raw: bytes) -> ParsedMessage:
    """Parse one RFC 822 message into text the model can consume. Uses
    `policy=default` (email.message.EmailMessage) so header access
    transparently decodes RFC 2047 encoded-words."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)
    return ParsedMessage(
        uid=uid,
        subject=_header(msg, "Subject"),
        from_=_header(msg, "From"),
        to=_header(msg, "To"),
        cc=_header(msg, "Cc"),
        date=_header(msg, "Date"),
        message_id=_header(msg, "Message-ID"),
        in_reply_to=_header(msg, "In-Reply-To"),
        text_body=_body_text(msg),
        attachments=_attachment_metas(msg),
    )


def extract_attachment(raw: bytes, index: int) -> tuple[bytes, str, str]:
    """Return (content, filename, content_type) for the attachment at
    `index` (same ordering as `parse_message(...).attachments`)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)
    for i, part in enumerate(msg.iter_attachments()):
        if i == index:
            content = part.get_payload(decode=True) or b""
            filename = part.get_filename() or f"attachment-{index}"
            return content, filename, part.get_content_type()
    raise MailProtocolError(f"No attachment at index {index}")
