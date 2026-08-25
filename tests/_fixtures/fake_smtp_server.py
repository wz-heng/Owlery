"""Fake SMTP server for `mail` connector tests (mail-connector.md §4.5) —
aiosmtpd-based, real AUTH + TLS (implicit) / STARTTLS against the throwaway
cert from `mail_tls.py`. Records every accepted message for assertions.
"""

from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass, field

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword


def _free_port() -> int:
    """aiosmtpd's Controller (unlike asyncio.start_server) doesn't resolve
    `port=0` to the OS-assigned ephemeral port before its own post-start
    readiness probe reconnects to `self.port` — so we have to pick a free
    port ourselves. Small TOCTOU race in theory; fine for a local test."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class _RecordingHandler:
    sent: list[dict] = field(default_factory=list)

    async def handle_DATA(self, server, session, envelope):
        self.sent.append(
            {
                "mail_from": envelope.mail_from,
                "rcpt_tos": list(envelope.rcpt_tos),
                "content": envelope.content,
            }
        )
        return "250 OK"


class FakeSmtpServer:
    def __init__(
        self,
        *,
        email: str,
        auth_code: str,
        starttls: bool,
        ssl_context: ssl.SSLContext,
        port: int | None = None,
    ) -> None:
        self.email = email
        self.auth_code = auth_code
        self.handler = _RecordingHandler()
        kwargs: dict = dict(
            hostname="127.0.0.1",
            port=port if port is not None else _free_port(),
            auth_required=True,
            authenticator=self._authenticate,
        )
        if starttls:
            kwargs["require_starttls"] = True
            kwargs["tls_context"] = ssl_context
        else:
            kwargs["ssl_context"] = ssl_context
            # aiosmtpd's `_tls_protocol` (the flag `auth_require_tls` gates
            # on) is only ever set by its own STARTTLS upgrade handler — an
            # implicit-TLS connection (whole socket wrapped at accept time,
            # our `ssl_context=` above) never sets it, so AUTH would never
            # be advertised even though the transport genuinely is TLS.
            # Known aiosmtpd quirk for implicit TLS; harmless to relax here
            # since the channel actually is encrypted.
            kwargs["auth_require_tls"] = False
        self.controller = Controller(self.handler, **kwargs)

    def _authenticate(self, server, session, envelope, mechanism, auth_data) -> AuthResult:
        if not isinstance(auth_data, LoginPassword):
            return AuthResult(success=False, handled=False)
        ok = (
            auth_data.login.decode() == self.email
            and auth_data.password.decode() == self.auth_code
        )
        return AuthResult(success=ok)

    def start(self) -> None:
        self.controller.start()

    def stop(self) -> None:
        self.controller.stop()

    @property
    def port(self) -> int:
        return self.controller.port

    @property
    def sent(self) -> list[dict]:
        return self.handler.sent
