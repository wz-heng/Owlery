"""REST endpoint for the `tbexec` MCP tool's decision relay.

The `run`/`done` tools in `server/mcp_servers/tbexec.py` are a child process
of the `claude`/`codex` CLI and can't reach `AgentBeatsExamManager` directly
— same reachability constraint as `bg.py`/`ask.py` — so they POST here.

Auth: same bearer token as every other API surface.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..agentbeats.manager import agentbeats_exam_manager
from ..auth import verify_token

router = APIRouter(prefix="/api/sessions", tags=["agentbeats"])


class TbexecDecisionRequest(BaseModel):
    kind: Literal["exec", "finish"]
    command: str | None = None
    timeout: int | None = None
    output: str | None = None


@router.post("/{session_id}/tbexec/decision")
async def post_tbexec_decision(
    session_id: str,
    req: TbexecDecisionRequest,
    _: str = Depends(verify_token),
) -> dict[str, Any]:
    decision: dict[str, Any] = {"kind": req.kind}
    if req.kind == "exec":
        if not req.command or not req.command.strip():
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "command is required for kind='exec'"
            )
        decision["command"] = req.command
        decision["timeout"] = req.timeout or 60
    else:
        decision["output"] = req.output or ""

    ok = agentbeats_exam_manager.record_decision(session_id, decision)
    if not ok:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No exam task waiting on a decision for this session this turn "
            "(already recorded once, or the session isn't an AgentBeats exam "
            "session).",
        )
    message = (
        "Recorded. End your turn now — you'll see the real result next turn."
        if req.kind == "exec"
        else "Recorded. The task is finished."
    )
    return {"message": message}
