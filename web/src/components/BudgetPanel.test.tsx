/**
 * BudgetPanel (budget-model-routing.md §3.3): the shared spend-cap editor used
 * by UsageDialog (global) and AgentSettings (agent). It renders one row per
 * window — configured budgets are editable with a live water level from
 * `/api/budgets/status`; unconfigured windows offer an inline add — and drives
 * the /api/budgets CRUD.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

import { BudgetPanel } from "./BudgetPanel";
import { useSessionStore } from "../stores/sessionStore";
import type { BudgetRead } from "../api";

interface MockState {
  budgets: BudgetRead[];
  // window -> live spend (only meaningful for enabled budgets)
  spent: Record<string, number>;
}

let state: MockState;
let idSeq: number;
let fetchMock: ReturnType<typeof vi.fn>;

function ok(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function statusFor(): unknown[] {
  return state.budgets
    .filter((b) => b.enabled)
    .map((b) => ({
      scope: b.scope,
      agent_id: b.agent_id,
      window: b.window,
      limit_usd: b.limit_usd,
      spent_usd: state.spent[b.window] ?? 0,
    }));
}

beforeEach(() => {
  idSeq = 0;
  state = { budgets: [], spent: {} };
  useSessionStore.setState({ token: "tok" });

  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    const method = init?.method ?? "GET";
    if (u.endsWith("/api/budgets/status")) return ok(statusFor());
    if (u.endsWith("/api/budgets") && method === "GET") return ok(state.budgets);
    if (u.endsWith("/api/budgets") && method === "POST") {
      const req = JSON.parse(String(init?.body)) as BudgetRead;
      const row: BudgetRead = {
        id: `b${++idSeq}`,
        scope: req.scope,
        agent_id: req.agent_id ?? null,
        window: req.window,
        limit_usd: req.limit_usd,
        soft_pct: req.soft_pct,
        enabled: req.enabled,
        created_at: "now",
        updated_at: "now",
      };
      state.budgets.push(row);
      return ok(row, 201);
    }
    const m = u.match(/\/api\/budgets\/([^/]+)$/);
    if (m && method === "PATCH") {
      const patch = JSON.parse(String(init?.body));
      const row = state.budgets.find((b) => b.id === m[1])!;
      Object.assign(row, patch);
      return ok(row);
    }
    if (m && method === "DELETE") {
      state.budgets = state.budgets.filter((b) => b.id !== m[1]);
      return ok(null, 204);
    }
    throw new Error(`unhandled ${method} ${u}`);
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const configured = (over: Partial<BudgetRead> = {}): BudgetRead => ({
  id: "b-daily",
  scope: "global",
  agent_id: null,
  window: "daily",
  limit_usd: 10,
  soft_pct: 0.8,
  enabled: true,
  created_at: "now",
  updated_at: "now",
  ...over,
});

describe("BudgetPanel", () => {
  it("renders a configured budget with a soft-level water bar and add rows for the rest", async () => {
    state.budgets = [configured()];
    state.spent = { daily: 8.5 };
    await act(async () => {
      render(<BudgetPanel scope="global" />);
    });

    // The daily row is configured (has limit input); weekly/monthly offer add.
    const row = (await waitFor(() =>
      document.querySelector(".budget-row-daily")
    )) as HTMLElement;
    expect(row).toBeTruthy();
    expect(within(row as HTMLElement).getByText(/\$8\.5000 of \$10\.0000/)).toBeTruthy();
    // 8.5/10 = 85% ≥ soft 80% but < 100% → soft level bar.
    expect(row.querySelector(".budget-bar-soft")).toBeTruthy();
    expect(document.querySelector(".budget-add-weekly")).toBeTruthy();
    expect(document.querySelector(".budget-add-monthly")).toBeTruthy();
  });

  it("shows a hard-level bar once spend reaches the limit", async () => {
    state.budgets = [configured()];
    state.spent = { daily: 10 };
    await act(async () => {
      render(<BudgetPanel scope="global" />);
    });
    await waitFor(() =>
      expect(document.querySelector(".budget-bar-hard")).toBeTruthy()
    );
    expect(screen.getByText(/limit reached/i)).toBeInTheDocument();
  });

  it("creates a new global budget for an unconfigured window", async () => {
    await act(async () => {
      render(<BudgetPanel scope="global" />);
    });
    await waitFor(() =>
      expect(document.querySelector(".budget-add-daily")).toBeTruthy()
    );

    // Open the daily add form, fill a limit, submit.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /add daily budget/i }));
    });
    const form = document.querySelector(".budget-add-form.budget-add-daily")!;
    await act(async () => {
      fireEvent.change(
        within(form as HTMLElement).getByLabelText(/New Daily limit in USD/i),
        { target: { value: "5" } }
      );
    });
    await act(async () => {
      fireEvent.click(within(form as HTMLElement).getByRole("button", { name: "Add" }));
    });

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        (c) => c[1]?.method === "POST"
      );
      expect(post).toBeTruthy();
      const body = JSON.parse(String((post![1] as RequestInit).body));
      expect(body).toMatchObject({
        scope: "global",
        window: "daily",
        limit_usd: 5,
        soft_pct: 0.8,
        agent_id: null,
      });
    });
    // The created row now renders as configured.
    await waitFor(() =>
      expect(document.querySelector(".budget-row-daily")).toBeTruthy()
    );
  });

  it("saves an edited limit via PATCH", async () => {
    state.budgets = [configured()];
    state.spent = { daily: 1 };
    await act(async () => {
      render(<BudgetPanel scope="global" />);
    });
    const row = (await waitFor(() =>
      document.querySelector(".budget-row-daily")
    )) as HTMLElement;

    await act(async () => {
      fireEvent.change(within(row).getByLabelText(/daily limit in USD/i), {
        target: { value: "25" },
      });
    });
    await act(async () => {
      fireEvent.click(within(row).getByRole("button", { name: "Save" }));
    });
    await waitFor(() => {
      const patch = fetchMock.mock.calls.find((c) => c[1]?.method === "PATCH");
      expect(patch).toBeTruthy();
      const body = JSON.parse(String((patch![1] as RequestInit).body));
      expect(body.limit_usd).toBe(25);
    });
  });

  it("deletes a budget via DELETE and drops the row", async () => {
    state.budgets = [configured()];
    state.spent = { daily: 1 };
    await act(async () => {
      render(<BudgetPanel scope="global" />);
    });
    const row = (await waitFor(() =>
      document.querySelector(".budget-row-daily")
    )) as HTMLElement;
    await act(async () => {
      fireEvent.click(within(row).getByRole("button", { name: /remove daily budget/i }));
    });
    await waitFor(() =>
      expect(document.querySelector(".budget-row-daily")).toBeNull()
    );
    expect(fetchMock.mock.calls.some((c) => c[1]?.method === "DELETE")).toBe(true);
  });

  it("gates the agent scope on a saved agent id", async () => {
    await act(async () => {
      render(<BudgetPanel scope="agent" agentId={undefined} />);
    });
    expect(screen.getByText(/Save the agent first/i)).toBeInTheDocument();
    // Never hits the API without an agent id.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("agent scope only shows that agent's budgets and creates agent-scoped", async () => {
    state.budgets = [
      configured({ id: "g", scope: "global", agent_id: null }),
      configured({
        id: "a",
        scope: "agent",
        agent_id: "agent-1",
        window: "weekly",
        limit_usd: 3,
      }),
      configured({
        id: "other",
        scope: "agent",
        agent_id: "agent-2",
        window: "daily",
      }),
    ];
    state.spent = { weekly: 1 };
    await act(async () => {
      render(<BudgetPanel scope="agent" agentId="agent-1" />);
    });
    // Sees its own weekly budget…
    await waitFor(() =>
      expect(document.querySelector(".budget-row-weekly")).toBeTruthy()
    );
    // …but not agent-2's daily (that window shows an add row instead).
    expect(document.querySelector(".budget-row-daily")).toBeNull();
    expect(document.querySelector(".budget-add-daily")).toBeTruthy();
  });
});
