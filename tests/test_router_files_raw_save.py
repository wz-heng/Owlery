"""`/api/sessions/{id}/files/raw` + `/files/save` (mail-connector.md §4.2):
the internal read/write pair an MCP server uses to move attachment bytes
through the session's working directory — no direct filesystem access from
the MCP subprocess, same host-mediated model as `bg.py`. Traversal safety
is exercised via `file_viewer.resolve_new_write_path` /
`resolve_raw_read_path`; this file covers the HTTP contract on top."""

from __future__ import annotations

import base64

import pytest
from httpx import ASGITransport, AsyncClient

from server.database import Database
from server.main import app
from server.session_manager import session_manager

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def client(tmp_path):
    db = Database(":memory:")
    await db.initialize()
    await db.save_session(
        session_id="s-1",
        name="t",
        working_dir=str(tmp_path),
        created_at="2026-05-18T00:00:00Z",
    )
    session_manager.sessions.clear()
    # `initialize` loads existing (non-archived) sessions into the live
    # in-memory dict — must run AFTER the row exists, or `_working_dir_for`
    # finds it in neither the live dict nor the archived-only DB fallback.
    await session_manager.initialize(db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.working_dir = tmp_path  # type: ignore[attr-defined]
        yield c
    await db.close()


@pytest.mark.asyncio
async def test_requires_auth(client):
    assert (await client.get("/api/sessions/s-1/files/raw", params={"path": "a.txt"})).status_code in (
        401,
        403,
    )
    assert (
        await client.post(
            "/api/sessions/s-1/files/save",
            json={"filename": "a.txt", "content_base64": "aGk="},
        )
    ).status_code in (401, 403)


@pytest.mark.asyncio
async def test_save_then_read_round_trips(client):
    r = await client.post(
        "/api/sessions/s-1/files/save",
        json={
            "relative_dir": "mail-attachments",
            "filename": "notes.txt",
            "content_base64": base64.b64encode(b"hello attachment").decode(),
        },
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "mail-attachments/notes.txt"
    assert body["size"] == len(b"hello attachment")
    assert (client.working_dir / "mail-attachments" / "notes.txt").read_bytes() == b"hello attachment"

    r2 = await client.get(
        "/api/sessions/s-1/files/raw", params={"path": "mail-attachments/notes.txt"}, headers=HEADERS
    )
    assert r2.status_code == 200
    assert r2.content == b"hello attachment"


@pytest.mark.asyncio
async def test_save_dedupes_instead_of_overwriting(client):
    for _ in range(3):
        r = await client.post(
            "/api/sessions/s-1/files/save",
            json={"filename": "dup.txt", "content_base64": base64.b64encode(b"x").decode()},
            headers=HEADERS,
        )
        assert r.status_code == 200
    paths = {p.name for p in client.working_dir.iterdir()}
    assert paths == {"dup.txt", "dup-1.txt", "dup-2.txt"}


@pytest.mark.asyncio
async def test_save_rejects_arbitrary_types_no_extension_gate(client):
    """Unlike /files (the viewer), /files/save has no extension allowlist —
    attachments can be any type."""
    r = await client.post(
        "/api/sessions/s-1/files/save",
        json={"filename": "archive.zip", "content_base64": base64.b64encode(b"PK\x03\x04").decode()},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_save_sanitizes_filename_to_basename(client):
    r = await client.post(
        "/api/sessions/s-1/files/save",
        json={"filename": "../../evil.txt", "content_base64": base64.b64encode(b"x").decode()},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json()["path"] == "evil.txt"
    # Confirm it landed inside working_dir, not two levels up.
    assert (client.working_dir / "evil.txt").exists()


@pytest.mark.asyncio
async def test_save_rejects_relative_dir_traversal(client):
    r = await client.post(
        "/api/sessions/s-1/files/save",
        json={
            "relative_dir": "../../etc",
            "filename": "passwd.txt",
            "content_base64": base64.b64encode(b"x").decode(),
        },
        headers=HEADERS,
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_save_rejects_bad_base64(client):
    r = await client.post(
        "/api/sessions/s-1/files/save",
        json={"filename": "a.txt", "content_base64": "not-valid-base64!!"},
        headers=HEADERS,
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_read_raw_rejects_traversal(client):
    r = await client.get(
        "/api/sessions/s-1/files/raw", params={"path": "../outside.txt"}, headers=HEADERS
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_read_raw_missing_file_404(client):
    r = await client.get(
        "/api/sessions/s-1/files/raw", params={"path": "nope.txt"}, headers=HEADERS
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_read_raw_unknown_session_404(client):
    r = await client.get(
        "/api/sessions/ghost/files/raw", params={"path": "a.txt"}, headers=HEADERS
    )
    assert r.status_code == 404
