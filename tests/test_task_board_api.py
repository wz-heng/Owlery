"""Focused contract tests for the Task Board REST router."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.routers import task_boards as routes
from server.database import Database
from server.task_board.models import TaskConflictError
from server.task_board.repository import TaskRepository

HEADERS = {"Authorization": "Bearer changeme"}


@dataclass
class _Record:
    id: str
    updated_at: str = "v1"
    status: str = "triage"

    def to_dict(self):
        return vars(self)


class _Manager:
    def __init__(self):
        self.calls = []

    async def wake_dispatcher(self):
        self.calls.append(("wake",))

    async def publish_task_update(self, task_id):
        self.calls.append(("publish", task_id))

    async def publish_board_update(self, board_id):
        self.calls.append(("publish_board", board_id))

    async def publish_board_updates(self, board_id):
        self.calls.append(("publish_board_tasks", board_id))

    async def complete_worker(self, *identity, **payload):
        self.calls.append(("complete", identity, payload))
        return {"state": "completed"}

    async def create_worker_task(self, *identity, **payload):
        self.calls.append(("worker_create", identity, payload))
        return {"id": "child-1", "status": payload["status"]}


@pytest.fixture
async def client(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    manager = _Manager()
    routes.set_manager(manager)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, manager


@pytest.mark.asyncio
async def test_create_task_reports_idempotency_winner(client, monkeypatch):
    c, _manager = client

    class Repo:
        async def create_task_result(self, **kwargs):
            assert kwargs["dependencies"] == ["dep-1"]
            assert kwargs["created_by_kind"] == "user"
            return _Record("task-1"), True

    monkeypatch.setattr(routes, "task_repository", Repo())
    response = await c.post(
        "/api/task-boards/board-1/tasks",
        headers=HEADERS,
        json={
            "title": "Ship it",
            "specified": True,
            "dependencies": ["dep-1"],
            "idempotency_key": "request-1",
        },
    )
    assert response.status_code == 201
    assert response.headers["location"] == "/api/tasks/task-1"


@pytest.mark.asyncio
async def test_create_task_replay_is_200(client, monkeypatch):
    c, _manager = client

    class Repo:
        async def create_task_result(self, **kwargs):
            return _Record("task-existing"), False

    monkeypatch.setattr(routes, "task_repository", Repo())
    response = await c.post(
        "/api/task-boards/board-1/tasks",
        headers=HEADERS,
        json={"title": "Ship it", "idempotency_key": "same"},
    )
    assert response.status_code == 200
    assert response.json()["id"] == "task-existing"


@pytest.mark.asyncio
async def test_patch_task_uses_repository_cas(client, monkeypatch):
    c, _manager = client
    seen = {}

    class Repo:
        async def get_task(self, task_id):
            return _Record(task_id, updated_at="stamp-1")

        async def update_task(self, task_id, **kwargs):
            seen.update(kwargs)
            return _Record(task_id, updated_at="stamp-2")

    monkeypatch.setattr(routes, "task_repository", Repo())
    response = await c.patch(
        "/api/tasks/task-1",
        headers={**HEADERS, "If-Match": '"stamp-1"'},
        json={"title": "New title", "parent_id": "parent-1"},
    )
    assert response.status_code == 200
    assert response.headers["etag"] == '"stamp-2"'
    assert seen == {
        "expected_updated_at": "stamp-1",
        "title": "New title",
        "parent_task_id": "parent-1",
    }


@pytest.mark.asyncio
async def test_patch_task_rejects_stale_drawer(client, monkeypatch):
    c, _manager = client

    class Repo:
        async def get_task(self, task_id):
            return _Record(task_id, updated_at="newer")

    monkeypatch.setattr(routes, "task_repository", Repo())
    response = await c.patch(
        "/api/tasks/task-1",
        headers={**HEADERS, "If-Match": '"old"'},
        json={"title": "Lost edit"},
    )
    assert response.status_code == 412
    assert response.json()["detail"]["current"]["updated_at"] == "newer"


@pytest.mark.asyncio
async def test_repository_conflict_is_structured_409(client, monkeypatch):
    c, _manager = client
    current = _Record("task-1", status="ready")

    class Repo:
        async def add_dependency(self, *args, **kwargs):
            raise TaskConflictError("dependency would create a cycle", current=current)

    monkeypatch.setattr(routes, "task_repository", Repo())
    response = await c.post(
        "/api/tasks/task-1/dependencies",
        headers=HEADERS,
        json={"depends_on_task_id": "task-2"},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "conflict"
    assert detail["current"]["id"] == "task-1"


@pytest.mark.asyncio
async def test_worker_complete_uses_only_trusted_headers(client):
    c, manager = client
    response = await c.post(
        "/api/task-worker/current/complete",
        headers={
            **HEADERS,
            "X-Owlery-Session-ID": "session-1",
            "X-Owlery-Task-ID": "task-1",
            "X-Owlery-Task-Run-ID": "run-1",
        },
        json={"summary": "Done", "metadata": {"tests": "green"}},
    )
    assert response.status_code == 200
    assert response.json() == {"state": "completed"}
    assert manager.calls == [
        (
            "complete",
            ("task-1", "run-1", "session-1"),
            {"summary": "Done", "metadata": {"tests": "green"}, "artifacts": []},
        )
    ]


@pytest.mark.asyncio
async def test_worker_callback_requires_all_identity_headers(client):
    c, manager = client
    response = await c.post(
        "/api/task-worker/current/complete",
        headers={**HEADERS, "X-Owlery-Task-ID": "task-1"},
        json={"summary": "Done"},
    )
    assert response.status_code == 422
    assert manager.calls == []


@pytest.mark.asyncio
async def test_worker_create_preserves_unspecified_triage_state(client):
    c, manager = client
    response = await c.post(
        "/api/task-worker/current/tasks",
        headers={
            **HEADERS,
            "X-Owlery-Session-ID": "session-1",
            "X-Owlery-Task-ID": "task-1",
            "X-Owlery-Task-Run-ID": "run-1",
        },
        json={"title": "Investigate later"},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "triage"
    operation, identity, payload = manager.calls[0]
    assert operation == "worker_create"
    assert identity == ("task-1", "run-1", "session-1")
    assert payload["status"] == "triage"
    assert "workspace_mode" not in payload
    assert "working_dir_override" not in payload


@pytest.mark.asyncio
async def test_real_repository_board_task_roundtrip(client, monkeypatch, tmp_path):
    """Exercise the router against the real second SQLite connection."""
    c, manager = client
    db_path = tmp_path / "task-api.db"
    database = Database(str(db_path))
    await database.initialize()
    repository = TaskRepository(str(db_path))
    await repository.initialize()
    monkeypatch.setattr(routes, "task_repository", repository)
    try:
        board = await c.post(
            "/api/task-boards",
            headers=HEADERS,
            json={"name": "Release", "working_dir": str(tmp_path)},
        )
        assert board.status_code == 201, board.text
        board_id = board.json()["id"]

        created = await c.post(
            f"/api/task-boards/{board_id}/tasks",
            headers=HEADERS,
            json={"title": "Ship", "body": "Do the work"},
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        assert created.json()["status"] == "triage"

        detail = await c.get(f"/api/tasks/{task_id}", headers=HEADERS)
        assert detail.status_code == 200
        assert detail.json()["dependencies"] == []
        assert detail.json()["runs"] == []

        specified = await c.post(
            f"/api/tasks/{task_id}/specify",
            headers=HEADERS,
            json={"body": "Executable spec"},
        )
        assert specified.status_code == 200
        assert specified.json()["status"] == "todo"
        assert ("publish", task_id) in manager.calls
    finally:
        await repository.close()
        await database.close()


# --------------------------------------------------------------- enrichment
# The list, tree, and single-task exits must all serialize the same card-facing
# derived fields (task-card-status.md §1).  Wire a real repository behind the
# router and assert the three exits agree, including the delivery summary.

_ENRICHMENT_KEYS = {
    "latest_run_state",
    "latest_heartbeat_at",
    "latest_run_workspace_mode",
    "child_count",
    "dependency_count",
    "delivery",
}


@pytest.fixture
async def real_client(tmp_path):
    db_path = tmp_path / "owlery.db"
    db = Database(str(db_path))
    await db.initialize()
    repo = TaskRepository(str(db_path))
    await repo.initialize()
    agent_id = (await db.load_agents())[0]["id"]
    app = FastAPI()
    app.include_router(routes.router)
    previous = routes.task_repository
    routes.task_repository = repo
    # Mutation exits publish via _get_manager(); bind (and restore) a lightweight
    # manager so these tests don't depend on global _manager pollution from an
    # earlier test — they must pass in isolation too.
    previous_manager = routes._manager
    routes.set_manager(_Manager())
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, repo, tmp_path, agent_id
    finally:
        routes.task_repository = previous
        routes.set_manager(previous_manager)
        await repo.close()
        await db.close()


async def _accepted_task_with_delivery(repo, root, agent_id):
    """A git_worktree task whose run completed and whose delivery is accepted
    (status=ready, dirty, 1 commit ahead) — the enrichment-bearing shape the
    card exits must preserve.  Returns (board, task)."""
    board = await repo.create_board(
        name="Ship", working_dir=str(root), default_workspace_mode="git_worktree"
    )
    task = await repo.create_task(
        board_id=board.id, title="Deliver", status="todo", assignee_agent_id=agent_id
    )
    run = await repo.claim_ready(
        task.id, workspace_mode="git_worktree", workspace_path=str(root / "wt")
    )
    await repo.complete_run(task.id, run.id, summary="done")
    delivery = await repo.create_delivery(
        run.id, repository="/repo", attempt_branch="owlery/x", base_ref="main", base_head="a"
    )
    await repo.start_accept(delivery.id)
    await repo.record_baseline(delivery.id, status="ready", dirty=True, commits_ahead=1)
    return board, task


@pytest.mark.asyncio
async def test_list_tree_and_detail_expose_identical_enrichment(real_client):
    c, repo, root, agent_id = real_client
    board, task = await _accepted_task_with_delivery(repo, root, agent_id)

    listed = await c.get(f"/api/tasks?board_id={board.id}", headers=HEADERS)
    assert listed.status_code == 200
    listed_body = listed.json()
    assert listed_body["total"] >= 1
    card = next(t for t in listed_body["items"] if t["id"] == task.id)
    assert "body" not in card
    assert "body_excerpt" in card

    tree = await c.get(f"/api/task-boards/{board.id}/tree", headers=HEADERS)
    assert tree.status_code == 200
    tree_node = next(t for t in tree.json() if t["id"] == task.id)

    detail = await c.get(f"/api/tasks/{task.id}", headers=HEADERS)
    assert detail.status_code == 200
    detail_body = detail.json()

    for body in (card, tree_node, detail_body):
        assert _ENRICHMENT_KEYS <= set(body)
        assert body["latest_run_state"] == "completed"
        assert body["latest_run_workspace_mode"] == "git_worktree"
        assert body["delivery"]["status"] == "ready"
        assert body["delivery"]["dirty"] is True
        assert body["delivery"]["commits_ahead"] == 1
    for key in _ENRICHMENT_KEYS:
        assert card[key] == tree_node[key] == detail_body[key], key


@pytest.mark.asyncio
async def test_mutation_response_carries_enrichment(real_client):
    """A mutation the browser upserts must return the same enriched card shape as
    the WS payload published for that change; a bare TaskRecord would lose the
    timestamp-tie merge race and strip the card's delivery chip (§3).

    A delivered card is always a done task (its run completed), so archival — not
    a title edit — is the realistic successful mutation a user runs against it."""
    c, repo, root, agent_id = real_client
    _board, task = await _accepted_task_with_delivery(repo, root, agent_id)

    resp = await c.post(f"/api/tasks/{task.id}/archive", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["archived"] is True
    assert _ENRICHMENT_KEYS <= set(body)
    assert body["latest_run_state"] == "completed"
    assert body["latest_run_workspace_mode"] == "git_worktree"
    assert body["delivery"]["status"] == "ready"
    assert body["delivery"]["commits_ahead"] == 1


@pytest.mark.asyncio
async def test_stale_patch_conflict_current_is_enriched(real_client):
    """The 412 conflict snapshot the browser upserts on a rejected stale edit
    must be enriched too, or a losing edit wipes the card's chip (§3)."""
    c, repo, root, agent_id = real_client
    _board, task = await _accepted_task_with_delivery(repo, root, agent_id)

    resp = await c.patch(
        f"/api/tasks/{task.id}",
        json={"title": "Renamed"},
        headers={**HEADERS, "If-Match": '"1999-01-01T00:00:00+00:00"'},
    )
    assert resp.status_code == 412
    current = resp.json()["detail"]["current_task"]
    assert _ENRICHMENT_KEYS <= set(current)
    assert current["delivery"]["status"] == "ready"
    assert current["latest_run_state"] == "completed"


