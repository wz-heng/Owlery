/**
 * Budget messages in chat (budget-model-routing.md §3.2):
 *  - a soft-threshold `budget_warning` renders as a calm inline banner (the
 *    turn still ran, so it's not an error),
 *  - a hard `budget_exceeded` error renders as an actionable "Budget reached"
 *    card with the offending scope/window/spend and next-step guidance,
 *  - a non-budget error still renders as the generic red error box.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { MessageBubble } from "./MessageBubble";
import type { Message } from "../stores/sessionStore";

afterEach(cleanup);

const bubble = (message: Message) =>
  render(<MessageBubble message={message} sessionId="s1" />);

describe("MessageBubble budget messages", () => {
  it("renders a soft budget_warning as a calm banner, not an error", () => {
    const { container } = bubble({
      role: "system",
      type: "budget_warning",
      content: "Global daily budget is at 80%+ of its $10.00 limit.",
    } as Message);
    expect(
      screen.getByText(/Global daily budget is at 80%\+/)
    ).toBeInTheDocument();
    expect(container.querySelector(".budget-warning-banner")).toBeTruthy();
    // Not styled as an error.
    expect(container.querySelector(".msg-error")).toBeNull();
  });

  it("renders a hard budget_exceeded error as an actionable Budget reached card", () => {
    const { container } = bubble({
      role: "system",
      type: "error",
      code: "budget_exceeded",
      content:
        "Budget limit reached: global daily budget of $10.00 is spent ($10.5000 used this window).",
      budget: {
        scope: "global",
        agent_id: null,
        window: "daily",
        limit_usd: 10,
        spent_usd: 10.5,
      },
    } as Message);
    // Dedicated card, not the generic crash box.
    expect(container.querySelector(".msg-budget-block")).toBeTruthy();
    expect(container.querySelector(".msg-error")).toBeNull();
    expect(screen.getByText("Budget reached")).toBeInTheDocument();
    // Structured detail + the message text.
    expect(screen.getByText(/Budget limit reached/)).toBeInTheDocument();
    expect(screen.getByText(/\$10\.5000 \/ \$10\.00 spent/)).toBeInTheDocument();
    // Next-step guidance names both routes out (raise/disable, or another agent).
    expect(screen.getByText(/raise or disable this budget/i)).toBeInTheDocument();
    expect(screen.getByText(/different agent/i)).toBeInTheDocument();
  });

  it("mentions the agent's own settings when the block is an agent budget", () => {
    bubble({
      role: "system",
      type: "error",
      code: "budget_exceeded",
      content: "Budget limit reached: agent abc daily budget of $1.00 is spent.",
      budget: {
        scope: "agent",
        agent_id: "abc",
        window: "daily",
        limit_usd: 1,
        spent_usd: 1.2,
      },
    } as Message);
    expect(screen.getByText(/agent's settings/i)).toBeInTheDocument();
  });

  it("still renders a non-budget error as the generic error box", () => {
    const { container } = bubble({
      role: "system",
      type: "error",
      content: "Backend crashed",
    } as Message);
    expect(container.querySelector(".msg-error")).toBeTruthy();
    expect(container.querySelector(".msg-budget-block")).toBeNull();
    expect(screen.getByText("Backend crashed")).toBeInTheDocument();
  });
});
