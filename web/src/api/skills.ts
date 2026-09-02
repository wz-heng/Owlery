/** Typed fetch wrapper for the skill candidate review queue
 * (`server/routers/skills.py`, docs/plans/experience-consolidation.md
 * §3.4/§5). Hand-rolled types, same call as `api/memory.ts` and
 * `api/tasks.ts` — a small, stable surface is cheaper to keep in sync by
 * hand than to regenerate for.
 */

export type SkillCandidateStatus = "pending" | "approved" | "rejected";

export interface SkillCandidate {
  id: string;
  slug: string;
  title: string;
  description: string;
  body_markdown: string;
  repository: string;
  rationale: string;
  status: SkillCandidateStatus;
  proposed_by_agent_id: string | null;
  proposed_by_session_id: string | null;
  task_id: string | null;
  run_id: string | null;
  reviewed_at: string | null;
  review_note: string | null;
  landed_path: string | null;
  landed_branch: string | null;
  landed_commit: string | null;
  use_count: number;
  last_used_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface SkillCandidateDetail {
  candidate: SkillCandidate;
  diff: string;
}

const API = () => window.location.origin;

function authHeaders(token: string): HeadersInit {
  return { Authorization: `Bearer ${token}` };
}

async function getJson<T>(token: string, path: string): Promise<T> {
  const res = await fetch(`${API()}${path}`, { headers: authHeaders(token) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

async function postJson<T>(
  token: string, path: string, body: unknown
): Promise<T> {
  const res = await fetch(`${API()}${path}`, {
    method: "POST",
    headers: { ...authHeaders(token), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

export const skillsApi = {
  listCandidates: (token: string, status?: SkillCandidateStatus) =>
    getJson<SkillCandidate[]>(
      token,
      `/api/skills/candidates${status ? `?status=${status}` : ""}`
    ),

  getCandidate: (token: string, candidateId: string) =>
    getJson<SkillCandidateDetail>(
      token,
      `/api/skills/candidates/${encodeURIComponent(candidateId)}`
    ),

  approve: (token: string, candidateId: string, reviewNote?: string) =>
    postJson<SkillCandidate>(
      token,
      `/api/skills/candidates/${encodeURIComponent(candidateId)}/approve`,
      { review_note: reviewNote ?? null }
    ),

  reject: (token: string, candidateId: string, reviewNote: string) =>
    postJson<SkillCandidate>(
      token,
      `/api/skills/candidates/${encodeURIComponent(candidateId)}/reject`,
      { review_note: reviewNote }
    ),
};
