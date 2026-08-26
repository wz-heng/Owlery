"""Shared sandboxing + mime detection for the in-app file viewer.

The viewer lets the user open a file from the session's working_dir in a
browser modal via `/showme`. The endpoint funnels through
`resolve_safe_path` so the security model is single-sourced.

Threats addressed:
  - Path traversal (../../etc/passwd): realpath + commonpath gate.
  - Symlink escape: a symlink under working_dir pointing outside is
    rejected after resolution.
  - Oversized files: 2 MiB cap returns FileTooLarge so the caller can
    surface a clear error instead of streaming a 1 GB log into the
    browser modal.
  - Unsupported / hostile types: extension allowlist of the formats
    the modal can actually render (markdown, plain text, code, images,
    pdf). Anything else is rejected before bytes leave the disk.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path

# Hard cap. PDFs and screenshots are the realistic ceiling; 2 MiB
# comfortably covers both without letting the model surface multi-MB
# log files that would freeze the browser.
MAX_FILE_BYTES = 2 * 1024 * 1024


# Extension → semantic kind the frontend renderer dispatches on.
# Keys are lowercase, with the leading dot.
EXT_KINDS: dict[str, str] = {
    # Markdown
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdx": "markdown",
    # Images
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".svg": "image",
    ".bmp": "image",
    ".avif": "image",
    # PDF
    ".pdf": "pdf",
    # Code (rendered with syntax highlighting)
    ".py": "code",
    ".pyi": "code",
    ".js": "code",
    ".jsx": "code",
    ".ts": "code",
    ".tsx": "code",
    ".mjs": "code",
    ".cjs": "code",
    ".rs": "code",
    ".go": "code",
    ".java": "code",
    ".kt": "code",
    ".c": "code",
    ".h": "code",
    ".cpp": "code",
    ".hpp": "code",
    ".cs": "code",
    ".rb": "code",
    ".php": "code",
    ".swift": "code",
    ".scala": "code",
    ".sh": "code",
    ".bash": "code",
    ".zsh": "code",
    ".sql": "code",
    ".html": "code",
    ".css": "code",
    ".scss": "code",
    ".less": "code",
    ".vue": "code",
    ".lua": "code",
    ".r": "code",
    ".dart": "code",
    ".toml": "code",
    ".yaml": "code",
    ".yml": "code",
    ".json": "code",
    ".xml": "code",
    ".ini": "code",
    ".env": "code",
    ".dockerfile": "code",
    ".tf": "code",
    # Plain text
    ".txt": "text",
    ".log": "text",
    ".csv": "text",
    ".tsv": "text",
}


# Files whose *name* (case-insensitive, no extension) we also want
# to render. Treated as code-with-no-language.
ALLOWLISTED_BARE_NAMES = {
    "dockerfile",
    "makefile",
    "readme",
    "license",
    "notice",
    "changelog",
    "authors",
    ".gitignore",
    ".dockerignore",
    ".editorconfig",
    ".prettierrc",
    ".eslintrc",
}


class FileViewerError(Exception):
    """Base. Each subclass maps to a distinct HTTP status."""


class PathRejected(FileViewerError):
    """Path is outside working_dir, or otherwise malformed."""


class FileNotFound(FileViewerError):
    """Resolved path does not point at a regular file."""


class FileTooLarge(FileViewerError):
    """File exceeds MAX_FILE_BYTES."""


class UnsupportedType(FileViewerError):
    """Extension is not in the allowlist."""


@dataclass(frozen=True)
class ResolvedFile:
    """Validated, ready-to-serve descriptor.

    `kind` is the renderer hint the frontend uses to pick its
    component (markdown / image / pdf / code / text). `mime_type`
    is the Content-Type for the HTTP response.
    """

    abs_path: Path
    relative_path: str
    kind: str
    mime_type: str
    size: int


def _classify(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in EXT_KINDS:
        return EXT_KINDS[suffix]
    if path.name.lower() in ALLOWLISTED_BARE_NAMES:
        return "code"
    raise UnsupportedType(
        f"Unsupported file type: {path.name!r}. "
        "The viewer renders markdown, code, images, PDFs, and plain text."
    )


def _mime_for(path: Path, kind: str) -> str:
    # mimetypes covers the common ones; we override only where its
    # default is wrong for what the browser wants.
    if kind == "markdown":
        return "text/markdown; charset=utf-8"
    if kind == "pdf":
        return "application/pdf"
    if kind == "image":
        guessed, _ = mimetypes.guess_type(path.name)
        if guessed:
            return guessed
        # SVG is sometimes missed depending on the platform mimetypes DB.
        if path.suffix.lower() == ".svg":
            return "image/svg+xml"
        return "application/octet-stream"
    if kind == "code" or kind == "text":
        # Always text/plain so the browser never tries to execute or
        # download. Charset matters — files are read as UTF-8.
        return "text/plain; charset=utf-8"
    return "application/octet-stream"


def _resolved_root(working_dir: str | Path) -> Path:
    root = Path(working_dir).resolve(strict=False)
    if not root.is_dir():
        raise PathRejected(f"Working directory does not exist: {working_dir}")
    return root


def resolve_existing_path(working_dir: str | Path, requested: str) -> Path:
    """The traversal/symlink-escape gate shared by every "read a file the
    caller names" entry point. `requested` may be absolute or relative;
    either way it must resolve to a regular file under `working_dir`
    (`resolve_safe_path` layers extension/size checks on top for the
    browser viewer; `resolve_raw_read_path` — mail-connector.md attachment
    sends — skips those, since attachments aren't viewer-renderable types).
    """
    if not requested or not requested.strip():
        raise PathRejected("Empty path")

    cleaned = requested.strip()
    # No tilde expansion — we sandbox to working_dir, full stop.
    if cleaned.startswith("~"):
        raise PathRejected("Tilde paths are not allowed; use a working-dir-relative path")

    root = _resolved_root(working_dir)

    candidate = Path(cleaned)
    if not candidate.is_absolute():
        candidate = root / candidate

    # `strict=False`: we want to resolve symlinks even if the final
    # component doesn't exist, so we can surface "not found" cleanly
    # below rather than as a confusing path-rejected error.
    resolved = candidate.resolve(strict=False)

    try:
        resolved.relative_to(root)
    except ValueError:
        raise PathRejected(
            f"Path escapes working directory: {requested!r}"
        ) from None

    if not resolved.exists():
        raise FileNotFound(f"File not found: {requested!r}")
    if not resolved.is_file():
        raise FileNotFound(f"Not a regular file: {requested!r}")
    return resolved


def resolve_raw_read_path(working_dir: str | Path, requested: str) -> Path:
    """Like `resolve_existing_path`, for callers that want raw bytes of any
    existing file (no viewer extension allowlist, no size cap here — the
    caller enforces its own)."""
    return resolve_existing_path(working_dir, requested)


def resolve_new_write_path(
    working_dir: str | Path, relative_dir: str, filename: str
) -> Path:
    """Where a new file (that doesn't have to exist yet) may be written
    under `working_dir`. `filename` is sanitized to its basename — no path
    separators, no traversal via the filename itself. `relative_dir` may
    add subdirectories but, after resolution, must still live under
    `working_dir` (mail-connector.md attachment downloads)."""
    root = _resolved_root(working_dir)

    name = Path(filename).name
    if not name or name in (".", ".."):
        raise PathRejected(f"Invalid filename: {filename!r}")

    sub = (relative_dir or "").strip().strip("/")
    base = root if not sub else (root / sub)
    resolved = (base / name).resolve(strict=False)

    try:
        resolved.relative_to(root)
    except ValueError:
        raise PathRejected(f"Path escapes working directory: {filename!r}") from None
    return resolved


def resolve_safe_path(working_dir: str | Path, requested: str) -> ResolvedFile:
    """Validate `requested` against `working_dir` AND the viewer's
    renderable-type allowlist. Raises on rejection — see
    `resolve_existing_path` for the traversal/symlink-escape rules this
    layers on top of."""
    resolved = resolve_existing_path(working_dir, requested)

    size = resolved.stat().st_size
    if size > MAX_FILE_BYTES:
        raise FileTooLarge(
            f"File is {size} bytes; viewer limit is {MAX_FILE_BYTES} bytes"
        )

    kind = _classify(resolved)
    mime = _mime_for(resolved, kind)
    rel = str(resolved.relative_to(_resolved_root(working_dir)))

    return ResolvedFile(
        abs_path=resolved,
        relative_path=rel,
        kind=kind,
        mime_type=mime,
        size=size,
    )
