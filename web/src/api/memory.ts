/** Typed fetch wrapper for the read-only `/api/memory/*` routes
 * (`server/routers/memory.py`, docs/plans/memory-ui.md §设计要点 1).
 *
 * Types are hand-rolled rather than pulled from `contracts.ts` — same call
 * as `api/tasks.ts`: a small, stable surface (4 GET routes) is cheaper to
 * keep in sync by hand than to regenerate for. There is no write path here
 * and there must never be one; the Memory page's only way to change a
 * memory file is to delegate to the owning agent (see `buildCorrectionPrompt`
 * in `components/memory/memoryPresentation.ts`).
 */

export interface MemoryFileMeta {
  file: string;
  name: string | null;
  description: string | null;
  type: string | null;
}

export interface MemoryListResponse {
  agent_id: string;
  index: MemoryFileMeta | null;
  files: MemoryFileMeta[];
}

export interface MemorySearchHit {
  agent_id: string;
  file: string;
  name: string | null;
  type: string | null;
  snippet: string;
}

export interface MemorySearchResponse {
  query: string;
  hits: MemorySearchHit[];
}

export interface MemoryGraphNode {
  id: string;
  file: string | null;
  description: string | null;
  type: string | null;
  ghost: boolean;
}

export interface MemoryGraphEdge {
  source: string;
  target: string;
}

export interface MemoryGraphResponse {
  agent_id: string;
  nodes: MemoryGraphNode[];
  edges: MemoryGraphEdge[];
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

export const memoryApi = {
  list: (token: string, agentId: string) =>
    getJson<MemoryListResponse>(
      token,
      `/api/memory/${encodeURIComponent(agentId)}`
    ),

  file: async (token: string, agentId: string, name: string): Promise<string> => {
    const res = await fetch(
      `${API()}/api/memory/${encodeURIComponent(agentId)}/file?name=${encodeURIComponent(name)}`,
      { headers: authHeaders(token) }
    );
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.text();
  },

  search: (token: string, q: string) =>
    getJson<MemorySearchResponse>(
      token,
      `/api/memory/search?q=${encodeURIComponent(q)}`
    ),

  graph: (token: string, agentId: string) =>
    getJson<MemoryGraphResponse>(
      token,
      `/api/memory/${encodeURIComponent(agentId)}/graph`
    ),
};
