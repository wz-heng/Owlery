/**
 * Renderer tests for SkillCandidatesPage — the human review queue for
 * experience consolidation (experience-consolidation.md §3.4/§5). Covers
 * the pending -> diff -> approve/reject shape: a pending candidate is
 * listed, its diff loads, approve/reject POST and refresh the list.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { SkillCandidatesPage } from "./SkillCandidatesPage";
import { useSessionStore } from "../stores/sessionStore";
import type { SkillCandidate } from "../api/skills";

function candidate(overrides: Partial<SkillCandidate> = {}): SkillCandidate {
  return {
    id: "cand-1",
    slug: "hermes-pr-flow",
    title: "Hermes PR flow",
    description: "How to open a PR against an external repo.",
    body_markdown: "---\nname: hermes-pr-flow\n---\nBody.\n",
    repository: "/repo",
    rationale: "Walked this once, hit real friction, will recur.",
    status: "pending",
    proposed_by_agent_id: "agent-1",
    proposed_by_session_id: "session-1",
    task_id: "task-1",
    run_id: "run-1",
    reviewed_at: null,
    review_note: null,
    landed_path: null,
    landed_branch: null,
    landed_commit: null,
    use_count: 0,
    last_used_at: null,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
    ...overrides,
  };
}

let fetchMock: ReturnType<typeof vi.fn>;
let store: Record<string, SkillCandidate[]>;

beforeEach(() => {
  useSessionStore.setState({ token: "tok" });
  store = { pending: [candidate()], approved: [], rejected: [] };
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const u = new URL(url);
    if (u.pathname === "/api/skills/candidates" && method === "GET") {
      const status = (u.searchParams.get("status") ?? "pending") as
        | "pending" | "approved" | "rejected";
      return new Response(JSON.stringify(store[status] ?? []), {
        status: 200, headers: { "content-type": "application/json" },
      });
    }
    const detailMatch = u.pathname.match(/^\/api\/skills\/candidates\/([^/]+)$/);
    if (detailMatch && method === "GET") {
      const id = detailMatch[1];
      const all = [...store.pending, ...store.approved, ...store.rejected];
      const found = all.find((c) => c.id === id) ?? candidate({ id });
      return new Response(
        JSON.stringify({ candidate: found, diff: "+new line\n" }),
        { status: 200, headers: { "content-type": "application/json" } }
      );
    }
    const approveMatch = u.pathname.match(/^\/api\/skills\/candidates\/([^/]+)\/approve$/);
    if (approveMatch && method === "POST") {
      const id = approveMatch[1];
      store.pending = store.pending.filter((c) => c.id !== id);
      const approved = candidate({ id, status: "approved", landed_path: ".claude/skills/hermes-pr-flow/SKILL.md" });
      store.approved = [...store.approved, approved];
      return new Response(JSON.stringify(approved), {
        status: 200, headers: { "content-type": "application/json" },
      });
    }
    const rejectMatch = u.pathname.match(/^\/api\/skills\/candidates\/([^/]+)\/reject$/);
    if (rejectMatch && method === "POST") {
      const id = rejectMatch[1];
      const body = JSON.parse((init?.body as string) ?? "{}");
      store.pending = store.pending.filter((c) => c.id !== id);
      const rejected = candidate({ id, status: "rejected", review_note: body.review_note });
      store.rejected = [...store.rejected, rejected];
      return new Response(JSON.stringify(rejected), {
        status: 200, headers: { "content-type": "application/json" },
      });
    }
    return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("SkillCandidatesPage", () => {
  it("lists a pending candidate and shows its diff on selection", async () => {
    await act(async () => {
      render(<SkillCandidatesPage />);
    });
    await screen.findByText("Hermes PR flow");
    await waitFor(() => expect(screen.getByText(/new line/)).toBeTruthy());
    expect(screen.getByText(/Walked this once/)).toBeTruthy();
  });

  it("approving a pending candidate removes it from the pending list", async () => {
    await act(async () => {
      render(<SkillCandidatesPage />);
    });
    await screen.findByText("Hermes PR flow");
    const approveBtn = await screen.findByRole("button", { name: "Approve" });
    await act(async () => {
      fireEvent.click(approveBtn);
    });
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([u]) => String(u).endsWith("/approve"))
      ).toBe(true)
    );
    await waitFor(() => expect(screen.queryByText("Hermes PR flow")).toBeNull());
  });

  it("reject is disabled until a note is entered, then POSTs it", async () => {
    await act(async () => {
      render(<SkillCandidatesPage />);
    });
    await screen.findByText("Hermes PR flow");
    const rejectBtn = screen.getByRole("button", { name: /Reject/ });
    expect(rejectBtn).toBeDisabled();

    const noteInput = screen.getByLabelText("Reason for rejecting");
    fireEvent.change(noteInput, { target: { value: "not general enough" } });
    expect(rejectBtn).not.toBeDisabled();

    await act(async () => {
      fireEvent.click(rejectBtn);
    });
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([u]) => String(u).endsWith("/reject"));
      expect(call).toBeTruthy();
      const body = JSON.parse((call![1] as RequestInit).body as string);
      expect(body.review_note).toBe("not general enough");
    });
  });

  it("switching to the approved tab shows use_count", async () => {
    store.pending = [];
    store.approved = [candidate({ id: "cand-2", status: "approved", use_count: 3 })];
    await act(async () => {
      render(<SkillCandidatesPage />);
    });
    await screen.findByText("No pending candidates.");
    const approvedTab = screen.getByRole("tab", { name: "approved" });
    await act(async () => {
      fireEvent.click(approvedTab);
    });
    await screen.findByText(/used 3×/);
  });
});
