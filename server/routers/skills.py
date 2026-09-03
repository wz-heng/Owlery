"""REST endpoints for the skill candidate review queue
(experience-consolidation.md §3.4/§5).

Proposing is session-scoped (`/api/sessions/{sid}/skills/candidates`,
mirroring the research router) because a candidate always has a proposing
session. Listing/reviewing is global (`/api/skills/candidates`) — the human
reviewer's queue isn't scoped to any one session.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import verify_token
from ..skill_registry import SkillRegistryError, skill_registry

router = APIRouter(tags=["skills"])


async def _run(call: Any) -> Any:
    try:
        return await call
    except SkillRegistryError as exc:
        code = {
            "not_found": 404,
            "validation": 422,
            "conflict": 409,
        }.get(exc.code, 500)
        raise HTTPException(code, {"code": exc.code, "message": str(exc)}) from exc


class ProposeRequest(BaseModel):
    slug: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    body_markdown: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    task_id: str | None = None
    run_id: str | None = None
    scope: str = "agent+repo"
    bundle_files: dict[str, str] | None = None


class ReviewRequest(BaseModel):
    review_note: str | None = None


class ApproveRequest(BaseModel):
    review_note: str | None = None
    # Reviewer override of the proposer's chosen scope (experience-
    # consolidation-v2.md §3③: "提名时选定,人审可改").
    scope: str | None = None


@router.post("/api/sessions/{session_id}/skills/candidates", status_code=201)
async def propose_candidate(
    session_id: str, req: ProposeRequest, _: str = Depends(verify_token)
) -> dict[str, Any]:
    return await _run(
        skill_registry.propose(
            session_id=session_id,
            slug=req.slug,
            title=req.title,
            description=req.description,
            body_markdown=req.body_markdown,
            rationale=req.rationale,
            task_id=req.task_id,
            run_id=req.run_id,
            scope=req.scope,
            bundle_files=req.bundle_files,
        )
    )


@router.get("/api/skills/candidates")
async def list_candidates(
    status: str | None = None, _: str = Depends(verify_token)
) -> list[dict[str, Any]]:
    return await _run(skill_registry.list_candidates(status=status))


@router.get("/api/skills/candidates/{candidate_id}")
async def get_candidate(
    candidate_id: str, _: str = Depends(verify_token)
) -> dict[str, Any]:
    return await _run(skill_registry.diff(candidate_id))


@router.post("/api/skills/candidates/{candidate_id}/approve")
async def approve_candidate(
    candidate_id: str, req: ApproveRequest, _: str = Depends(verify_token)
) -> dict[str, Any]:
    return await _run(
        skill_registry.approve(
            candidate_id, review_note=req.review_note, scope=req.scope
        )
    )


@router.post("/api/skills/candidates/{candidate_id}/reject")
async def reject_candidate(
    candidate_id: str, req: ReviewRequest, _: str = Depends(verify_token)
) -> dict[str, Any]:
    return await _run(
        skill_registry.reject(candidate_id, review_note=req.review_note or "")
    )
