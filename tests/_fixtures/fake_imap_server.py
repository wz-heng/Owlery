"""Scripted fake IMAP server for `mail` connector tests (mail-connector.md
§4.5 — "IMAP 用脚本化 fake"). Real TLS via asyncio's `ssl=` listener param;
just enough RFC 3501 surface to drive genuine `imaplib` traffic:
CAPABILITY, LOGIN, the RFC 2971 `ID` extension, SELECT, UID SEARCH (`ALL`
and the one `(OR SUBJECT "x" FROM "x")` shape the connector sends), UID
FETCH (RFC822), LOGOUT.

Not a general-purpose IMAP server. `require_id_before_select=True` (the
default) reproduces QQ/163's "Unsafe Login" rejection of SELECT before ID
— every test that round-trips through `server.mail_protocol.imap_connect`
against this fixture is implicitly asserting the ID workaround fires.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import ssl
from dataclasses import dataclass, field


def _headers_text(raw: bytes) -> str:
    head = raw.split(b"\r\n\r\n", 1)[0]
    return head.decode("utf-8", "replace").lower()


@dataclass
class FakeImapServer:
    email: str
    auth_code: str
    messages: list[bytes] = field(default_factory=list)
    ssl_context: ssl.SSLContext | None = None
    require_id_before_select: bool = True

    host: str = "127.0.0.1"
    port: int = 0
    _server: asyncio.Server | None = field(default=None, init=False, repr=False)

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self.host, self.port, ssl=self.ssl_context
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"* OK IMAP4rev1 fake-mail ready\r\n")
        await writer.drain()
        logged_in = False
        id_seen = False
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                if not text:
                    continue
                parts = text.split(" ", 2)
                tag = parts[0]
                cmd = parts[1].upper() if len(parts) > 1 else ""
                rest = parts[2] if len(parts) > 2 else ""

                if cmd == "CAPABILITY":
                    writer.write(b"* CAPABILITY IMAP4rev1 ID\r\n")
                    writer.write(f"{tag} OK CAPABILITY completed\r\n".encode())
                elif cmd == "LOGIN":
                    try:
                        user, code = shlex.split(rest)[:2]
                    except ValueError:
                        writer.write(f"{tag} BAD malformed LOGIN\r\n".encode())
                    else:
                        if user == self.email and code == self.auth_code:
                            logged_in = True
                            writer.write(f"{tag} OK LOGIN completed\r\n".encode())
                        else:
                            writer.write(
                                f"{tag} NO [AUTHENTICATIONFAILED] invalid credentials\r\n".encode()
                            )
                elif cmd == "ID":
                    id_seen = True
                    writer.write(b"* ID NIL\r\n")
                    writer.write(f"{tag} OK ID completed\r\n".encode())
                elif cmd in ("SELECT", "EXAMINE"):
                    if not logged_in:
                        writer.write(f"{tag} NO login first\r\n".encode())
                    elif self.require_id_before_select and not id_seen:
                        writer.write(f"{tag} NO [UNSAFE-LOGIN] Unsafe Login\r\n".encode())
                    else:
                        writer.write(f"* {len(self.messages)} EXISTS\r\n".encode())
                        writer.write(b"* 0 RECENT\r\n")
                        writer.write(f"{tag} OK [READ-ONLY] {cmd} completed\r\n".encode())
                elif cmd == "UID":
                    await self._handle_uid(tag, rest, writer, logged_in)
                elif cmd == "LOGOUT":
                    writer.write(b"* BYE logging out\r\n")
                    writer.write(f"{tag} OK LOGOUT completed\r\n".encode())
                    await writer.drain()
                    break
                else:
                    writer.write(f"{tag} BAD unknown command\r\n".encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()

    async def _handle_uid(
        self, tag: str, rest: str, writer: asyncio.StreamWriter, logged_in: bool
    ) -> None:
        if not logged_in:
            writer.write(f"{tag} NO login first\r\n".encode())
            return
        sub_parts = rest.split(" ", 1)
        subcmd = sub_parts[0].upper()
        subrest = sub_parts[1] if len(sub_parts) > 1 else ""
        if subcmd == "SEARCH":
            uids = self._search(subrest)
            writer.write(("* SEARCH " + " ".join(str(u) for u in uids) + "\r\n").encode())
            writer.write(f"{tag} OK UID SEARCH completed\r\n".encode())
        elif subcmd == "FETCH":
            m = re.match(r"(\d+)\s+\((.*)\)", subrest)
            if not m:
                writer.write(f"{tag} BAD malformed FETCH\r\n".encode())
                return
            uid = int(m.group(1))
            idx = uid - 1
            if idx < 0 or idx >= len(self.messages):
                writer.write(f"{tag} OK UID FETCH completed\r\n".encode())
                return
            raw = self.messages[idx]
            writer.write(f"* {uid} FETCH (UID {uid} RFC822 {{{len(raw)}}}\r\n".encode())
            writer.write(raw)
            writer.write(b")\r\n")
            writer.write(f"{tag} OK UID FETCH completed\r\n".encode())
        else:
            writer.write(f"{tag} BAD unknown UID subcommand\r\n".encode())

    def _search(self, criteria: str) -> list[int]:
        criteria = criteria.strip()
        if criteria.upper() == "ALL":
            return list(range(1, len(self.messages) + 1))
        m = re.match(r'\(OR SUBJECT "(.*)" FROM "(.*)"\)', criteria)
        if m:
            subject_q, from_q = m.group(1).lower(), m.group(2).lower()
            out = []
            for i, raw in enumerate(self.messages, start=1):
                headers = _headers_text(raw)
                if subject_q in headers or from_q in headers:
                    out.append(i)
            return out
        return []
