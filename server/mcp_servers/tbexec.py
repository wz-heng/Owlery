"""MCP stdio server: terminal-bench shell protocol decision relay.

`tbexec` exposes exactly two tools to the model, mirroring the
`terminal-bench-shell-v1` A2A protocol spoken by AgentBeats' Terminal
Bench 2.0 green agent (docs/plans/agentbeats-a2a-adapter.md §2): the
green agent owns the real Docker sandbox and only ever lets the model
act on it by round-tripping one JSON decision per A2A message. There is
no local shell access in this session — the task's filesystem lives
inside the green agent's container, reachable only through this tool.

  - `run(command, timeout?)` — request one shell command in the task's
    sandbox. The sandbox is on the *green agent's* side, not this
    machine: calling this records the request and ends your turn. You
    will see the real exit code/stdout/stderr as the next user message.

  - `done(output)` — declare the task finished (solved, given up, or out
    of ideas). Ends the whole exercise; no further turns follow.

This server is only ever wired into the dedicated, no-memory exam agent
that `server/a2a_adapter.py` drives — it is intentionally never part of
the default built-in MCP set (`server/models.py:DEFAULT_MCP_SERVERS`),
so no production agent (Dobby/Snape/Albus/...) picks it up.

Channel: this process is a child of the `claude`/`codex` CLI, not of
Owlery's FastAPI server — same env-injection contract as `bg.py`/`ask.py`:

  OWLERY_API_BASE     e.g. "http://127.0.0.1:8000"
  OWLERY_AUTH_TOKEN   the same bearer token everything else uses
  OWLERY_SESSION_ID   the session this CLI invocation is bound to
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s tbexec-mcp %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


mcp = FastMCP("owlery-tbexec")


def _required_env(name: str) -> str | None:
    v = os.environ.get(name)
    if not v:
        logger.error("Required env var %s not set", name)
    return v


def _api_base() -> str | None:
    return _required_env("OWLERY_API_BASE")


def _session_id() -> str | None:
    return _required_env("OWLERY_SESSION_ID")


def _headers() -> dict[str, str] | None:
    tok = _required_env("OWLERY_AUTH_TOKEN")
    if not tok:
        return None
    return {"Authorization": f"Bearer {tok}"}


def _post_decision(payload: dict) -> str:
    api = _api_base()
    sid = _session_id()
    hdrs = _headers()
    if not (api and sid and hdrs):
        return "Error: tbexec server is misconfigured (env vars missing); cannot record decision."
    url = f"{api}/api/sessions/{sid}/tbexec/decision"
    try:
        r = httpx.post(url, json=payload, headers=hdrs, timeout=10.0, trust_env=False)
    except httpx.HTTPError as e:
        return f"Error: failed to reach Owlery to record the decision: {e}"
    if r.status_code == 409:
        return (
            "Error: a decision was already recorded this turn. Exactly one "
            "of `run`/`done` may be called per turn — stop now, you will "
            "not be asked again until the next turn."
        )
    if r.status_code != 200:
        return f"Error: Owlery rejected the decision ({r.status_code}): {r.text[:300]}"
    return r.json().get("message", "Recorded.")


@mcp.tool(name="run")
def tbexec_run(command: str, timeout: int = 60) -> str:
    """Request one shell command in the task's remote sandbox.

    This does NOT execute locally — there is no filesystem or shell on
    this machine for the task. The command is handed to the evaluator,
    which runs it inside the real sandbox container and reports back
    the exit code/stdout/stderr as your *next* turn's input.

    Call this at most once per turn. After calling it, stop — do not
    call any other tool and do not guess at the output; just end your
    turn with a short acknowledgement. You'll see the real result next
    turn.

    Args:
        command: A single shell command (or `&&`/`;`-joined sequence)
            to run in the task's sandbox.
        timeout: Seconds to allow the command to run, 1-300 (default 60).
            Clamped server-side.

    Returns:
        A short confirmation string, or an error if misconfigured or if
        a decision was already recorded this turn.
    """
    if not command.strip():
        return "Error: command must be a non-empty shell command string."
    timeout = max(1, min(int(timeout), 300))
    return _post_decision({"kind": "exec", "command": command, "timeout": timeout})


@mcp.tool(name="done")
def tbexec_done(output: str) -> str:
    """Declare the task finished — solved, given up, or out of budget.

    Call this instead of `run` once you have nothing more to try (you
    believe the task is solved, or you're stuck). This ends the whole
    exercise; there is no next turn after this.

    Call this at most once per turn, and never together with `run`.

    Args:
        output: A short final summary of what you did and the outcome.
            This is reported to the evaluator verbatim as your final
            answer for this task.

    Returns:
        A short confirmation string, or an error if misconfigured or if
        a decision was already recorded this turn.
    """
    return _post_decision({"kind": "finish", "output": output})


if __name__ == "__main__":
    mcp.run()
