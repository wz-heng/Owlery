"""End-to-end tests for REST API using FastAPI TestClient."""

import subprocess
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import settings
from server.database import Database
from server.deploy import DeployLayout
from server.main import app
from server.session_manager import session_manager

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def client():
    # Initialize session_manager with in-memory DB before each test
    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    await session_manager.initialize(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await db.close()


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    # Local deploy disabled by default (no `deploy_root`) → fail-closed None,
    # not a 500 (docs/plans/local-deploy.md §6/§13.2).
    assert data["sha"] is None
    assert data["slot"] is None


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.mark.asyncio
async def test_health_reports_running_slot_sha(client, tmp_path, monkeypatch):
    """The switcher's `_fetch_health_sha` (server/switcher.py) polls `/health`
    and compares its `sha` against the handoff's `new_sha`/`old_sha` to confirm
    a flip (or rollback flip-back) actually took effect — so `sha`/`slot` must
    reflect whichever slot `current` resolves to right now, matching that
    slot's own git HEAD (the exact commit `deploy_stage` checked out there,
    §5 step 2)."""
    root = tmp_path / "deploy"
    root.mkdir()
    layout = DeployLayout.at(root)

    # A separate source repo (never itself a slot) at two commits — one per
    # slot, exactly like `deploy_stage`'s local-path fetch + detached checkout
    # of an exact sha (§5 steps 1-2). Building each slot from its own clone at
    # its own commit means a later commit in `src` never rewrites a slot that
    # already checked out an earlier one.
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q")
    _git(src, "config", "user.email", "a@b.c")
    _git(src, "config", "user.name", "T")
    (src / "f.txt").write_text("base\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "base")
    _git(src, "branch", "-M", "main")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(src),
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    _git(root, "clone", "-q", str(src), str(layout.slot_path("a")))

    (src / "f.txt").write_text("changed\n")
    _git(src, "commit", "-qam", "second")
    new_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(src),
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    _git(root, "clone", "-q", str(src), str(layout.slot_path("b")))

    layout.snapshots_path.mkdir(exist_ok=True)
    layout.journal_path.touch()
    layout.switch_current("a")

    monkeypatch.setattr(settings, "deploy_root", str(root))

    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["sha"] == head
    assert data["slot"] == "a"

    # A flip to 'b' (the switcher's atomic `current` move, §13.1) is visible on
    # the very next request — no caching, no stale DB row during probation.
    layout.switch_current("b")
    resp2 = await client.get("/health")
    data2 = resp2.json()
    assert data2["slot"] == "b"
    assert data2["sha"] == new_head


@pytest.mark.asyncio
async def test_auth_required(client):
    resp = await client.get("/api/sessions")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_auth_bad_token(client):
    resp = await client.get(
        "/api/sessions", headers={"Authorization": "Bearer wrong"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_sessions_empty(client):
    resp = await client.get("/api/sessions", headers=HEADERS)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_create_session(client):
    resp = await client.post(
        "/api/sessions",
        headers=HEADERS,
        json={"name": "Test Session", "working_dir": "/tmp"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test Session"
    assert data["working_dir"] == "/tmp"
    assert data["status"] == "idle"
    assert "id" in data


@pytest.mark.asyncio
async def test_get_session(client):
    # Create first
    create_resp = await client.post(
        "/api/sessions",
        headers=HEADERS,
        json={"name": "Get Me"},
    )
    sid = create_resp.json()["id"]

    # Get it
    resp = await client.get(f"/api/sessions/{sid}", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == sid
    assert data["name"] == "Get Me"
    assert "messages" in data


@pytest.mark.asyncio
async def test_get_session_not_found(client):
    resp = await client.get("/api/sessions/nonexistent", headers=HEADERS)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_session_surfaces_a_pending_park(client):
    """A usage-limit-parked session carries its pending park on the snapshot, so
    a reload/reconnect restores the "auto-resumes at HH:MM" banner instead of
    showing a paused session as ordinary idle (limit-auto-resume.md §4). A
    non-parked session reports null."""
    from datetime import datetime, timedelta, timezone

    from server.parked_turns import ParkedTurnRunner

    parked_id = (
        await client.post("/api/sessions", headers=HEADERS, json={"name": "Parked"})
    ).json()["id"]
    idle_id = (
        await client.post("/api/sessions", headers=HEADERS, json={"name": "Idle"})
    ).json()["id"]

    runner = ParkedTurnRunner(session_manager, session_manager.db)
    session_manager.set_parked_turn_runner(runner)
    try:
        await runner.park(
            parked_id,
            resume_mode="prompt",
            payload="p",
            resume_at_turn_start=None,
            limit_kind="five_hour",
            reset_at=datetime.now(timezone.utc) + timedelta(hours=2),
        )

        parked = (
            await client.get(f"/api/sessions/{parked_id}", headers=HEADERS)
        ).json()
        assert parked["pending_park"] is not None
        assert parked["pending_park"]["limit_kind"] == "five_hour"
        assert parked["pending_park"]["resume_at"]

        idle = (
            await client.get(f"/api/sessions/{idle_id}", headers=HEADERS)
        ).json()
        assert idle["pending_park"] is None
    finally:
        # Don't leak the runner onto the module-level singleton for later tests.
        session_manager.set_parked_turn_runner(None)


@pytest.mark.asyncio
async def test_delete_session(client):
    create_resp = await client.post(
        "/api/sessions",
        headers=HEADERS,
        json={"name": "Delete Me"},
    )
    sid = create_resp.json()["id"]

    resp = await client.delete(f"/api/sessions/{sid}", headers=HEADERS)
    assert resp.status_code == 204

    # Verify gone
    resp = await client.get(f"/api/sessions/{sid}", headers=HEADERS)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_session_not_found(client):
    resp = await client.delete("/api/sessions/nonexistent", headers=HEADERS)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_archive_session(client):
    """POST /api/sessions/{id}/archive returns a fresh SessionInfo with the
    same name/working_dir, the old session disappears from the list,
    and the new id is different."""
    create_resp = await client.post(
        "/api/sessions",
        headers=HEADERS,
        json={"name": "Archive Me", "working_dir": "/tmp/archived"},
    )
    old_id = create_resp.json()["id"]

    arc = await client.post(
        f"/api/sessions/{old_id}/archive", headers=HEADERS
    )
    assert arc.status_code == 201
    body = arc.json()
    new_id = body["id"]
    assert new_id != old_id
    assert body["name"] == "Archive Me"
    assert body["working_dir"] == "/tmp/archived"

    # Old session is hidden from the list; new one appears.
    list_resp = await client.get("/api/sessions", headers=HEADERS)
    ids = [s["id"] for s in list_resp.json()]
    assert old_id not in ids
    assert new_id in ids

    # GET on the old id still works — it returns the archived row's
    # detail (so the UI's "view archived" can read history).
    archived = await client.get(f"/api/sessions/{old_id}", headers=HEADERS)
    assert archived.status_code == 200
    assert archived.json()["archived"] is True

    # GET on the list with ?include_archived=true surfaces both.
    inc = await client.get(
        "/api/sessions?include_archived=true", headers=HEADERS
    )
    ids = [s["id"] for s in inc.json()]
    assert old_id in ids
    assert new_id in ids

    # Unarchive brings the old id back; it returns to the default list.
    un = await client.post(
        f"/api/sessions/{old_id}/unarchive", headers=HEADERS
    )
    assert un.status_code == 200
    assert un.json()["id"] == old_id
    assert un.json()["archived"] is False
    list_after = await client.get("/api/sessions", headers=HEADERS)
    assert old_id in [s["id"] for s in list_after.json()]


@pytest.mark.asyncio
async def test_archive_session_not_found(client):
    resp = await client.post(
        "/api/sessions/nonexistent/archive", headers=HEADERS
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_session_defaults_backend_to_claude_code(client):
    resp = await client.post(
        "/api/sessions", headers=HEADERS, json={"name": "Default Backend"}
    )
    assert resp.status_code == 201
    assert resp.json()["backend"] == "claude-code"


@pytest.mark.asyncio
async def test_create_session_with_codex_backend(client):
    resp = await client.post(
        "/api/sessions", headers=HEADERS, json={"name": "Cx", "backend": "codex"}
    )
    assert resp.status_code == 201
    assert resp.json()["backend"] == "codex"


@pytest.mark.asyncio
async def test_create_session_rejects_credential_backend_mismatch(client):
    """A Codex session must not run a claude-code credential (codex-backend.md
    §4.2) — the route returns 400."""
    from datetime import datetime, timezone
    from server.config import settings
    from server.crypto import encrypt

    enc = encrypt("sk-x", settings.auth_token)
    await session_manager.db.save_credential(
        credential_id="c-cc",
        backend="claude-code",
        label="L",
        auth_type="api_key",
        secret_encrypted=enc,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    resp = await client.post(
        "/api/sessions",
        headers=HEADERS,
        json={"name": "Bad", "backend": "codex", "credential_id": "c-cc"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_backends_includes_claude_code(client):
    resp = await client.get("/api/backends", headers=HEADERS)
    assert resp.status_code == 200
    assert "claude-code" in resp.json()["available"]
