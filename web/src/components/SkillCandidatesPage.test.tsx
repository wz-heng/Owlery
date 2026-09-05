/**
 * Renderer tests for SkillCandidatesPage — the human review queue for
 * experience consolidation (experience-consolidation.md §3.4/§5,
 * experience-consolidation-v2.md §3②). Covers the pending -> diff ->
 * approve/reject shape and the v2 evidence chain: task/run/session links,
 * lint results, a per-file diff over the bundle, invocation history, and
 * the reviewer's scope override on approve.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { SkillCandidatesPage } from "./SkillCandidatesPage";
import { useSessionStore } from "../stores/sessionStore";
import type { SkillCandidate, SkillCandidateDetail, SkillInvocation } from "../api/skills";

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
    scope: "agent+repo",
    bundle_files: null,
    lint_results: {
      frontmatter_valid: true,
      slug_conflict: false,
      bundle_refs_valid: true,
      issues: [],
    },
    materialized_backends: null,
    superseded_at: null,
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
    ...overrides,
  };
}

function detailFor(
  c: SkillCandidate, invocations: SkillInvocation[] = []
): SkillCandidateDetail {
  return {
    candidate: c,
    diff: "+new line\n",
    file_diffs: { "SKILL.md": "+new line\n" },
    task: c.task_id ? { id: c.task_id, board_id: "board-1", title: "Ship the thing", status: "done" } : null,
    run: c.run_id ? { id: c.run_id, task_id: c.task_id ?? "", attempt_no: 1, state: "completed" } : null,
    session: c.proposed_by_session_id
      ? { id: c.proposed_by_session_id, backend: "claude-code", archived: false }
      : null,
    invocations,
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
        JSON.stringify(detailFor(found)),
        { status: 200, headers: { "content-type": "application/json" } }
      );
    }
    const approveMatch = u.pathname.match(/^\/api\/skills\/candidates\/([^/]+)\/approve$/);
    if (approveMatch && method === "POST") {
      const id = approveMatch[1];
      const body = JSON.parse((init?.body as string) ?? "{}");
      store.pending = store.pending.filter((c) => c.id !== id);
      const approved = candidate({
        id,
        status: "approved",
        landed_path: ".claude/skills/hermes-pr-flow/SKILL.md",
        scope: body.scope ?? "agent+repo",
        materialized_backends: ["claude", "codex"],
      });
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
    await screen.findByRole("heading", { name: "Hermes PR flow" });
    await waitFor(() => expect(screen.getByText(/new line/)).toBeTruthy());
    expect(screen.getByText(/Walked this once/)).toBeTruthy();
  });

  it("shows the evidence chain: task, run, session, and lint results", async () => {
    await act(async () => {
      render(<SkillCandidatesPage />);
    });
    await screen.findByRole("heading", { name: "Hermes PR flow" });
    await screen.findByText(/Ship the thing/);
    expect(screen.getByText(/attempt 1/)).toBeTruthy();
    expect(screen.getByText("claude-code")).toBeTruthy();
    expect(screen.getByText(/frontmatter: valid/)).toBeTruthy();
    expect(screen.getByText(/slug conflict: no/)).toBeTruthy();
  });

  it("surfaces lint issues when bundle refs are dangling", async () => {
    store.pending = [
      candidate({
        lint_results: {
          frontmatter_valid: true,
          slug_conflict: true,
          bundle_refs_valid: false,
          issues: ["body_markdown references 'scripts/run.sh', which is not in bundle_files"],
        },
      }),
    ];
    await act(async () => {
      render(<SkillCandidatesPage />);
    });
    await screen.findByRole("heading", { name: "Hermes PR flow" });
    expect(screen.getByText(/slug conflict: yes/)).toBeTruthy();
    expect(screen.getByText(/not in bundle_files/)).toBeTruthy();
  });

  it("renders a file tab per bundle file and switches the diff shown", async () => {
    const withBundle = candidate({
      bundle_files: { "scripts/run.sh": "#!/bin/sh\necho hi\n" },
    });
    store.pending = [withBundle];
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const u = new URL(url);
      if (u.pathname === "/api/skills/candidates" && method === "GET") {
        return new Response(JSON.stringify([withBundle]), {
          status: 200, headers: { "content-type": "application/json" },
        });
      }
      if (u.pathname === `/api/skills/candidates/${withBundle.id}` && method === "GET") {
        const detail = detailFor(withBundle);
        detail.file_diffs = {
          "SKILL.md": "+skill diff\n",
          "scripts/run.sh": "+script diff\n",
        };
        detail.diff = detail.file_diffs["SKILL.md"];
        return new Response(JSON.stringify(detail), {
          status: 200, headers: { "content-type": "application/json" },
        });
      }
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    });

    await act(async () => {
      render(<SkillCandidatesPage />);
    });
    await screen.findByRole("heading", { name: "Hermes PR flow" });
    await waitFor(() => expect(screen.getByText(/skill diff/)).toBeTruthy());

    const fileTab = screen.getByRole("tab", { name: "scripts/run.sh" });
    await act(async () => {
      fireEvent.click(fileTab);
    });
    await waitFor(() => expect(screen.getByText(/script diff/)).toBeTruthy());
  });

  it("approving posts the selected scope override", async () => {
    await act(async () => {
      render(<SkillCandidatesPage />);
    });
    await screen.findByRole("heading", { name: "Hermes PR flow" });
    const scopeSelect = screen.getByLabelText("Land as");
    fireEvent.change(scopeSelect, { target: { value: "agent-global" } });

    const approveBtn = await screen.findByRole("button", { name: "Approve" });
    await act(async () => {
      fireEvent.click(approveBtn);
    });
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([u]) => String(u).endsWith("/approve"));
      expect(call).toBeTruthy();
      const body = JSON.parse((call![1] as RequestInit).body as string);
      expect(body.scope).toBe("agent-global");
    });
  });

  it("approving a pending candidate removes it from the pending list", async () => {
    await act(async () => {
      render(<SkillCandidatesPage />);
    });
    await screen.findByRole("heading", { name: "Hermes PR flow" });
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

  it("shows materialized backend badges for an approved candidate", async () => {
    store.pending = [];
    store.approved = [
      candidate({ id: "cand-2", status: "approved", materialized_backends: ["claude", "codex"] }),
    ];
    await act(async () => {
      render(<SkillCandidatesPage />);
    });
    await screen.findByText("No pending candidates.");
    const approvedTab = screen.getByRole("tab", { name: "approved" });
    await act(async () => {
      fireEvent.click(approvedTab);
    });
    await screen.findByRole("heading", { name: "Hermes PR flow" });
    expect(screen.getByText("claude")).toBeTruthy();
    expect(screen.getByText("codex")).toBeTruthy();
  });

  it("shows a superseded notice for an approved candidate a later approval relocated", async () => {
    store.pending = [];
    store.approved = [
      candidate({
        id: "cand-2", status: "approved",
        superseded_at: "2026-09-02T00:00:00Z",
      }),
    ];
    await act(async () => {
      render(<SkillCandidatesPage />);
    });
    await screen.findByText("No pending candidates.");
    const approvedTab = screen.getByRole("tab", { name: "approved" });
    await act(async () => {
      fireEvent.click(approvedTab);
    });
    await screen.findByRole("heading", { name: "Hermes PR flow" });
    expect(screen.getByText(/Superseded/)).toBeTruthy();
  });

  it("reject is disabled until a note is entered, then POSTs it", async () => {
    await act(async () => {
      render(<SkillCandidatesPage />);
    });
    await screen.findByRole("heading", { name: "Hermes PR flow" });
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

  it("shows invocation history for an approved candidate", async () => {
    store.pending = [];
    store.approved = [candidate({ id: "cand-2", status: "approved", use_count: 1 })];
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const u = new URL(url);
      if (u.pathname === "/api/skills/candidates" && method === "GET") {
        const status = (u.searchParams.get("status") ?? "pending") as
          | "pending" | "approved" | "rejected";
        return new Response(JSON.stringify(store[status] ?? []), {
          status: 200, headers: { "content-type": "application/json" },
        });
      }
      if (u.pathname === "/api/skills/candidates/cand-2" && method === "GET") {
        const detail = detailFor(store.approved[0], [
          {
            id: "inv-1", candidate_id: "cand-2", agent_id: "agent-1",
            repository: "/repo", session_id: "session-1", task_id: "task-9",
            run_id: "run-9", backend: "claude-code",
            used_at: "2026-09-03T00:00:00Z",
          },
        ]);
        return new Response(JSON.stringify(detail), {
          status: 200, headers: { "content-type": "application/json" },
        });
      }
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    });
    await act(async () => {
      render(<SkillCandidatesPage />);
    });
    await screen.findByText("No pending candidates.");
    const approvedTab = screen.getByRole("tab", { name: "approved" });
    await act(async () => {
      fireEvent.click(approvedTab);
    });
    await screen.findByRole("heading", { name: "Hermes PR flow" });
    await screen.findByText("Invocation history");
    expect(screen.getByText(/run run-9/)).toBeTruthy();
  });

  it("shows the prior version's invocation history for a PENDING replacement candidate", async () => {
    // The regression this guards: `list_skill_invocations` used to filter
    // by the pending candidate's own id, which never has invocations of
    // its own (it's never been approved/loaded yet) — the review-page
    // detail now joins on lineage (slug+scope+agent) instead, so a
    // reviewer deciding on a REPLACEMENT sees the version it would replace
    // actually got used.
    const pending = candidate({ id: "cand-3", status: "pending" });
    store.pending = [pending];
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const u = new URL(url);
      if (u.pathname === "/api/skills/candidates" && method === "GET") {
        return new Response(JSON.stringify([pending]), {
          status: 200, headers: { "content-type": "application/json" },
        });
      }
      if (u.pathname === "/api/skills/candidates/cand-3" && method === "GET") {
        const detail = detailFor(pending, [
          {
            id: "inv-old", candidate_id: "cand-1", agent_id: "agent-1",
            repository: "/repo", session_id: "session-1", task_id: null,
            run_id: "run-old", backend: "claude-code",
            used_at: "2026-08-01T00:00:00Z",
          },
        ]);
        return new Response(JSON.stringify(detail), {
          status: 200, headers: { "content-type": "application/json" },
        });
      }
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    });
    await act(async () => {
      render(<SkillCandidatesPage />);
    });
    await screen.findByRole("heading", { name: "Hermes PR flow" });
    await screen.findByText(/Invocation history/);
    expect(screen.getByText(/\(prior version\)/)).toBeTruthy();
    expect(screen.getByText(/run run-old/)).toBeTruthy();
    // Not approved — the landed-path/branch/use-count block must stay hidden.
    expect(screen.queryByText(/landed at/)).toBeNull();
  });

  it("surfaces the server's error message when approve fails", async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const u = new URL(url);
      if (u.pathname === "/api/skills/candidates" && method === "GET") {
        return new Response(JSON.stringify(store.pending), {
          status: 200, headers: { "content-type": "application/json" },
        });
      }
      if (u.pathname === "/api/skills/candidates/cand-1" && method === "GET") {
        return new Response(JSON.stringify(detailFor(candidate())), {
          status: 200, headers: { "content-type": "application/json" },
        });
      }
      if (u.pathname === "/api/skills/candidates/cand-1/approve" && method === "POST") {
        return new Response(
          JSON.stringify({
            detail: {
              code: "validation",
              message: "approving this candidate would not change anything on disk",
            },
          }),
          { status: 422, headers: { "content-type": "application/json" } }
        );
      }
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    });
    await act(async () => {
      render(<SkillCandidatesPage />);
    });
    await screen.findByRole("heading", { name: "Hermes PR flow" });
    const approveBtn = await screen.findByRole("button", { name: "Approve" });
    await act(async () => {
      fireEvent.click(approveBtn);
    });
    await screen.findByText(/would not change anything on disk/);
  });

  it("clicking the task link calls onOpenTask", async () => {
    const onOpenTask = vi.fn();
    await act(async () => {
      render(<SkillCandidatesPage onOpenTask={onOpenTask} />);
    });
    await screen.findByRole("heading", { name: "Hermes PR flow" });
    const taskLink = await screen.findByText(/Ship the thing/);
    await act(async () => {
      fireEvent.click(taskLink);
    });
    expect(onOpenTask).toHaveBeenCalledWith("task-1");
  });
});
