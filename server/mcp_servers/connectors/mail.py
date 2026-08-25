"""Mail (IMAP/SMTP) connector MCP server (mail-connector.md §4.2).

Spawned as `python -m server.mcp_servers.connectors.mail`. Unlike the
REST-based connectors (github/gmail), this one speaks raw IMAP/SMTP via
`server.mail_protocol` — the "token" `ConnectorContext` fetches from the
host is a JSON blob of `{email, auth_code, imap_host, imap_port, smtp_host,
smtp_port}` (connector_manager.py's static-install path), not an opaque
bearer token. Read tools scope to INBOX only (v1 doesn't do folder
browsing). Each tool call opens its own IMAP/SMTP connection and tears it
down — no persistent session across calls.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import sys
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid
from pathlib import Path
from typing import Any, Callable

import httpx

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from server import mail_protocol  # noqa: E402
from server.mcp_servers.connectors._shared import (  # noqa: E402
    ConnectorContext,
    to_text,
    truncate,
)

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s mail-mcp %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP("owlery-mail")
ctx = ConnectorContext()

_RECONNECT_MSG = (
    "Error: mailbox login failed — the authorization code may have been "
    "revoked. Ask the user to reconnect this mailbox in Owlery's sidebar."
)
_HTTP_TIMEOUT = 30.0
# Fallback client-side search (§4.4: IMAP SEARCH's CHARSET support for
# non-ASCII is unreliable on QQ) scans at most this many recent messages.
_MAX_SCAN = 200


def _session_id() -> str | None:
    return os.environ.get("OWLERY_SESSION_ID")


def _creds() -> mail_protocol.MailCredentials | None:
    token = ctx.access_token()
    if token is None:
        return None
    try:
        fields = json.loads(token)
    except json.JSONDecodeError:
        logger.error("mail connector token is not valid JSON")
        return None
    return mail_protocol.MailCredentials.from_fields(fields)


def _search_uids(conn: Any, criteria: str) -> list[str]:
    typ, data = conn.uid("SEARCH", None, criteria)
    if typ != "OK" or not data or not data[0]:
        return []
    return [u.decode() for u in data[0].split()]


def _fetch_raw(conn: Any, uid: str) -> bytes:
    typ, data = conn.uid("FETCH", uid, "(RFC822)")
    if typ != "OK" or not data or data[0] is None:
        raise mail_protocol.MailProtocolError(f"could not fetch message {uid}")
    return data[0][1]


def _brief(uid: str, raw: bytes) -> dict:
    msg = mail_protocol.parse_message(uid, raw)
    return {"uid": msg.uid, "from": msg.from_, "subject": msg.subject, "date": msg.date}


def _imap_call(fn: Callable[[Any], Any]) -> tuple[Any, str | None]:
    """Open an IMAP connection scoped to INBOX, run fn(conn), always
    logout. Returns (result, None) or (None, error_message)."""
    creds = _creds()
    if creds is None:
        return None, "Error: connector unavailable — reconnect this mailbox in Owlery."
    try:
        conn = mail_protocol.imap_connect(creds)
    except mail_protocol.MailAuthError:
        ctx.mark_needs_reconnect("invalid_grant")
        return None, _RECONNECT_MSG
    except mail_protocol.MailConnectionError as e:
        return None, f"Error: {e}"
    try:
        typ, _data = conn.select("INBOX", readonly=True)
        if typ != "OK":
            return None, "Error: could not open INBOX."
        return fn(conn), None
    except mail_protocol.MailProtocolError as e:
        return None, f"Error: {e}"
    except Exception as e:  # noqa: BLE001 — surface as a tool error, don't crash the server
        logger.warning("IMAP call failed: %s", e)
        return None, f"Error: IMAP request failed: {e}"
    finally:
        try:
            conn.logout()
        except Exception:
            pass


@mcp.tool(name="list_recent")
def list_recent(limit: int = 20) -> str:
    """Most recent INBOX messages (brief: uid, from, subject, date), newest
    first. INBOX only. Call read(uid) for the full body."""
    capped = max(1, min(limit, 100))

    def run(conn: Any) -> list[dict]:
        uids = list(reversed(_search_uids(conn, "ALL")))[:capped]
        out = []
        for uid in uids:
            try:
                out.append(_brief(uid, _fetch_raw(conn, uid)))
            except mail_protocol.MailProtocolError:
                continue
        return out

    result, err = _imap_call(run)
    if err:
        return err
    return truncate(to_text(result))


@mcp.tool(name="search")
def search(query: str, limit: int = 20) -> str:
    """Best-effort INBOX search by subject/sender substring, newest first.
    Tries an IMAP server-side SEARCH first; for non-ASCII queries (CHARSET
    support is unreliable on some providers, e.g. QQ) or if that fails,
    falls back to scanning the most recent messages client-side. This is
    NOT a guaranteed full-text search — it only looks at Subject/From."""
    capped = max(1, min(limit, 100))
    ascii_query = query.isascii()

    def run(conn: Any) -> list[dict]:
        uids: list[str] = []
        if ascii_query:
            escaped = query.replace('"', '\\"')
            try:
                uids = list(
                    reversed(
                        _search_uids(conn, f'(OR SUBJECT "{escaped}" FROM "{escaped}")')
                    )
                )
            except Exception:
                uids = []
        if not uids:
            candidates = list(reversed(_search_uids(conn, "ALL")))[:_MAX_SCAN]
            needle = query.lower()
            matched: list[str] = []
            for uid in candidates:
                try:
                    raw = _fetch_raw(conn, uid)
                except mail_protocol.MailProtocolError:
                    continue
                parsed = mail_protocol.parse_message(uid, raw)
                if needle in parsed.subject.lower() or needle in parsed.from_.lower():
                    matched.append(uid)
                if len(matched) >= capped:
                    break
            uids = matched
        out = []
        for uid in uids[:capped]:
            try:
                out.append(_brief(uid, _fetch_raw(conn, uid)))
            except mail_protocol.MailProtocolError:
                continue
        return out

    result, err = _imap_call(run)
    if err:
        return err
    return truncate(to_text(result))


@mcp.tool(name="read")
def read(uid: str) -> str:
    """Full message: headers, plain-text body (HTML stripped; truncated
    with a marker if long), and attachment metadata (filename/type/size —
    call get_attachment(uid, attachment_index) to fetch one)."""

    def run(conn: Any) -> dict:
        parsed = mail_protocol.parse_message(uid, _fetch_raw(conn, uid))
        return {
            "uid": parsed.uid,
            "from": parsed.from_,
            "to": parsed.to,
            "cc": parsed.cc,
            "subject": parsed.subject,
            "date": parsed.date,
            "message_id": parsed.message_id,
            "body": parsed.text_body,
            "attachments": [
                {
                    "index": a.index,
                    "filename": a.filename,
                    "content_type": a.content_type,
                    "size": a.size,
                }
                for a in parsed.attachments
            ],
        }

    result, err = _imap_call(run)
    if err:
        return err
    return truncate(to_text(result))


@mcp.tool(name="get_attachment")
def get_attachment(uid: str, attachment_index: int) -> str:
    """Download one attachment (by the `index` from read()'s attachment
    list) into this session's working directory, under mail-attachments/.
    Returns the saved path."""

    def run(conn: Any) -> tuple[bytes, str, str]:
        return mail_protocol.extract_attachment(_fetch_raw(conn, uid), attachment_index)

    result, err = _imap_call(run)
    if err:
        return err
    content, filename, content_type = result

    session_id = _session_id()
    if not session_id or not ctx.api_base or not ctx.auth_token:
        return "Error: connector unavailable — missing session context."
    try:
        resp = httpx.post(
            f"{ctx.api_base}/api/sessions/{session_id}/files/save",
            json={
                "relative_dir": "mail-attachments",
                "filename": filename,
                "content_base64": base64.b64encode(content).decode("ascii"),
            },
            headers={"Authorization": f"Bearer {ctx.auth_token}"},
            timeout=_HTTP_TIMEOUT,
            trust_env=False,
        )
    except httpx.HTTPError as e:
        return f"Error: could not save attachment: {e}"
    if resp.status_code != 200:
        detail = resp.text[:200]
        return f"Error: could not save attachment (HTTP {resp.status_code}): {detail}"
    body = resp.json()
    return to_text({"saved_path": body["path"], "size": body["size"], "content_type": content_type})


def _read_workdir_file(path: str) -> tuple[bytes | None, str | None, str | None]:
    """Fetch raw bytes of a session-workdir-relative file via the host (the
    MCP subprocess has no direct FS access — mirrors bg.py's host-mediated
    model). Returns (content, filename, error)."""
    session_id = _session_id()
    if not session_id or not ctx.api_base or not ctx.auth_token:
        return None, None, "Error: connector unavailable — missing session context."
    try:
        resp = httpx.get(
            f"{ctx.api_base}/api/sessions/{session_id}/files/raw",
            params={"path": path},
            headers={"Authorization": f"Bearer {ctx.auth_token}"},
            timeout=_HTTP_TIMEOUT,
            trust_env=False,
        )
    except httpx.HTTPError as e:
        return None, None, f"Error: could not read attachment {path!r}: {e}"
    if resp.status_code != 200:
        return None, None, f"Error: could not read attachment {path!r} (HTTP {resp.status_code})"
    return resp.content, os.path.basename(path), None


def _send_message(
    creds: mail_protocol.MailCredentials, msg: EmailMessage, to: str, cc: str | None
) -> str:
    rcpts = [a.strip() for a in to.split(",") if a.strip()]
    if cc:
        rcpts += [a.strip() for a in cc.split(",") if a.strip()]
    try:
        smtp = mail_protocol.smtp_connect(creds)
    except mail_protocol.MailAuthError:
        ctx.mark_needs_reconnect("invalid_grant")
        return _RECONNECT_MSG
    except mail_protocol.MailConnectionError as e:
        return f"Error: {e}"
    try:
        smtp.send_message(msg, from_addr=creds.email, to_addrs=rcpts)
    except Exception as e:  # noqa: BLE001
        return f"Error: send failed: {e}"
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
    return to_text({"sent": True, "to": to, "cc": cc, "subject": str(msg["Subject"])})


@mcp.tool(name="send")
def send(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    attachment_paths: list[str] | None = None,
) -> str:
    """Send a plain-text email. `to`/`cc` are comma-separated addresses.
    `attachment_paths` are paths relative to this session's working
    directory. This WRITES (delivers mail) — show the user the recipient,
    subject, and body and get an explicit OK before calling this."""
    creds = _creds()
    if creds is None:
        return "Error: connector unavailable — reconnect this mailbox in Owlery."

    msg = EmailMessage()
    msg["From"] = creds.email
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)

    for path in attachment_paths or []:
        content, filename, err = _read_workdir_file(path)
        if err:
            return err
        ctype, _enc = mimetypes.guess_type(filename or "")
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(
            content, maintype=maintype, subtype=subtype or "octet-stream", filename=filename
        )

    return _send_message(creds, msg, to, cc)


def _reply_recipients(
    original: mail_protocol.ParsedMessage, self_email: str, reply_all: bool
) -> tuple[str, str | None]:
    """(to, cc) for a reply: `to` is always the original sender. With
    `reply_all`, `cc` adds everyone from the original To/Cc — minus the
    sender (already in `to`) and minus our own mailbox — deduped by
    address so a name/address pair isn't repeated across To and Cc."""
    to = original.from_
    if not reply_all:
        return to, None
    seen = {addr.lower() for _, addr in getaddresses([to]) if addr}
    seen.add(self_email.lower())
    cc_parts: list[str] = []
    for name, addr in getaddresses([original.to, original.cc]):
        if not addr or addr.lower() in seen:
            continue
        seen.add(addr.lower())
        cc_parts.append(formataddr((name, addr)) if name else addr)
    return to, ", ".join(cc_parts) if cc_parts else None


@mcp.tool(name="reply")
def reply(uid: str, body: str, reply_all: bool = False) -> str:
    """Reply to message `uid` with a plain-text body (threads via
    In-Reply-To/References). This WRITES (delivers mail) — show the user
    the reply text and get an explicit OK before calling this."""
    creds = _creds()
    if creds is None:
        return "Error: connector unavailable — reconnect this mailbox in Owlery."

    def run(conn: Any) -> mail_protocol.ParsedMessage:
        return mail_protocol.parse_message(uid, _fetch_raw(conn, uid))

    original, err = _imap_call(run)
    if err:
        return err

    to, cc = _reply_recipients(original, creds.email, reply_all)
    subject = original.subject
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    msg = EmailMessage()
    msg["From"] = creds.email
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    if original.message_id:
        msg["In-Reply-To"] = original.message_id
        refs = f"{original.in_reply_to} {original.message_id}".strip()
        msg["References"] = refs
    msg.set_content(body)

    return _send_message(creds, msg, to, cc)


if __name__ == "__main__":
    mcp.run()
