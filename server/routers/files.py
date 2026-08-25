"""In-app file viewer endpoint + `/showme` reference resolver.

Streams a file from the session's working_dir to the browser modal.
`/showme` is a client-only flow: the browser sends the user's reference to
the resolver below (a one-shot model call interprets it in conversation
context), receives a concrete path, then fetches bytes through the
endpoints here. Security gates live in `server/file_viewer.py` so the read
paths can't drift.

Auth: bearer header OR `?token=`. The query-param path exists because
the dialog renders images/PDFs via `<img src>` / `<iframe src>`, and
those can't carry custom headers. Same auth value as the bearer one;
no second weaker token to leak.
"""

from __future__ import annotations

import base64
import binascii
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse

from ..config import settings

logger = logging.getLogger(__name__)
from ..file_viewer import (
    FileNotFound,
    FileTooLarge,
    PathRejected,
    ResolvedFile,
    UnsupportedType,
    resolve_new_write_path,
    resolve_raw_read_path,
    resolve_safe_path,
)
from ..harness import get_harness
from ..model_routing import resolve_model
from ..models import (
    SaveFileRequest,
    SaveFileResponse,
    ShowMeResolveRequest,
    ShowMeResolveResponse,
)
from ..session_manager import session_manager
from ..showme_ai import resolve_showme_reference

router = APIRouter(prefix="/api/sessions", tags=["files"])

# Cap for the raw read/save endpoints (MCP-server attachment traffic,
# mail-connector.md §4.2) — larger than the 2 MiB browser-viewer cap since
# these move arbitrary attachment bytes, not stream to a chat-modal render.
MAX_RAW_FILE_BYTES = 25 * 1024 * 1024


def _verify_token(
    request: Request, token: str | None = Query(default=None)
) -> str:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        candidate = auth_header.split(" ", 1)[1].strip()
        if candidate == settings.auth_token:
            return candidate
    if token and token == settings.auth_token:
        return token
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")


async def _working_dir_for(session_id: str) -> str:
    """Pull working_dir from the live session or, if archived, from the DB."""
    live = session_manager.get_session(session_id)
    if live is not None:
        return live.working_dir
    archived = await session_manager.load_archived_session_detail(session_id)
    if archived is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return archived.working_dir


def _resolve_or_raise(working_dir: str, path: str) -> ResolvedFile:
    try:
        return resolve_safe_path(working_dir, path)
    except PathRejected as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    except FileNotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    except FileTooLarge as e:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(e))
    except UnsupportedType as e:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(e))


@router.post("/{session_id}/showme/resolve", response_model=ShowMeResolveResponse)
async def resolve_showme(
    session_id: str,
    req: ShowMeResolveRequest,
    _: str = Depends(_verify_token),
) -> ShowMeResolveResponse:
    """Resolve a human file reference into a concrete viewer path."""
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    if session.agent_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")

    agent = await session_manager.db.get_agent(session.agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")

    harness = get_harness(session.backend)
    # Resolve through the same path the turn engine uses so the harness gets a
    # proper HarnessCredential (decrypted, OAuth-refreshed for Claude, or
    # `home_dir` for Codex), not a raw DB row. Effective id: session override
    # wins over the agent's default (agent-refactor.md §5.2 / decision #2).
    cred_id = session.credential_id or agent.get("credential_id")
    try:
        credential = await session_manager.resolve_credential_by_id(
            cred_id,
            style=harness.profile.credential_style,
            context=f"showme resolver for session {session_id}",
        )
        result = await resolve_showme_reference(
            req.text,
            harness=harness,
            # Same resolution as the turn engine: session override wins over the
            # agent's default model (budget-model-routing.md §4.1).
            model=resolve_model(session, agent),
            credential=credential,
            working_dir=session.working_dir,
            messages=await session_manager.db.load_messages(session_id),
            session_name=session.name,
        )
    except Exception:
        # Don't leak raw model/harness errors (paths, credential labels,
        # tracebacks) to the client; log them for the operator.
        logger.exception("showme resolve failed for session %s", session_id)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Couldn't resolve the file reference.",
        )
    return ShowMeResolveResponse(path=result.path, message=result.message)


@router.get("/{session_id}/files/meta")
async def file_meta(
    session_id: str,
    path: str = Query(...),
    _: str = Depends(_verify_token),
) -> JSONResponse:
    """Metadata-only sibling of /files.

    The dialog hits this first to decide which renderer to mount and
    whether to skip the bytes fetch entirely (e.g. for an oversized
    file, we show the error inline rather than streaming kilobytes
    that will never render). Same security path as /files.
    """
    working_dir = await _working_dir_for(session_id)
    resolved = _resolve_or_raise(working_dir, path)
    return JSONResponse(
        {
            "path": resolved.relative_path,
            "kind": resolved.kind,
            "mime_type": resolved.mime_type,
            "size": resolved.size,
        }
    )


@router.get("/{session_id}/files")
async def get_file(
    session_id: str,
    path: str = Query(...),
    _: str = Depends(_verify_token),
) -> FileResponse:
    working_dir = await _working_dir_for(session_id)
    resolved = _resolve_or_raise(working_dir, path)
    # FileResponse handles streaming + ETag + Range. We override
    # media_type so e.g. .md is delivered as text/markdown rather
    # than the default application/octet-stream.
    return FileResponse(
        resolved.abs_path,
        media_type=resolved.mime_type,
        filename=resolved.abs_path.name,
    )


@router.get("/{session_id}/files/raw")
async def get_file_raw(
    session_id: str,
    path: str = Query(...),
    _: str = Depends(_verify_token),
) -> FileResponse:
    """Raw bytes of any existing file under working_dir — no viewer
    extension allowlist (an MCP server reading a send()-attachment can name
    a .zip/.docx/etc that `/files` would reject). Internal: only connector
    MCP subprocesses call this, not the browser."""
    working_dir = await _working_dir_for(session_id)
    try:
        resolved = resolve_raw_read_path(working_dir, path)
    except PathRejected as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    except FileNotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    size = resolved.stat().st_size
    if size > MAX_RAW_FILE_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"File is {size} bytes; limit is {MAX_RAW_FILE_BYTES} bytes",
        )
    return FileResponse(
        resolved, media_type="application/octet-stream", filename=resolved.name
    )


def _dedupe_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    n = 1
    while True:
        candidate = parent / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


@router.post("/{session_id}/files/save", response_model=SaveFileResponse)
async def save_file(
    session_id: str,
    req: SaveFileRequest,
    _: str = Depends(_verify_token),
) -> SaveFileResponse:
    """Write a new file into the session's working directory — the
    counterpart to `/files/raw` (an MCP server, e.g. the `mail` connector's
    attachment downloads, writing bytes it fetched from a third party).
    Never overwrites an existing file — collisions get a `-1`, `-2`, …
    suffix. Internal-only, same as `/files/raw`."""
    working_dir = await _working_dir_for(session_id)
    try:
        path = resolve_new_write_path(working_dir, req.relative_dir, req.filename)
    except PathRejected as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    try:
        content = base64.b64decode(req.content_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "content_base64 is not valid base64")
    if len(content) > MAX_RAW_FILE_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"Content is {len(content)} bytes; limit is {MAX_RAW_FILE_BYTES} bytes",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    final = _dedupe_path(path)
    final.write_bytes(content)
    rel = str(final.relative_to(Path(working_dir).resolve(strict=False)))
    return SaveFileResponse(path=rel, size=len(content))
