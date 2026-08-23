/**
 * Integration-level tests for the top-level Memory page (memory-ui.md
 * §设计要点 2-4): agent switching, type filtering, cross-agent search jumps,
 * and the "纠错" delegation entry point (creates a session + primes the
 * chat composer via the store — no direct memory write ever happens here).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { MemoryPage } from "./MemoryPage";
import { useSessionStore, type Agent } from "../stores/sessionStore";
import { buildCorrectionPrompt } from "./memory/memoryPresentation";

const agents = [
  { id: "agent-1", name: "Athena" } as Agent,
  { id: "agent-2", name: "Snape" } as Agent,
];

const listByAgent: Record<string, unknown> = {
  "agent-1": {
    agent_id: "agent-1",
    index: { file: "MEMORY.md", name: null, description: null, type: null },
    files: [
      { file: "note-a.md", name: "Note A", description: "desc a", type: "user" },
      { file: "note-b.md", name: "Note B", description: "desc b", type: "feedback" },
    ],
  },
  "agent-2": {
    agent_id: "agent-2",
    index: null,
    // "other.md" sorts/lists first on purpose: a regression of the
    // selectedFile-gets-clobbered bug (Snape review) would fall back to
    // `files[0]` here and show the WRONG file, catching it instead of
    // coincidentally passing the way a single-file fixture would.
    files: [
      { file: "other.md", name: "Other", description: null, type: "project" },
      { file: "solo.md", name: "Solo", description: null, type: "project" },
    ],
  },
};

const graphByAgent: Record<string, unknown> = {
  "agent-1": {
    agent_id: "agent-1",
    nodes: [
      { id: "note-a", file: "note-a.md", description: null, type: "user", ghost: false },
    ],
    edges: [],
  },
  "agent-2": { agent_id: "agent-2", nodes: [], edges: [] },
};

const fileContent: Record<string, Record<string, string>> = {
  "agent-1": {
    "MEMORY.md": "# Index\n\nSee [[note-a]] for details.",
    "note-a.md": "Note A body",
    "note-b.md": "Note B body",
  },
  "agent-2": {
    "other.md": "Other body",
    "solo.md": "Solo body",
  },
};

const searchHits = [
  { agent_id: "agent-2", file: "solo.md", name: "Solo", type: "project", snippet: "…solo body…" },
];

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

let fetchMock: ReturnType<typeof vi.fn>;
let newSessionCounter = 0;

beforeEach(() => {
  newSessionCounter = 0;
  useSessionStore.setState({
    token: "tok",
    agents,
    activeAgentId: null,
    sessions: [],
    composerDrafts: {},
  });

  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const method = init?.method ?? "GET";

    if (method === "POST" && /\/api\/agents\/([^/]+)\/sessions$/.test(url)) {
      const agentId = url.match(/\/api\/agents\/([^/]+)\/sessions$/)![1];
      newSessionCounter += 1;
      const id = `new-session-${newSessionCounter}`;
      return jsonResponse({ id, agent_id: agentId, name: "correction session" });
    }
    if (url.includes("/api/memory/search")) {
      const q = new URL(url).searchParams.get("q") ?? "";
      return jsonResponse({ query: q, hits: searchHits });
    }
    const graphMatch = url.match(/\/api\/memory\/([^/?]+)\/graph/);
    if (graphMatch) {
      const agentId = decodeURIComponent(graphMatch[1]);
      return jsonResponse(graphByAgent[agentId] ?? { agent_id: agentId, nodes: [], edges: [] });
    }
    const fileMatch = url.match(/\/api\/memory\/([^/?]+)\/file\?name=([^&]+)/);
    if (fileMatch) {
      const agentId = decodeURIComponent(fileMatch[1]);
      const name = decodeURIComponent(fileMatch[2]);
      return new Response(fileContent[agentId]?.[name] ?? "", { status: 200 });
    }
    const listMatch = url.match(/\/api\/memory\/([^/?]+)$/);
    if (listMatch) {
      const agentId = decodeURIComponent(listMatch[1]);
      if (agentId === "agent-3") {
        return new Response("boom", { status: 500 });
      }
      return jsonResponse(
        listByAgent[agentId] ?? { agent_id: agentId, index: null, files: [] }
      );
    }
    throw new Error(`unexpected fetch: ${method} ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("MemoryPage", () => {
  it("loads the first agent's memory on mount: file list + index reading view", async () => {
    await act(async () => {
      render(<MemoryPage />);
    });
    await screen.findByText("Note A");
    expect(screen.getByText("Note B")).toBeTruthy();
    await screen.findByRole("heading", { name: "Index" });
  });

  it("renders a resolvable wikilink from the index page as clickable and navigates on click", async () => {
    await act(async () => {
      render(<MemoryPage />);
    });
    await screen.findByRole("heading", { name: "Index" });
    const link = await screen.findByText("note-a");
    expect(link.tagName).toBe("A");
    await act(async () => {
      fireEvent.click(link);
    });
    await screen.findByText("Note A body");
  });

  it("switches agents and refetches that agent's file list", async () => {
    await act(async () => {
      render(<MemoryPage />);
    });
    await screen.findByText("Note A");

    await act(async () => {
      fireEvent.click(screen.getByText("Snape"));
    });

    // Switching agent via the switcher (as opposed to jumping to a specific
    // search hit) resets to that agent's default file — the first one, since
    // agent-2 has no MEMORY.md index.
    await screen.findByRole("button", { name: "Other" });
    expect(screen.getByRole("button", { name: "Solo" })).toBeTruthy();
    expect(screen.queryByText("Note A")).toBeNull();
    await screen.findByText("Other body");
  });

  it("filters the file list by the active type chips", async () => {
    await act(async () => {
      render(<MemoryPage />);
    });
    await screen.findByText("Note A");
    expect(screen.getByText("Note B")).toBeTruthy();

    await act(async () => {
      fireEvent.click(screen.getByText("Feedback"));
    });

    expect(screen.queryByText("Note A")).toBeNull();
    expect(screen.getByText("Note B")).toBeTruthy();
  });

  it("searches across agents and jumps to the hit's agent + file", async () => {
    await act(async () => {
      render(<MemoryPage />);
    });
    await screen.findByText("Note A");

    const searchBox = screen.getByPlaceholderText("Search all agents' memory");
    fireEvent.change(searchBox, { target: { value: "solo" } });
    await act(async () => {
      fireEvent.submit(searchBox.closest("form")!);
    });

    const hit = await screen.findByText("Solo", { selector: "span" });
    await act(async () => {
      fireEvent.click(hit.closest("button")!);
    });

    // Must land on the hit's exact file, not agent-2's default/first file
    // (a regression here previously fell back to "other.md" — Snape review).
    await screen.findByText("Solo body");
    expect(screen.queryByText("Other body")).toBeNull();
    expect(screen.queryByPlaceholderText("Search all agents' memory")).toHaveValue("");
  });

  it("clears stale data and hides the correct button when switching to an agent whose fetch fails", async () => {
    useSessionStore.setState({
      agents: [...agents, { id: "agent-3", name: "Voldemort" } as Agent],
    });
    await act(async () => {
      render(<MemoryPage />);
    });
    await screen.findByRole("heading", { name: "Index" });

    await act(async () => {
      fireEvent.click(screen.getByText("Voldemort"));
    });

    // Stale agent-1 content must not linger once we've switched away, even
    // though agent-3's fetch is about to fail.
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Index" })).toBeNull()
    );
    expect(screen.queryByText("Note A")).toBeNull();
    // With nothing loaded for agent-3, there's nothing to file a correction
    // against — the button must not render (it would otherwise silently
    // file a correction naming a stale agent-1 file against agent-3).
    expect(screen.queryByText("纠错")).toBeNull();
  });

  it("only applies the most recently issued search's results (a slow earlier response can't clobber a faster later one)", async () => {
    await act(async () => {
      render(<MemoryPage />);
    });
    await screen.findByText("Note A");

    let resolveSlow!: (r: Response) => void;
    const slow = new Promise<Response>((resolve) => {
      resolveSlow = resolve;
    });
    // The next two `fetch` calls this test triggers are exactly the two
    // search submits below — queue their responses in issue order.
    fetchMock.mockImplementationOnce(async () => slow);
    fetchMock.mockImplementationOnce(async () =>
      jsonResponse({
        query: "fast",
        hits: [
          { agent_id: "agent-2", file: "solo.md", name: "Solo", type: "project", snippet: "fast hit" },
        ],
      })
    );

    const searchBox = screen.getByPlaceholderText("Search all agents' memory");
    const form = searchBox.closest("form")!;

    fireEvent.change(searchBox, { target: { value: "slow" } });
    await act(async () => {
      fireEvent.submit(form);
    });

    fireEvent.change(searchBox, { target: { value: "fast" } });
    await act(async () => {
      fireEvent.submit(form);
    });

    await screen.findByText("fast hit");

    // The slow (earlier-issued) response finally lands — it must be a no-op.
    await act(async () => {
      resolveSlow(jsonResponse({ query: "slow", hits: [] }));
      await slow;
    });

    expect(screen.getByText("fast hit")).toBeTruthy();
  });

  it("starts a correction session and primes the composer draft, without writing to the memory file", async () => {
    const onOpenSession = vi.fn();
    await act(async () => {
      render(<MemoryPage onOpenSession={onOpenSession} />);
    });
    await screen.findByRole("heading", { name: "Index" });

    await act(async () => {
      fireEvent.click(screen.getByText("纠错"));
    });

    await waitFor(() => expect(onOpenSession).toHaveBeenCalledWith("new-session-1"));

    // No PATCH/PUT/DELETE ever hits the memory router — only the ordinary
    // session-create POST used by every other "start a chat" entry point.
    const memoryWriteAttempts = fetchMock.mock.calls.filter(([, init]) => {
      const method = (init as RequestInit | undefined)?.method;
      return method && method !== "GET" && method !== "POST";
    });
    expect(memoryWriteAttempts).toHaveLength(0);

    const draft = useSessionStore.getState().composerDrafts["new-session-1"];
    expect(draft).toBe(buildCorrectionPrompt("MEMORY.md"));
  });

  it("refetches the current agent's list and graph on manual refresh", async () => {
    await act(async () => {
      render(<MemoryPage />);
    });
    await screen.findByText("Note A");
    const callsBefore = fetchMock.mock.calls.length;

    await act(async () => {
      fireEvent.click(screen.getByLabelText("Refresh"));
    });

    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(callsBefore));
  });
});
