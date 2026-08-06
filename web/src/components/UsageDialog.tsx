import { useEffect, useState } from "react";
import { useSessionStore } from "../stores/sessionStore";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";
import type { ReactNode } from "react";
import { AgentSeal } from "./ui/seal";
import { Skeleton } from "./ui/skeleton";
import { BudgetPanel } from "./BudgetPanel";

const API_URL = window.location.origin;

// Hand-declared: /api/usage/summary isn't in the generated OpenAPI contracts
// (usage-tracking.md §6). Keys are ids; names resolve from store state.
export interface UsageRow {
  key: string | null;
  turns: number;
  cost: number | null;
  input_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  output_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
}

export interface UsageSummary {
  group_by: string;
  rows: UsageRow[];
  totals: Omit<UsageRow, "key">;
}

type GroupBy = "agent" | "session" | "day" | "backend";
const GROUPS: { value: GroupBy; label: string }[] = [
  { value: "agent", label: "Agent" },
  { value: "session", label: "Session" },
  { value: "day", label: "Day" },
  { value: "backend", label: "Backend" },
];

const WINDOWS: { days: number; label: string }[] = [
  { days: 7, label: "7 days" },
  { days: 30, label: "30 days" },
  { days: 0, label: "All time" },
];

const fmtTokens = (n: number) => n.toLocaleString();
const fmtCost = (c: number | null) => (c == null ? "—" : `$${c.toFixed(4)}`);

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}

/** Manage page for per-turn consumption, reached from the account menu
 * (usage-tracking.md §6). One table over the turn_usage ledger, grouped by
 * agent / session / day / backend within a time window. Tables only — the
 * later limit-awareness work owns anything fancier. */
