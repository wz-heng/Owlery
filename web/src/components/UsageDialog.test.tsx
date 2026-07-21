/**
 * UsageDialog (usage-tracking.md §6): renders the /api/usage/summary
 * aggregation as one table — agent keys resolve to names from the store,
 * codex's null cost shows an em-dash, the totals footer sums the window,
 * and switching the group-by refetches with the new query param.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { UsageDialog, type UsageSummary } from "./UsageDialog";
import { useSessionStore, type Agent } from "../stores/sessionStore";

const agent: Agent = {
  id: "agent-1",
  name: "Octo",
} as Agent;

function summary(overrides: Partial<UsageSummary> = {}): UsageSummary {
  return {
    group_by: "agent",
    rows: [
      {
        key: "agent-1",
        turns: 3,
        cost: 1.5,
        input_tokens: 1000,
        cache_read_tokens: 200,
        cache_creation_tokens: 50,
        output_tokens: 4000,
        reasoning_tokens: 0,
        total_tokens: 5250,
      },
      {
        key: "agent-2",
        turns: 1,
        cost: null,
        input_tokens: 10,
        cache_read_tokens: 0,
        cache_creation_tokens: 0,
        output_tokens: 5,
        reasoning_tokens: 0,
        total_tokens: 15,
      },
    ],
    totals: {
      turns: 4,
      cost: 1.5,
      input_tokens: 1010,
      cache_read_tokens: 200,
      cache_creation_tokens: 50,
      output_tokens: 4005,
      reasoning_tokens: 0,
      total_tokens: 5265,
    },
    ...overrides,
  };
}

let fetchMock: ReturnType<typeof vi.fn>;
let payload: UsageSummary;

beforeEach(() => {
  payload = summary();
  useSessionStore.setState({
    token: "tok",
    agents: [agent],
    sessions: [],
    archivedSessions: [],
  });
  fetchMock = vi.fn(async () => {
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("UsageDialog", () => {
  it("renders rows with resolved agent names, em-dash cost and totals", async () => {
    await act(async () => {
      render(<UsageDialog open onOpenChange={() => {}} />);
    });

    // agent-1 resolves to its store name; unknown agent-2 shows an id tail
    // The agent label is now its wax seal + name (no emoji); the name
    // renders in its own span next to the aria-hidden seal.
    expect(await screen.findByText("Octo")).toBeInTheDocument();
    expect(screen.getByText("agent-2…")).toBeInTheDocument();
    // codex-style null cost renders as an em-dash, real cost as $x.xxxx
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.getAllByText("$1.5000").length).toBe(2); // row + totals
    // token numbers are locale-formatted; totals footer present
    expect(screen.getAllByText("5,250").length).toBe(1);
    expect(screen.getByText("Total")).toBeInTheDocument();
    expect(screen.getByText("5,265")).toBeInTheDocument();
    // default fetch carries group_by=agent + a since bound (30-day window)
    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain("/api/usage/summary?");
    expect(url).toContain("group_by=agent");
    expect(url).toContain("since=");
  });

  it("switches grouping and refetches; All time drops the since bound", async () => {
    await act(async () => {
      render(<UsageDialog open onOpenChange={() => {}} />);
    });
    await screen.findByText("Octo");

    payload = summary({
      group_by: "day",
      rows: [{ ...summary().rows[0], key: "2026-07-01" }],
    });
    await act(async () => {
      fireEvent.click(screen.getByText("Day"));
    });
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((c) => String(c[0]));
      expect(urls.some((u) => u.includes("group_by=day"))).toBe(true);
    });
    // day keys render verbatim — no id resolution
    expect(await screen.findByText("2026-07-01")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByText("All time"));
    });
    await waitFor(() => {
      const last = String(fetchMock.mock.calls.at(-1)?.[0]);
      expect(last).toContain("group_by=day");
      expect(last).not.toContain("since=");
    });
  });

  it("shows the empty state when the window has no rows", async () => {
    payload = summary({ rows: [], totals: { ...summary().totals, turns: 0 } });
    await act(async () => {
      render(<UsageDialog open onOpenChange={() => {}} />);
    });
    expect(
      await screen.findByText(/No usage recorded in this window/)
    ).toBeInTheDocument();
  });

  it("does not fetch while closed", async () => {
    await act(async () => {
      render(<UsageDialog open={false} onOpenChange={() => {}} />);
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("aborts the previous request and drops its stale response on switch", async () => {
    // First request hangs; we resolve it manually AFTER switching group-by.
    let resolveFirst!: (r: Response) => void;
    fetchMock.mockImplementationOnce(
      (_url: string, init: RequestInit) =>
        new Promise<Response>((res) => {
          resolveFirst = res;
          void init;
        })
    );
    await act(async () => {
      render(<UsageDialog open onOpenChange={() => {}} />);
    });

    payload = summary({
      group_by: "day",
      rows: [{ ...summary().rows[0], key: "2026-07-01" }],
    });
    await act(async () => {
      fireEvent.click(screen.getByText("Day"));
    });
    expect(await screen.findByText("2026-07-01")).toBeInTheDocument();

    // Switching aborted the first request's signal…
    const firstInit = fetchMock.mock.calls[0][1] as RequestInit;
    expect((firstInit.signal as AbortSignal).aborted).toBe(true);

    // …and its late (agent-shaped) response must not clobber the day table.
    await act(async () => {
      resolveFirst(
        new Response(JSON.stringify(summary()), {
          status: 200,
          headers: { "content-type": "application/json" },
        })
      );
    });
    expect(screen.queryByText("Octo")).not.toBeInTheDocument();
    expect(screen.getByText("2026-07-01")).toBeInTheDocument();
  });
});
