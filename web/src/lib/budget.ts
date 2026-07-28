/** Budget water-level maths, shared by the budget config panels
 * (budget-model-routing.md §3.3). Pure + unit-tested so the components can stay
 * about layout.
 *
 * The backend's `GET /api/budgets/status` returns `{limit_usd, spent_usd}` per
 * enabled budget; the frontend derives the fill level. The three levels mirror
 * the gate's own thresholds (server/budgets.py): below the soft percentage is
 * fine, at-or-above it is the soft warning (turns keep running), and at-or-above
 * the limit is the hard block (the next turn is refused). */

export type BudgetLevel = "ok" | "soft" | "hard";

export interface BudgetGauge {
  /** Fraction spent, clamped to [0, 1] for the bar width. */
  fraction: number;
  /** Raw spent/limit ratio, unclamped (can exceed 1 when over budget). */
  ratio: number;
  level: BudgetLevel;
}

/** Resolve the gauge for one budget's live spend against its limit and soft
 * percentage. A non-positive limit (never valid server-side, but be defensive)
 * degrades to a full, hard bar rather than dividing by zero. */
export function budgetGauge(
  spent: number,
  limit: number,
  softPct: number
): BudgetGauge {
  if (!(limit > 0)) {
    return { fraction: 1, ratio: spent > 0 ? Infinity : 0, level: "hard" };
  }
  const ratio = spent / limit;
  const fraction = Math.max(0, Math.min(1, ratio));
  let level: BudgetLevel = "ok";
  if (ratio >= 1) level = "hard";
  else if (ratio >= softPct) level = "soft";
  return { fraction, ratio, level };
}

/** Compact USD label. Budgets routinely run to sub-cent limits in tests and
 * fine-grained caps, so keep four decimals like the usage table does. */
export function fmtUsd(n: number): string {
  return `$${n.toFixed(4)}`;
}
