"""ConnectorManager's static-credential install path (mail-connector.md
§4.1): field validation, live-verify-then-persist, the upsert-on-account
dedup shared with OAuth installs, and the `auth_type='api_key'` branch of
`get_access_token` (no OAuthTokenSet envelope, no refresh)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from server.connector_manager import ConnectorError, ConnectorManager
from server.connectors.base import ConnectorBase, StaticCredentialField, StaticVerifyError
from server.connectors.registry import KIND_REGISTRY, register
from server.crypto import decrypt
from server.database import Database


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
        self.verify_calls: list[dict] = []
        self.should_fail = False
        self.identity = None  # None -> (fields["email"], fields["email"])

    async def verify_static_credentials(self, fields):
        self.verify_calls.append(dict(fields))
        if self.should_fail:
            raise StaticVerifyError("bad credentials, try again")
        if self.identity is not None:
            return self.identity
        return fields["email"], fields["email"]


@pytest.fixture
def clean_registry():
    saved = dict(KIND_REGISTRY)
    KIND_REGISTRY.clear()
    yield
    KIND_REGISTRY.clear()
    KIND_REGISTRY.update(saved)


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
def connector():
    return FakeStaticConnector()


@pytest.fixture
async def mgr(db, connector, clean_registry):
    register(connector)
    return ConnectorManager(db)


# --- catalog -----------------------------------------------------------


@pytest.mark.asyncio
async def test_static_kind_always_available_no_oauth_client(mgr):
    """Static connectors skip the OAuth-client setup step entirely."""
    cat = {c["kind"]: c for c in await mgr.catalog()}
    assert cat["fakestatic"]["available"] is True
    assert cat["fakestatic"]["auth_mode"] == "static"
    assert {f["key"] for f in cat["fakestatic"]["static_fields"]} == {"email", "auth_code"}


# --- install -------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_static_install_creates_and_encrypts(mgr, db, connector):
    inst = await mgr.complete_static_install(
        kind="fakestatic", fields={"email": "me@x.com", "auth_code": "abc"}
    )
    assert inst["kind"] == "fakestatic"
    assert inst["label"] == "me@x.com"
    assert inst["external_account_id"] == "me@x.com"
    assert inst["auth_type"] == "api_key"
    assert connector.verify_calls == [{"email": "me@x.com", "auth_code": "abc"}]

    blob = await db.get_connector_secret(inst["id"])
    fields = json.loads(decrypt(blob, "changeme"))
    assert fields == {"email": "me@x.com", "auth_code": "abc"}


@pytest.mark.asyncio
async def test_complete_static_install_strips_whitespace(mgr):
    inst = await mgr.complete_static_install(
        kind="fakestatic", fields={"email": "  me@x.com  ", "auth_code": " abc "}
    )
    assert inst["label"] == "me@x.com"


@pytest.mark.asyncio
async def test_complete_static_install_requires_all_fields(mgr):
    with pytest.raises(ConnectorError, match="Auth code"):
        await mgr.complete_static_install(kind="fakestatic", fields={"email": "me@x.com"})
    with pytest.raises(ConnectorError, match="Email"):
        await mgr.complete_static_install(
            kind="fakestatic", fields={"email": "  ", "auth_code": "abc"}
        )


@pytest.mark.asyncio
async def test_complete_static_install_verify_failure_does_not_persist(mgr, db, connector):
    connector.should_fail = True
    with pytest.raises(ConnectorError, match="bad credentials"):
        await mgr.complete_static_install(
            kind="fakestatic", fields={"email": "me@x.com", "auth_code": "abc"}
        )
    assert await db.load_connector_installations() == []


@pytest.mark.asyncio
async def test_complete_static_install_upserts_same_account(mgr):
    a = await mgr.complete_static_install(
        kind="fakestatic", fields={"email": "me@x.com", "auth_code": "abc"}
    )
    b = await mgr.complete_static_install(
        kind="fakestatic", fields={"email": "me@x.com", "auth_code": "new-code"}
    )
    assert a["id"] == b["id"]


@pytest.mark.asyncio
async def test_complete_static_install_survives_concurrent_insert_race(mgr, db, connector, monkeypatch):
    """Two installs of the same account racing past the "not found" check
    both try to INSERT; the loser's row.save trips the partial unique index
    on (kind, external_account_id) as a `sqlite3.IntegrityError` — the
    retry path must catch that and converge onto the winner's row instead
    of leaking a raw 500."""
    real_save = db.save_connector_installation
    calls = {"n": 0}

    async def flaky_save(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate a concurrent request that already inserted the
            # winning row for this account just before we get here.
            await real_save(
                installation_id="winner-id",
                kind=kwargs["kind"],
                label="other-tab@x.com",
                auth_type=kwargs["auth_type"],
                secret_encrypted=kwargs["secret_encrypted"],
                created_at=kwargs["created_at"],
                external_account_id=kwargs["external_account_id"],
                scopes=kwargs["scopes"],
                token_expires_at=kwargs["token_expires_at"],
            )
            raise sqlite3.IntegrityError("UNIQUE constraint failed")
        await real_save(**kwargs)

    monkeypatch.setattr(db, "save_connector_installation", flaky_save)

    inst = await mgr.complete_static_install(
        kind="fakestatic", fields={"email": "me@x.com", "auth_code": "abc"}
    )
    assert inst["id"] == "winner-id"
    assert inst["label"] == "me@x.com"
    rows = await db.load_connector_installations()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_complete_static_install_unknown_kind(mgr):
    with pytest.raises(ConnectorError, match="unknown"):
        await mgr.complete_static_install(kind="ghost", fields={})


@pytest.mark.asyncio
async def test_complete_static_install_rejects_oauth_kind(db, clean_registry):
    class OAuthy(ConnectorBase):
        kind = "oauthy"
        display_name = "Oauthy"

        class _P:
            default_scopes = []

        oauth = _P()

    register(OAuthy())
    mgr = ConnectorManager(db)
    with pytest.raises(ConnectorError, match="does not use static credentials"):
        await mgr.complete_static_install(kind="oauthy", fields={})


# --- token access --------------------------------------------------------


@pytest.mark.asyncio
async def test_get_access_token_returns_raw_field_json_no_refresh(mgr):
    inst = await mgr.complete_static_install(
        kind="fakestatic", fields={"email": "me@x.com", "auth_code": "abc"}
    )
    out = await mgr.get_access_token(inst["id"])
    assert out["expires_at_epoch"] == 0.0
    assert json.loads(out["access_token"]) == {"email": "me@x.com", "auth_code": "abc"}