@pytest.mark.asyncio
async def test_list_tasks_page_omits_body_and_paginates(real_client):
    """GET /api/tasks must never carry a task's full body — a page-sized
    excerpt plus a total count instead (task-board-overhaul.md §3.5); full
    text is always a `show`/single-task fetch."""
    c, repo, root, agent_id = real_client
    board = await repo.create_board(
        name="Big", working_dir=str(root), default_workspace_mode="shared"
    )
    huge_body = "y" * 200_000
    for n in range(3):
        await repo.create_task(
            board_id=board.id, title=f"T{n}", status="todo",
            assignee_agent_id=agent_id, body=huge_body,
        )

    listed = await c.get(f"/api/tasks?board_id={board.id}", headers=HEADERS)
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["total"] == 3
    assert payload["limit"] == 200
    assert payload["offset"] == 0
    assert len(payload["items"]) == 3
    for item in payload["items"]:
        assert "body" not in item
        assert item["body_excerpt"] == huge_body[:200]
    # The whole page stays well under the ~19-char-per-task*10^5 blowup a full
    # body would cause — a hard ceiling that would catch a body regression.
    assert len(listed.content) < 20_000

    page1 = await c.get(
        f"/api/tasks?board_id={board.id}&limit=2&offset=0", headers=HEADERS
    )
    page2 = await c.get(
        f"/api/tasks?board_id={board.id}&limit=2&offset=2", headers=HEADERS
    )
    assert page1.json()["total"] == page2.json()["total"] == 3
    ids_1 = {item["id"] for item in page1.json()["items"]}
    ids_2 = {item["id"] for item in page2.json()["items"]}
    assert len(ids_1) == 2 and len(ids_2) == 1
    assert ids_1.isdisjoint(ids_2)