export function UsageDialog({ open, onOpenChange }: Props) {
  const token = useSessionStore((s) => s.token);
  // Catalog (incl. archived) so usage rows for an archived agent resolve to
  // its name + seal, not a truncated id (agent-identity.md).
  const agents = useSessionStore((s) => s.agentCatalog);
  const sessions = useSessionStore((s) => s.sessions);
  const archivedSessions = useSessionStore((s) => s.archivedSessions);
  const [groupBy, setGroupBy] = useState<GroupBy>("agent");
  const [windowDays, setWindowDays] = useState(30);
  const [data, setData] = useState<UsageSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Distinguishes "still fetching" from "fetched, and there is nothing".
  // Without it the dialog claims "No usage recorded in this window" during
  // the request — asserting an absence it hasn't confirmed yet.
  const [loading, setLoading] = useState(false);

  // Abort the in-flight request whenever the controls change or the dialog
  // closes — a slow stale response must never land after a fresh one and
  // mislabel the table. The explicit aborted checks also cover fetch
  // implementations that resolve (rather than reject) after abort.
  useEffect(() => {
    if (!open || !token) return;
    const ctrl = new AbortController();
    const params = new URLSearchParams({ group_by: groupBy });
    if (windowDays > 0) {
      const since = new Date(Date.now() - windowDays * 86_400_000);
      params.set("since", since.toISOString());
    }
    setLoading(true);
    (async () => {
      try {
        const res = await fetch(`${API_URL}/api/usage/summary?${params}`, {
          headers: { Authorization: `Bearer ${token}` },
          signal: ctrl.signal,
        });
        if (ctrl.signal.aborted) return;
        if (!res.ok) {
          setError(`Failed to load usage (${res.status})`);
          return;
        }
        const body = (await res.json()) as UsageSummary;
        if (ctrl.signal.aborted) return;
        setError(null);
        setData(body);
      } catch {
        if (!ctrl.signal.aborted) setError("Failed to load usage");
      } finally {
        if (!ctrl.signal.aborted) setLoading(false);
      }
    })();
    return () => ctrl.abort();
  }, [open, token, groupBy, windowDays]);

  const keyLabel = (key: string | null): ReactNode => {
    if (key == null) return "(none)";
    if (groupBy === "agent") {
      const agent = agents.find((a) => a.id === key);
      if (!agent) return `${key.slice(0, 8)}…`;
      return (
        <span className="inline-flex items-center gap-1.5 min-w-0">
          <AgentSeal agent={agent} />
          <span className="truncate">{agent.name}</span>
        </span>
      );
    }
    if (groupBy === "session") {
      const s =
        sessions.find((x) => x.id === key) ??
        archivedSessions.find((x) => x.id === key);
      return s ? s.name : `${key.slice(0, 8)}… (deleted)`;
    }
    return key;
  };

  const totals = data?.totals;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="usage-dialog max-w-3xl">
        <DialogHeader>
          <DialogTitle>Usage</DialogTitle>
          <DialogDescription>
            Cost and token consumption recorded per turn, aggregated over the
            selected window. Codex reports tokens only, so its cost shows “—”.
          </DialogDescription>
        </DialogHeader>

        {/* Global spend caps (budget-model-routing.md §3.3). A pre-run gate on
         * Claude USD spend per window — soft threshold warns in-session, hard
         * threshold blocks the next turn. Per-agent budgets live in that
         * agent's settings; these apply across every agent. */}
        <section className="usage-budgets space-y-2">
          <div>
            <h3 className="text-sm font-semibold text-foreground">Budgets</h3>
            <p className="text-xs text-muted-foreground">
              Cap Claude spend per window. A turn is warned near the limit and
              blocked once spent reaches it. Codex turns report no cost and
              aren’t counted.
            </p>
          </div>
          <BudgetPanel scope="global" refreshKey={open} />
        </section>

        <div className="usage-controls flex flex-wrap items-center gap-2">
          <div className="flex rounded-lg border border-ink-300 bg-ink-100 shadow-[var(--elevation-inset)] p-1">
            {GROUPS.map((g) => (
              <button
                key={g.value}
                className={`usage-group-${g.value} rounded-md px-3 py-1 text-xs transition-all ${
                  groupBy === g.value
                    ? "bg-card font-semibold text-primary-700 shadow-[var(--elevation-raised)]"
                    : "text-muted-foreground hover:text-foreground hover:bg-ink-200/70"
                }`}
                onClick={() => setGroupBy(g.value)}
              >
                {g.label}
              </button>
            ))}
          </div>
          <div className="ml-auto flex rounded-lg border border-ink-300 bg-ink-100 shadow-[var(--elevation-inset)] p-1">
            {WINDOWS.map((w) => (
              <button
                key={w.days}
                className={`usage-window-${w.days} rounded-md px-3 py-1 text-xs transition-all ${
                  windowDays === w.days
                    ? "bg-card font-semibold text-primary-700 shadow-[var(--elevation-raised)]"
                    : "text-muted-foreground hover:text-foreground hover:bg-ink-200/70"
                }`}
                onClick={() => setWindowDays(w.days)}
              >
                {w.label}
              </button>
            ))}
          </div>
        </div>

        {error ? (
          <div className="usage-error rounded-lg border border-destructive/40 bg-destructive-surface shadow-[var(--elevation-inset)] px-4 py-12 text-center text-sm text-destructive">
            {error}
          </div>
        ) : loading && !data ? (
          <div className="usage-loading space-y-2" aria-busy="true">
            <Skeleton className="h-9 w-full" />
            {Array.from({ length: 5 }, (_, i) => (
              <Skeleton key={i} className="h-8 w-full" />
            ))}
          </div>
        ) : !data || data.rows.length === 0 ? (
          <div className="usage-empty rounded-lg border border-ink-300 bg-ink-100 shadow-[var(--elevation-inset)] px-4 py-12 text-center text-sm text-muted-foreground">
            No usage recorded in this window yet. Run a turn and it will show
            up here.
          </div>
        ) : (
          <div className="usage-table max-h-[60vh] overflow-auto rounded-lg border border-ink-300">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-card text-xs uppercase tracking-wider text-muted-foreground">
                <tr className="[&>th]:px-3 [&>th]:py-2 [&>th]:font-semibold">
                  <th className="text-left">{groupBy}</th>
                  <th className="text-right">Turns</th>
                  <th className="text-right">Cost</th>
                  <th className="text-right">Input</th>
                  <th className="text-right">Cache read</th>
                  <th className="text-right">Cache write</th>
                  <th className="text-right">Output</th>
                  <th className="text-right">Total tokens</th>
                </tr>
              </thead>
              <tbody>
                {data.rows.map((r, i) => (
                  <tr
                    key={r.key ?? `none-${i}`}
                    className="usage-row border-t border-border [&>td]:px-3 [&>td]:py-1.5"
                  >
                    <td className="max-w-[16rem] truncate text-left">
                      {keyLabel(r.key)}
                    </td>
                    <td className="text-right tabular-nums">{r.turns}</td>
                    <td className="text-right tabular-nums">{fmtCost(r.cost)}</td>
                    <td className="text-right tabular-nums">
                      {fmtTokens(r.input_tokens)}
                    </td>
                    <td className="text-right tabular-nums">
                      {fmtTokens(r.cache_read_tokens)}
                    </td>
                    <td className="text-right tabular-nums">
                      {fmtTokens(r.cache_creation_tokens)}
                    </td>
                    <td className="text-right tabular-nums">
                      {fmtTokens(r.output_tokens)}
                    </td>
                    <td className="text-right font-medium tabular-nums">
                      {fmtTokens(r.total_tokens)}
                    </td>
                  </tr>
                ))}
              </tbody>
              {totals && (
                <tfoot>
                  <tr className="usage-totals border-t border-border bg-muted/40 font-medium [&>td]:px-3 [&>td]:py-2">
                    <td className="text-left">Total</td>
                    <td className="text-right tabular-nums">{totals.turns}</td>
                    <td className="text-right tabular-nums">
                      {fmtCost(totals.cost)}
                    </td>
                    <td className="text-right tabular-nums">
                      {fmtTokens(totals.input_tokens)}
                    </td>
                    <td className="text-right tabular-nums">
                      {fmtTokens(totals.cache_read_tokens)}
                    </td>
                    <td className="text-right tabular-nums">
                      {fmtTokens(totals.cache_creation_tokens)}
                    </td>
                    <td className="text-right tabular-nums">
                      {fmtTokens(totals.output_tokens)}
                    </td>
                    <td className="text-right tabular-nums">
                      {fmtTokens(totals.total_tokens)}
                    </td>
                  </tr>
                </tfoot>
              )}
            </table>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
