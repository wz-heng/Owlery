import { useCallback, useEffect, useState } from "react";
import { useSessionStore } from "../stores/sessionStore";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";

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
  const agents = useSessionStore((s) => s.agents);
  const sessions = useSessionStore((s) => s.sessions);
  const archivedSessions = useSessionStore((s) => s.archivedSessions);
  const [groupBy, setGroupBy] = useState<GroupBy>("agent");
  const [windowDays, setWindowDays] = useState(30);
  const [data, setData] = useState<UsageSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchSummary = useCallback(async () => {
    if (!token) return;
    const params = new URLSearchParams({ group_by: groupBy });
    if (windowDays > 0) {
      const since = new Date(Date.now() - windowDays * 86_400_000);
      params.set("since", since.toISOString());
    }
    try {
      const res = await fetch(`${API_URL}/api/usage/summary?${params}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) {
        setError(`Failed to load usage (${res.status})`);
        return;
      }
      setError(null);
      setData((await res.json()) as UsageSummary);
    } catch {
      setError("Failed to load usage");
    }
  }, [token, groupBy, windowDays]);

  useEffect(() => {
    if (open) fetchSummary();
  }, [open, fetchSummary]);

  const keyLabel = (key: string | null): string => {
    if (key == null) return "(none)";
    if (groupBy === "agent") {
      const agent = agents.find((a) => a.id === key);
      return agent ? `${agent.avatar || "🐙"} ${agent.name}` : `${key.slice(0, 8)}…`;
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

        <div className="usage-controls flex flex-wrap items-center gap-2">
          <div className="flex rounded-lg border border-border p-0.5">
            {GROUPS.map((g) => (
              <button
                key={g.value}
                className={`usage-group-${g.value} rounded-md px-2.5 py-1 text-xs ${
                  groupBy === g.value
                    ? "bg-accent font-medium text-foreground"
                    : "text-muted-foreground hover:text-foreground"
                }`}
                onClick={() => setGroupBy(g.value)}
              >
                {g.label}
              </button>
            ))}
          </div>
          <div className="ml-auto flex rounded-lg border border-border p-0.5">
            {WINDOWS.map((w) => (
              <button
                key={w.days}
                className={`usage-window-${w.days} rounded-md px-2.5 py-1 text-xs ${
                  windowDays === w.days
                    ? "bg-accent font-medium text-foreground"
                    : "text-muted-foreground hover:text-foreground"
                }`}
                onClick={() => setWindowDays(w.days)}
              >
                {w.label}
              </button>
            ))}
          </div>
        </div>

        {error ? (
          <div className="usage-error rounded-lg border border-dashed border-border bg-muted/30 px-4 py-8 text-center text-sm text-muted-foreground">
            {error}
          </div>
        ) : !data || data.rows.length === 0 ? (
          <div className="usage-empty rounded-lg border border-dashed border-border bg-muted/30 px-4 py-8 text-center text-sm text-muted-foreground">
            No usage recorded in this window yet. Run a turn and it will show
            up here.
          </div>
        ) : (
          <div className="usage-table max-h-[60vh] overflow-auto rounded-lg border border-border">
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