async def _extra_delivery(repo, board, agent_id, root, *, suffix, title="Deliver 2"):
    """A second (or third...) ready+accepted delivery on the SAME board, for
    supersede-chain tests — mirrors `_accepted_task_with_delivery` but reuses
    the given board instead of planning a new one (board names are unique)."""
    task = await repo.create_task(
        board_id=board.id, title=title, status="todo", assignee_agent_id=agent_id
    )
    run = await repo.claim_ready(
        task.id, workspace_mode="git_worktree", workspace_path=str(root / f"wt-{suffix}")
    )
    await repo.complete_run(task.id, run.id, summary="done")
    delivery = await repo.create_delivery(
        run.id, repository="/repo", attempt_branch=f"owlery/{suffix}", base_ref="main", base_head="a"
    )
    await repo.start_accept(delivery.id)
    await repo.record_baseline(delivery.id, status="ready", dirty=False, commits_ahead=2)
    return task, await repo.get_delivery_by_run(run.id)


@pytest.mark.asyncio
async def test_delivery_chain_endpoint_reports_target_and_superseded(real_client):
    """GET .../delivery/chain feeds the panel's collapse UI (task-board-
    overhaul.md §3.1): a superseded delivery reports its target task/title
    for the "collapsed by task X" link, and the tip reports the reverse list
    for its batch-teardown affordance."""
    c, repo, root, agent_id = real_client
    board, task_a = await _accepted_task_with_delivery(repo, root, agent_id)
    task_b, delivery_b = await _extra_delivery(repo, board, agent_id, root, suffix="y")
    delivery_a = await repo.get_delivery_by_run((await repo.list_runs(task_a.id))[0].id)
    await repo.set_superseded_by(delivery_a.id, delivery_b.id, expected_current=None)

    tip = await c.get(
        f"/api/tasks/{task_b.id}/runs/{delivery_b.run_id}/delivery/chain", headers=HEADERS
    )
    assert tip.status_code == 200
    tip_body = tip.json()
    assert tip_body["target"] is None
    assert [item["task_id"] for item in tip_body["superseded"]] == [task_a.id]
    assert tip_body["superseded"][0]["task_title"] == "Deliver"

    collapsed = await c.get(
        f"/api/tasks/{task_a.id}/runs/{delivery_a.run_id}/delivery/chain", headers=HEADERS
    )
    assert collapsed.status_code == 200
    collapsed_body = collapsed.json()
    assert collapsed_body["superseded"] == []
    assert collapsed_body["target"]["task_id"] == task_b.id
    assert collapsed_body["target"]["delivery_id"] == delivery_b.id


