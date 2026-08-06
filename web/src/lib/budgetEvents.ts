/** Mappers from budget WebSocket events to chat store messages
 * (budget-model-routing.md §3.2). Kept pure and unit-tested so the
 * `useWebSocket` handler stays a thin dispatch: the field-by-field lift off the
 * wire event (which is the part that can silently drift from the backend) lives
 * here where it can be asserted directly.
 *
 * The backend shapes these in `server/session_manager.py`:
 *  - `budget_warning`: a soft-threshold notice emitted once per window before a
 *    turn runs, carrying scope/agent_id/window/limit/spend/soft_pct + message.
 *  - `error` with `code="budget_exceeded"`: a hard block, carrying a nested
 *    `budget` object (no soft_pct — the limit is already reached). */

import type { Message } from "../stores/sessionStore";

/** Build the chat message for a soft `budget_warning` event. All the structured
 * detail rides along on `Message.budget` so the banner (and any future
 * affordance) has scope/window/limit/spend without a refetch. */
export function budgetWarningMessage(data: Record<string, unknown>): Message {
  return {
    role: "system",
    type: "budget_warning",
    content: data.message as string,
    budget: {
      scope: data.scope as string,
      agent_id: (data.agent_id as string | null) ?? null,
      window: data.window as string,
      limit_usd: data.limit_usd as number,
      spent_usd: data.spent_usd as number,
      soft_pct: data.soft_pct as number,
    },
  };
}

/** Lift the structured budget detail off a hard-block `error` event so the chat
 * can render the actionable "Budget reached" card. Returns undefined for any
 * other error (including a plain backend crash) so it falls back to the generic
 * red error box. */
export function budgetErrorDetail(
  data: Record<string, unknown>
): Message["budget"] | undefined {
  if (data.code !== "budget_exceeded" || !data.budget) return undefined;
  const b = data.budget as Record<string, unknown>;
  return {
    scope: b.scope as string,
    agent_id: (b.agent_id as string | null) ?? null,
    window: b.window as string,
    limit_usd: b.limit_usd as number,
    spent_usd: b.spent_usd as number,
  };
}
