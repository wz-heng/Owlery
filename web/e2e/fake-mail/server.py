#!/usr/bin/env python3
"""Standalone fake IMAP/SMTP server for the mail-connector e2e spec
(mail-connector.md §4.5 — "表单填入指向 fake 服务器的自定义预设").

Run as a Playwright `webServer` process (playwright.config.ts). Listens on
fixed ports so the spec's "Custom" preset form and this script agree on
host/port with no IPC. The cert is generated once by playwright.config.ts
(before any webServer starts) and passed in as argv — the SAME file the
backend trusts via OWLERY_MAIL_CA_FILE (server/mail_protocol.py).

Usage: server.py <certfile> <keyfile>
"""

from __future__ import annotations

import asyncio
import sys
from email.message import EmailMessage
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests._fixtures.fake_imap_server import FakeImapServer  # noqa: E402
from tests._fixtures.fake_smtp_server import FakeSmtpServer  # noqa: E402
from tests._fixtures.mail_tls import FakeMailTLS  # noqa: E402

EMAIL = "e2e@example.com"
AUTH_CODE = "e2e-auth-code"
IMAP_PORT = 19993
SMTP_PORT = 19465


def _seed_message() -> bytes:
    msg = EmailMessage()
    msg["From"] = "sender@example.com"
    msg["To"] = EMAIL
    msg["Subject"] = "Welcome to the fake mailbox"
    msg["Date"] = "Mon, 1 Jan 2026 00:00:00 +0000"
    msg.set_content("This is a seeded e2e test message.")
    return msg.as_bytes()


async def main() -> None:
    if len(sys.argv) != 3:
        print("usage: server.py <certfile> <keyfile>", file=sys.stderr)
        sys.exit(2)
    tls = FakeMailTLS(certfile=Path(sys.argv[1]), keyfile=Path(sys.argv[2]))

    smtp = FakeSmtpServer(
        email=EMAIL,
        auth_code=AUTH_CODE,
        starttls=False,
        ssl_context=tls.server_context(),
        port=SMTP_PORT,
    )
    smtp.start()

    imap = FakeImapServer(
        email=EMAIL,
        auth_code=AUTH_CODE,
        messages=[_seed_message()],
        ssl_context=tls.server_context(),
        port=IMAP_PORT,
    )
    await imap.start()

    # Playwright's webServer readiness probe polls this port.
    print(f"ready imap={imap.port} smtp={smtp.port}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
