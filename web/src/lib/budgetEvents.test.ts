/** Budget WS event → store message mappers (budget-model-routing.md §3.2).
 *
 * These lift the structured detail off the wire event, which is exactly the
 * part that can silently drift from the backend's emitted shape
 * (server/session_manager.py). The tests assert every field lands, so a rename
 * on either side fails here instead of showing an empty banner/card in the UI.
 */
import { describe, expect, it } from "vitest";

import { budgetWarningMessage, budgetErrorDetail } from "./budgetEvents";

describe("budgetWarningMessage", () => {
  it("lifts every field of a soft budget_warning event onto the message", () => {
    // Shape mirrors server/session_manager._enforce_budget's emitted event.
    const msg = budgetWarningMessage({
      type: "budget_warning",
      session_id: "s1",
      scope: "agent",
      agent_id: "a1",
      window: "weekly",
      limit_usd: 10,
      spent_usd: 8.5,
      soft_pct: 0.8,
      message: "Agent a1 weekly budget is at 80%+ of its $10.00 limit.",
    });
    expect(msg.role).toBe("system");
    expect(msg.type).toBe("budget_warning");
    expect(msg.content).toBe(
      "Agent a1 weekly budget is at 80%+ of its $10.00 limit."
    );
    expect(msg.budget).toEqual({
      scope: "agent",
      agent_id: "a1",
      window: "weekly",
      limit_usd: 10,
      spent_usd: 8.5,
      soft_pct: 0.8,
    });
  });

  it("defaults a missing agent_id (global scope) to null", () => {
    const msg = budgetWarningMessage({
      scope: "global",
      window: "daily",
      limit_usd: 5,
      spent_usd: 4,
      soft_pct: 0.8,
      message: "Global daily budget is near its limit.",
    });
    expect(msg.budget?.agent_id).toBeNull();
    expect(msg.budget?.scope).toBe("global");
  });
});

describe("budgetErrorDetail", () => {
  it("lifts the nested budget detail off a hard budget_exceeded error", () => {
    const detail = budgetErrorDetail({
      type: "error",
      code: "budget_exceeded",
      message: "Budget limit reached: global daily budget of $10.00 is spent.",
      budget: {
        scope: "global",
        agent_id: null,
        window: "daily",
        limit_usd: 10,
        spent_usd: 10.5,
      },
    });
    expect(detail).toEqual({
      scope: "global",
      agent_id: null,
      window: "daily",
      limit_usd: 10,
      spent_usd: 10.5,
    });
  });

  it("returns undefined for a non-budget error so the generic box renders", () => {
    expect(
      budgetErrorDetail({ type: "error", message: "Backend crashed" })
    ).toBeUndefined();
  });

  it("returns undefined when code is budget_exceeded but detail is missing", () => {
    // Defensive: never build a half-empty card from a malformed event.
    expect(
      budgetErrorDetail({ type: "error", code: "budget_exceeded" })
    ).toBeUndefined();
  });
});