@pytest.mark.asyncio
async def test_delivery_chain_endpoint_resolves_transitive_chain(real_client):
    """A→B→C: each recompute only ever updates ONE link (delivery.py's
    propagation skips a sibling that already points somewhere), so A's stored
    pointer still says B even after B itself gets collapsed into C. The
    `/delivery/chain` endpoint must resolve past that one-hop staleness:
    every node's `target` is the ultimate tip C, and C's `superseded` must
    include BOTH A and B — otherwise C's batch-teardown never reaches A, and
    A's collapsed panel points at a delivery (B) that is itself collapsed
    (Snape review, task-board-overhaul.md §3.1)."""
    c, repo, root, agent_id = real_client
    board, task_a = await _accepted_task_with_delivery(repo, root, agent_id)
    task_b, delivery_b = await _extra_delivery(repo, board, agent_id, root, suffix="y", title="Deliver 2")
    task_c, delivery_c = await _extra_delivery(repo, board, agent_id, root, suffix="z", title="Deliver 3")
    delivery_a = await repo.get_delivery_by_run((await repo.list_runs(task_a.id))[0].id)
    # A points to B, B points to C — B's own pointer is NOT retargeted to C.
    await repo.set_superseded_by(delivery_a.id, delivery_b.id, expected_current=None)
    await repo.set_superseded_by(delivery_b.id, delivery_c.id, expected_current=None)

    chain_a = (await c.get(
        f"/api/tasks/{task_a.id}/runs/{delivery_a.run_id}/delivery/chain", headers=HEADERS
    )).json()
    assert chain_a["target"]["task_id"] == task_c.id
    assert chain_a["target"]["delivery_id"] == delivery_c.id
    assert chain_a["superseded"] == []

    chain_b = (await c.get(
        f"/api/tasks/{task_b.id}/runs/{delivery_b.run_id}/delivery/chain", headers=HEADERS
    )).json()
    assert chain_b["target"]["task_id"] == task_c.id
    assert [item["task_id"] for item in chain_b["superseded"]] == [task_a.id]

    chain_c = (await c.get(
        f"/api/tasks/{task_c.id}/runs/{delivery_c.run_id}/delivery/chain", headers=HEADERS
    )).json()
    assert chain_c["target"] is None
    assert {item["task_id"] for item in chain_c["superseded"]} == {task_a.id, task_b.id}


@pytest.mark.asyncio
async def test_enriched_accepts_task_record_or_mapping(monkeypatch):
    """The manager's block / running-cancel exits hand _enriched an already
    serialized task dict, not a TaskRecord; it must accept both — through the
    enrich path and the capability-less fallback — without a 500."""

    class _Enrich:
        async def enrich_task(self, task_id):
            return {"id": task_id, "enriched": True}

    monkeypatch.setattr(routes, "task_repository", _Enrich())
    from_mapping = await routes._enriched({"id": "task-map"})
    from_record = await routes._enriched(_Record("task-rec"))
    assert from_mapping == {"id": "task-map", "enriched": True}
    assert from_record == {"id": "task-rec", "enriched": True}

    class _NoEnrich:  # a repository double lacking the enrich capability
        pass

    monkeypatch.setattr(routes, "task_repository", _NoEnrich())
    payload = {"id": "task-9", "title": "keep me"}
    assert await routes._enriched(payload) == payload
    assert (await routes._enriched(_Record("task-8")))["id"] == "task-8"
