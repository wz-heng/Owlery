"""REST tests for the read-only memory router (docs/plans/memory-ui.md §设计要点 1).

Four endpoints, no DB/manager wiring needed — everything reads straight off
`agent_memory_dir()`. Each test monkeypatches `settings.agents_dir` to an
isolated `tmp_path` so it never touches the shared test-session agents dir
conftest.py points at.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import settings
from server.main import app

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

FACT_TEMPLATE = """---
name: {name}
description: {description}
metadata:
  type: {type}
---

{body}
"""


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def agents_root(tmp_path, monkeypatch):
    root = tmp_path / "agents"
    root.mkdir()
    monkeypatch.setattr(settings, "agents_dir", str(root))
    return root


def _write_fact(mem_dir, filename, *, name, description, type_, body=""):
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / filename).write_text(
        FACT_TEMPLATE.format(name=name, description=description, type=type_, body=body)
    )


# ---------------------------------------------------------------- list ----


@pytest.mark.asyncio
async def test_list_unknown_agent_returns_empty(client, agents_root):
    r = await client.get("/api/memory/ghost-agent", headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"agent_id": "ghost-agent", "index": None, "files": []}


@pytest.mark.asyncio
async def test_list_splits_index_and_parses_frontmatter(client, agents_root):
    mem_dir = agents_root / "a1" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text("# Memory Index\n\n- [X](x.md) — hook\n")
    _write_fact(
        mem_dir, "x.md", name="x", description="fact about x", type_="project"
    )
    _write_fact(
        mem_dir, "y.md", name="y", description="fact about y", type_="feedback"
    )
    # non-markdown files must be ignored entirely
    (mem_dir / "notes.txt").write_text("not a memory file")

    r = await client.get("/api/memory/a1", headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["index"]["file"] == "MEMORY.md"
    assert body["index"]["name"] is None  # MEMORY.md carries no frontmatter

    files = {f["file"]: f for f in body["files"]}
    assert set(files) == {"x.md", "y.md"}
    assert files["x.md"] == {
        "file": "x.md", "name": "x", "description": "fact about x", "type": "project"
    }
    assert files["y.md"]["type"] == "feedback"


@pytest.mark.asyncio
async def test_list_tolerates_file_with_no_frontmatter(client, agents_root):
    mem_dir = agents_root / "a1" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "plain.md").write_text("just some text, no frontmatter\n")

    r = await client.get("/api/memory/a1", headers=HEADERS)
    assert r.status_code == 200, r.text
    files = r.json()["files"]
    assert files == [{"file": "plain.md", "name": None, "description": None, "type": None}]


@pytest.mark.asyncio
async def test_list_requires_auth(client, agents_root):
    r = await client.get("/api/memory/a1")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_list_skips_symlink_escaping_memory_dir(client, agents_root):
    """An agent's harness can write inside its own memory dir; a symlink
    planted there pointing outside must not be followed by the directory
    scan shared by list/search/graph — same containment the file-read
    endpoint already enforces."""
    outside = agents_root.parent / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("TOP SECRET OUTSIDE FILE")

    mem_dir = agents_root / "a1" / "memory"
    mem_dir.mkdir(parents=True)
    _write_fact(mem_dir, "real.md", name="real", description="d", type_="project")
    (mem_dir / "escape.md").symlink_to(outside / "secret.md")

    r = await client.get("/api/memory/a1", headers=HEADERS)
    assert r.status_code == 200, r.text
    files = {f["file"] for f in r.json()["files"]}
    assert files == {"real.md"}


# ---------------------------------------------------------------- file ----


@pytest.mark.asyncio
async def test_read_file_returns_raw_content(client, agents_root):
    mem_dir = agents_root / "a1" / "memory"
    _write_fact(mem_dir, "x.md", name="x", description="d", type_="project", body="hello world")

    r = await client.get("/api/memory/a1/file", params={"name": "x.md"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/markdown")
    assert r.text == (mem_dir / "x.md").read_text()
    assert "hello world" in r.text


@pytest.mark.asyncio
async def test_read_file_missing_404(client, agents_root):
    (agents_root / "a1" / "memory").mkdir(parents=True)
    r = await client.get("/api/memory/a1/file", params={"name": "nope.md"}, headers=HEADERS)
    assert r.status_code == 404


@pytest.mark.parametrize(
    "name",
    [
        "../MEMORY.md",
        "../secret.txt",
        "sub/dir.md",
        "..",
        ".",
    ],
)
@pytest.mark.asyncio
async def test_read_file_rejects_path_traversal_in_name(client, agents_root, name):
    mem_dir = agents_root / "a1" / "memory"
    mem_dir.mkdir(parents=True)
    # A secret file OUTSIDE the agent's memory dir that a traversal attempt
    # would try to reach.
    (agents_root / "a1").joinpath("secret.txt").write_text("TOP SECRET")
    (agents_root / "MEMORY.md").write_text("TOP SECRET SIBLING")

    r = await client.get("/api/memory/a1/file", params={"name": name}, headers=HEADERS)
    assert r.status_code == 400, r.text
    assert "TOP SECRET" not in r.text


@pytest.mark.asyncio
async def test_read_file_rejects_path_traversal_in_agent_id(client, agents_root):
    (agents_root / "a1" / "memory").mkdir(parents=True)
    (agents_root / "outside-secret.md").write_text("TOP SECRET")

    r = await client.get("/api/memory/%2e%2e/file", params={"name": "outside-secret.md"}, headers=HEADERS)
    # ".." as a bare path segment either fails to route (404) or is rejected
    # by our own validation (400) — either way it must never leak content.
    assert r.status_code in (400, 404)
    assert "TOP SECRET" not in r.text


def test_safe_agent_memory_dir_rejects_traversal_directly(agents_root):
    """Unit-level guard on the validation helper itself, so the traversal
    defense is asserted independent of any HTTP-client-side URL
    normalization that might mask the same attack over the wire."""
    from fastapi import HTTPException

    from server.routers.memory import _safe_agent_memory_dir

    for bad in ("..", ".", "../escape", "a/b", "a\\b", ""):
        with pytest.raises(HTTPException) as exc_info:
            _safe_agent_memory_dir(bad)
        assert exc_info.value.status_code == 400

    # A well-formed agent_id resolves cleanly under agents_root.
    resolved = _safe_agent_memory_dir("a1")
    assert resolved == agents_root / "a1" / "memory"


# -------------------------------------------------------------- search ----


@pytest.mark.asyncio
async def test_search_finds_hits_across_agents_case_insensitive(client, agents_root):
    _write_fact(
        agents_root / "a1" / "memory", "x.md",
        name="x", description="d1", type_="project", body="The Clash proxy hijacks loopback traffic.",
    )
    _write_fact(
        agents_root / "a2" / "memory", "y.md",
        name="y", description="d2", type_="feedback", body="unrelated content here.",
    )

    r = await client.get("/api/memory/search", params={"q": "CLASH"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["query"] == "CLASH"
    assert len(body["hits"]) == 1
    hit = body["hits"][0]
    assert hit["agent_id"] == "a1"
    assert hit["file"] == "x.md"
    assert hit["name"] == "x"
    assert hit["type"] == "project"
    assert "clash" in hit["snippet"].lower()


@pytest.mark.asyncio
async def test_search_no_matches_returns_empty_hits(client, agents_root):
    _write_fact(agents_root / "a1" / "memory", "x.md", name="x", description="d", type_="project", body="abc")
    r = await client.get("/api/memory/search", params={"q": "zzz-nomatch"}, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["hits"] == []


@pytest.mark.asyncio
async def test_search_does_not_follow_symlink_outside_memory_dir(client, agents_root):
    outside = agents_root.parent / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("TOP SECRET NEEDLE")

    mem_dir = agents_root / "a1" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "escape.md").symlink_to(outside / "secret.md")

    r = await client.get("/api/memory/search", params={"q": "NEEDLE"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["hits"] == []


@pytest.mark.asyncio
async def test_search_missing_agents_dir_returns_empty(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "does-not-exist"))
    r = await client.get("/api/memory/search", params={"q": "anything"}, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["hits"] == []


@pytest.mark.asyncio
async def test_search_requires_nonempty_query(client, agents_root):
    r = await client.get("/api/memory/search", params={"q": ""}, headers=HEADERS)
    assert r.status_code == 422


# --------------------------------------------------------------- graph ----


@pytest.mark.asyncio
async def test_graph_builds_nodes_and_edges_with_ghost(client, agents_root):
    mem_dir = agents_root / "a1" / "memory"
    (mem_dir).mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text("# Memory Index\n\n- [[a]] linked here too (must be ignored)\n")
    _write_fact(
        mem_dir, "a.md", name="a", description="fact a", type_="project",
        body="See also [[b]] and the not-yet-written [[c]].",
    )
    _write_fact(mem_dir, "b.md", name="b", description="fact b", type_="feedback", body="no links here")

    r = await client.get("/api/memory/a1/graph", headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()

    nodes_by_id = {n["id"]: n for n in body["nodes"]}
    assert set(nodes_by_id) == {"a", "b", "c"}
    assert nodes_by_id["a"]["ghost"] is False
    assert nodes_by_id["a"]["file"] == "a.md"
    assert nodes_by_id["a"]["type"] == "project"
    assert nodes_by_id["b"]["ghost"] is False
    assert nodes_by_id["c"]["ghost"] is True
    assert nodes_by_id["c"]["file"] is None

    edges = {(e["source"], e["target"]) for e in body["edges"]}
    assert edges == {("a", "b"), ("a", "c")}
    # MEMORY.md's own `[[a]]` text must never produce an edge — it is the
    # index page, not a graph participant.
    assert all(e["source"] != "MEMORY.md" for e in body["edges"])


@pytest.mark.asyncio
async def test_graph_empty_agent_returns_empty_graph(client, agents_root):
    r = await client.get("/api/memory/nope/graph", headers=HEADERS)
    assert r.status_code == 200
    assert r.json() == {"agent_id": "nope", "nodes": [], "edges": []}


@pytest.mark.asyncio
async def test_graph_does_not_follow_symlink_outside_memory_dir(client, agents_root):
    outside = agents_root.parent / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("---\nname: leaked\n---\n\nTOP SECRET")

    mem_dir = agents_root / "a1" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "escape.md").symlink_to(outside / "secret.md")

    r = await client.get("/api/memory/a1/graph", headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["nodes"] == []
    assert body["edges"] == []


@pytest.mark.asyncio
async def test_graph_falls_back_to_filename_when_frontmatter_missing_name(client, agents_root):
    mem_dir = agents_root / "a1" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "no-frontmatter.md").write_text("plain body, no frontmatter, no links")

    r = await client.get("/api/memory/a1/graph", headers=HEADERS)
    assert r.status_code == 200
    nodes_by_id = {n["id"]: n for n in r.json()["nodes"]}
    assert set(nodes_by_id) == {"no-frontmatter"}
    assert nodes_by_id["no-frontmatter"]["file"] == "no-frontmatter.md"
