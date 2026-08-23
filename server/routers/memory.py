"""Read-only agent memory browsing (docs/plans/memory-ui.md §设计要点 1).

Four GET endpoints over the on-disk per-agent memory dirs written by
`server/agent_memory.py`: list, single-file read, cross-agent search, and a
`[[link]]` graph. There is no write path here — and there must never be one;
all corrections go through the agent itself (delegated chat), never a direct
file edit from this router.

The one real security surface is the single-file read: `name` is
attacker-influenced and must never escape the requesting agent's memory dir.
Both `agent_id` (URL path segment) and `name` (query param) are validated as
plain path segments before any filesystem access, and the resolved path is
re-checked against the resolved memory dir as defense in depth.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ..agent_memory import agent_memory_dir
from ..auth import verify_token
from ..config import settings

router = APIRouter(prefix="/api/memory", tags=["memory"])

INDEX_FILENAME = "MEMORY.md"

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]\[]+)\]\]")


class MemoryFileMeta(BaseModel):
    file: str
    name: str | None = None
    description: str | None = None
    type: str | None = None


class MemoryListResponse(BaseModel):
    agent_id: str
    index: MemoryFileMeta | None
    files: list[MemoryFileMeta]


class MemorySearchHit(BaseModel):
    agent_id: str
    file: str
    name: str | None = None
    type: str | None = None
    snippet: str


class MemorySearchResponse(BaseModel):
    query: str
    hits: list[MemorySearchHit]


class MemoryGraphNode(BaseModel):
    id: str
    file: str | None = None
    description: str | None = None
    type: str | None = None
    ghost: bool = False


class MemoryGraphEdge(BaseModel):
    source: str
    target: str


class MemoryGraphResponse(BaseModel):
    agent_id: str
    nodes: list[MemoryGraphNode]
    edges: list[MemoryGraphEdge]


def _is_safe_segment(value: str) -> bool:
    """A single path segment: no separators, no `..`/`.`, no null byte."""
    if not value or value in (".", ".."):
        return False
    if "/" in value or "\\" in value or "\x00" in value:
        return False
    return True


def _require_safe_segment(value: str, field: str) -> None:
    if not _is_safe_segment(value):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid {field}")


def _safe_agent_memory_dir(agent_id: str) -> Path:
    """`agent_memory_dir(agent_id)`, rejecting any `agent_id` that would let
    the join escape `<agents_dir>/<agent_id>/memory` (path-traversal guard —
    the sole security-relevant helper in this router)."""
    _require_safe_segment(agent_id, "agent_id")
    return agent_memory_dir(agent_id)


def _list_memory_md_files(agent_id: str) -> list[Path]:
    mem_dir = _safe_agent_memory_dir(agent_id)
    if not mem_dir.is_dir():
        return []
    return sorted(p for p in mem_dir.iterdir() if p.is_file() and p.suffix == ".md")


def _parse_frontmatter(text: str) -> dict:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _file_meta(path: Path) -> MemoryFileMeta:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    fm = _parse_frontmatter(text)
    metadata = fm.get("metadata")
    mtype = metadata.get("type") if isinstance(metadata, dict) else None
    return MemoryFileMeta(
        file=path.name,
        name=fm.get("name") if isinstance(fm.get("name"), str) else None,
        description=fm.get("description")
        if isinstance(fm.get("description"), str)
        else None,
        type=mtype if isinstance(mtype, str) else None,
    )


@router.get("/search", response_model=MemorySearchResponse)
async def search_memory(
    q: str = Query(min_length=1), _: str = Depends(verify_token)
):
    """Scan every agent's memory dir for `q` (case-insensitive substring).
    Corpus is small (a few dozen short files per agent) so a direct scan is
    the whole implementation — no search index (memory-ui.md §不做清单)."""
    root = Path(settings.agents_dir).expanduser()
    hits: list[MemorySearchHit] = []
    if not root.is_dir():
        return MemorySearchResponse(query=q, hits=hits)

    needle = q.lower()
    for agent_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        agent_id = agent_dir.name
        for path in _list_memory_md_files(agent_id):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            idx = text.lower().find(needle)
            if idx == -1:
                continue
            start = max(0, idx - 60)
            end = min(len(text), idx + len(q) + 60)
            snippet = " ".join(text[start:end].split())
            meta = _file_meta(path)
            hits.append(
                MemorySearchHit(
                    agent_id=agent_id,
                    file=path.name,
                    name=meta.name,
                    type=meta.type,
                    snippet=snippet,
                )
            )
    return MemorySearchResponse(query=q, hits=hits)


@router.get("/{agent_id}", response_model=MemoryListResponse)
async def list_memory(agent_id: str, _: str = Depends(verify_token)):
    files = _list_memory_md_files(agent_id)
    index: MemoryFileMeta | None = None
    entries: list[MemoryFileMeta] = []
    for path in files:
        if path.name == INDEX_FILENAME:
            index = _file_meta(path)
        else:
            entries.append(_file_meta(path))
    return MemoryListResponse(agent_id=agent_id, index=index, files=entries)


@router.get("/{agent_id}/file")
async def read_memory_file(
    agent_id: str, name: str = Query(...), _: str = Depends(verify_token)
):
    """Raw file contents. Path traversal is the only real risk in this
    router: `name` is validated as a plain segment, then the resolved path is
    re-checked to stay under the resolved memory dir before it is opened."""
    _require_safe_segment(name, "name")
    mem_dir = _safe_agent_memory_dir(agent_id).resolve()
    target = (mem_dir / name).resolve()
    if target != mem_dir and mem_dir not in target.parents:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid name")
    if not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "memory file not found")
    return PlainTextResponse(target.read_text(encoding="utf-8"), media_type="text/markdown")


@router.get("/{agent_id}/graph", response_model=MemoryGraphResponse)
async def memory_graph(agent_id: str, _: str = Depends(verify_token)):
    """`[[name]]` link graph for one agent (agent-scoped namespace — no
    cross-agent resolution, per memory-ui.md §不做清单). `MEMORY.md` is the
    index page, not a graph node, and is excluded. A link to a `name` with no
    matching file still produces a node, marked `ghost`."""
    files = [p for p in _list_memory_md_files(agent_id) if p.name != INDEX_FILENAME]

    nodes: dict[str, MemoryGraphNode] = {}
    node_id_by_file: dict[str, str] = {}
    file_links: list[tuple[str, str]] = []  # (source file, target id)

    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        fm = _parse_frontmatter(text)
        slug = fm.get("name") if isinstance(fm.get("name"), str) else path.stem
        metadata = fm.get("metadata")
        mtype = metadata.get("type") if isinstance(metadata, dict) else None
        nodes[slug] = MemoryGraphNode(
            id=slug,
            file=path.name,
            description=fm.get("description")
            if isinstance(fm.get("description"), str)
            else None,
            type=mtype if isinstance(mtype, str) else None,
            ghost=False,
        )
        node_id_by_file[path.name] = slug
        for m in _WIKILINK_RE.finditer(text):
            file_links.append((path.name, m.group(1).strip()))

    edges: list[MemoryGraphEdge] = []
    seen_edges: set[tuple[str, str]] = set()
    for source_file, target_id in file_links:
        source_id = node_id_by_file[source_file]
        if target_id not in nodes:
            nodes[target_id] = MemoryGraphNode(id=target_id, ghost=True)
        pair = (source_id, target_id)
        if pair in seen_edges:
            continue
        seen_edges.add(pair)
        edges.append(MemoryGraphEdge(source=source_id, target=target_id))

    return MemoryGraphResponse(
        agent_id=agent_id, nodes=list(nodes.values()), edges=edges
    )
