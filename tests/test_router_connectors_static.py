"""`/api/connectors/{kind}/install-static` (mail-connector.md §4.1): the
non-OAuth install route — verify live, persist on success, surface the
service's own error on failure, and the oauth/start guard that rejects a
static-mode kind."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from server.connector_manager import ConnectorManager
from server.connectors.base import ConnectorBase, StaticCredentialField, StaticVerifyError
from server.connectors.registry import KIND_REGISTRY, register
from server.database import Database
from server.main import app
from server.routers import connectors as connectors_mod
from server.session_manager import session_manager

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class FakeStaticConnector(ConnectorBase):
    kind = "fakestatic"
    display_name = "FakeStatic"
    category = "test"
    allows_multiple = True
    auth_mode = "static"
    static_fields = (
        StaticCredentialField(key="email", label="Email"),
        StaticCredentialField(key="auth_code", label="Auth code", secret=True),
    )

    def __init__(self):
        self.should_fail = False

    async def verify_static_credentials(self, fields):
        if self.should_fail:
            raise StaticVerifyError("IMAP login failed: bad authorization code")
        return fields["email"], fields["email"]


@pytest.fixture
async def client():
    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    await session_manager.initialize(db)
    connectors_mod.set_manager(ConnectorManager(db))

    saved = dict(KIND_REGISTRY)
    KIND_REGISTRY.clear()
    connector = FakeStaticConnector()
    register(connector)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.connector = connector  # type: ignore[attr-defined]
        yield c

    KIND_REGISTRY.clear()
    KIND_REGISTRY.update(saved)
    await db.close()


@pytest.mark.asyncio
async def test_install_static_success(client):
    r = await client.post(
        "/api/connectors/fakestatic/install-static",
        json={"fields": {"email": "me@x.com", "auth_code": "abc"}},
        headers=HEADERS,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "fakestatic"
    assert body["label"] == "me@x.com"
    assert body["auth_type"] == "api_key"

    listed = (await client.get("/api/connectors", headers=HEADERS)).json()
    assert len(listed) == 1 and listed[0]["id"] == body["id"]


@pytest.mark.asyncio
async def test_install_static_verify_failure_surfaces_service_message(client):
    client.connector.should_fail = True  # type: ignore[attr-defined]
    r = await client.post(
        "/api/connectors/fakestatic/install-static",
        json={"fields": {"email": "me@x.com", "auth_code": "wrong"}},
        headers=HEADERS,
    )
    assert r.status_code == 400
    assert "IMAP login failed" in r.json()["detail"]
    assert (await client.get("/api/connectors", headers=HEADERS)).json() == []


@pytest.mark.asyncio
async def test_install_static_missing_field(client):
    r = await client.post(
        "/api/connectors/fakestatic/install-static",
        json={"fields": {"email": "me@x.com"}},
        headers=HEADERS,
    )
    assert r.status_code == 400
    assert "Auth code" in r.json()["detail"]


@pytest.mark.asyncio
async def test_install_static_unknown_kind(client):
    r = await client.post(
        "/api/connectors/ghost/install-static",
        json={"fields": {}},
        headers=HEADERS,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_install_static_requires_auth(client):
    r = await client.post(
        "/api/connectors/fakestatic/install-static", json={"fields": {}}
    )
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_oauth_start_rejects_static_kind(client):
    r = await client.post(
        "/api/connectors/oauth/start", json={"kind": "fakestatic"}, headers=HEADERS
    )
    assert r.status_code == 400
    assert "install-static" in r.json()["detail"]


@pytest.mark.asyncio
async def test_catalog_marks_static_available_with_form_schema(client):
    cat = {
        c["kind"]: c
        for c in (await client.get("/api/connectors/catalog", headers=HEADERS)).json()
    }
    entry = cat["fakestatic"]
    assert entry["available"] is True
    assert entry["auth_mode"] == "static"
    assert {f["key"] for f in entry["static_fields"]} == {"email", "auth_code"}
