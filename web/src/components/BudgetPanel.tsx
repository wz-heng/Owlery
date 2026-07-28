import { useCallback, useEffect, useState } from "react";
import { IconPlus, IconTrash } from "@tabler/icons-react";
import {
  createBudget,
  deleteBudget,
  fetchBudgetStatus,
  fetchBudgets,
  updateBudget,
} from "../api/budgets";
import type { BudgetRead, BudgetScope, BudgetWindow } from "../api";
import { useSessionStore } from "../stores/sessionStore";
import { budgetGauge, fmtUsd } from "../lib/budget";
import { Button } from "./ui/button";
import { Input } from "./ui/input";

const WINDOWS: { value: BudgetWindow; label: string }[] = [
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
];

interface Props {
  scope: BudgetScope;
  /** Required when scope is "agent"; the agent these budgets belong to. */
  agentId?: string;
  /** Reload signal: bump/toggle to force a refetch (e.g. when a dialog opens). */
  refreshKey?: unknown;
}

/** Spend cap configuration for one scope (global, or a single agent), shared by
 * UsageDialog and AgentSettings (budget-model-routing.md §3.3).
 *
 * Budgets are unique per (scope, agent, window), so this renders exactly one
 * row per window: a configured budget is editable (limit / soft % / enabled)
 * with a live water level from `GET /api/budgets/status`; an unconfigured
 * window offers an inline "add". All spend is Claude USD — Codex turns report
 * no cost and can't be gated. */
export function BudgetPanel({ scope, agentId, refreshKey }: Props) {
  const token = useSessionStore((s) => s.token);
  const [budgets, setBudgets] = useState<BudgetRead[] | null>(null);
  // window -> live spend for the current window, only for enabled budgets.
  const [spent, setSpent] = useState<Record<string, number>>({});
  const [error, setError] = useState<string | null>(null);

  const mine = useCallback(
    (b: { scope: string; agent_id?: string | null }) =>
      scope === "global"
        ? b.scope === "global"
        : b.scope === "agent" && b.agent_id === agentId,
    [scope, agentId]
  );

  const reload = useCallback(async () => {
    if (!token) return;
    if (scope === "agent" && !agentId) return;
    try {
      const [all, statuses] = await Promise.all([
        fetchBudgets(token),
        fetchBudgetStatus(token),
      ]);
      setBudgets(all.filter(mine));
      const map: Record<string, number> = {};
      for (const s of statuses) if (mine(s)) map[s.window] = s.spent_usd;
      setSpent(map);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load budgets");
    }
  }, [token, scope, agentId, mine]);

  useEffect(() => {
    void reload();
  }, [reload, refreshKey]);

  const byWindow = (w: BudgetWindow) => budgets?.find((b) => b.window === w);

  const onDelete = async (id: string) => {
    try {
      await deleteBudget(token, id);
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to delete budget");
    }
  };

  const onSave = async (
    id: string,
    patch: { limit_usd: number; soft_pct: number; enabled: boolean }
  ) => {
    try {
      await updateBudget(token, id, patch);
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save budget");
      throw e;
    }
  };

  const onCreate = async (window: BudgetWindow, limit: number, soft: number) => {
    try {
      await createBudget(token, {
        scope,
        agent_id: scope === "agent" ? (agentId ?? null) : null,
        window,
        limit_usd: limit,
        soft_pct: soft,
        enabled: true,
      });
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create budget");
      throw e;
    }
  };

  if (scope === "agent" && !agentId) {
    return (
      <p className="budget-panel-hint text-xs text-muted-foreground">
        Save the agent first, then set a budget for it.
      </p>
    );
  }

  return (
    <div className="budget-panel space-y-2">
      {WINDOWS.map((w) => {
        const b = byWindow(w.value);
        return b ? (
          <BudgetRow
            key={w.value}
            budget={b}
            label={w.label}
            spent={spent[w.value]}
            onSave={onSave}
            onDelete={onDelete}
          />
        ) : (
          <AddBudgetRow
            key={w.value}
            window={w.value}
            label={w.label}
            onCreate={onCreate}
          />
        );
      })}
      {error && <p className="budget-error text-sm text-destructive">{error}</p>}
    </div>
  );
}

const LEVEL_BAR: Record<string, string> = {
  ok: "bg-primary",
  soft: "bg-attention-solid",
  hard: "bg-destructive",
};

function BudgetRow({
  budget,
  label,
  spent,
  onSave,
  onDelete,
}: {
  budget: BudgetRead;
  label: string;
  spent: number | undefined;
  onSave: (
    id: string,
    patch: { limit_usd: number; soft_pct: number; enabled: boolean }
  ) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
}) {
  const [limit, setLimit] = useState(String(budget.limit_usd));
  const [softPct, setSoftPct] = useState(String(Math.round(budget.soft_pct * 100)));
  const [enabled, setEnabled] = useState(budget.enabled);
  const [saving, setSaving] = useState(false);

  // Re-seed if the underlying budget changes out from under us (a reload after
  // a sibling row's edit, or the dialog reopening).
  useEffect(() => {
    setLimit(String(budget.limit_usd));
    setSoftPct(String(Math.round(budget.soft_pct * 100)));
    setEnabled(budget.enabled);
  }, [budget.id, budget.limit_usd, budget.soft_pct, budget.enabled]);

  const limitNum = Number(limit);
  const softNum = Number(softPct);
  const valid =
    Number.isFinite(limitNum) &&
    limitNum > 0 &&
    Number.isFinite(softNum) &&
    softNum > 0 &&
    softNum <= 100;
  const dirty =
    limitNum !== budget.limit_usd ||
    softNum / 100 !== budget.soft_pct ||
    enabled !== budget.enabled;

  const gauge =
    enabled && spent != null
      ? budgetGauge(spent, budget.limit_usd, budget.soft_pct)
      : null;

  const save = async () => {
    if (!valid) return;
    setSaving(true);
    try {
      await onSave(budget.id, {
        limit_usd: limitNum,
        soft_pct: softNum / 100,
        enabled,
      });
    } catch {
      // error surfaced by parent
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className={`budget-row budget-row-${budget.window} rounded-lg border border-ink-300 bg-ink-100 p-3 space-y-2`}
    >
      <div className="flex items-center gap-2">
        <span className="budget-window-label text-sm font-medium text-foreground w-16 shrink-0">
          {label}
        </span>
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <span>$</span>
          <Input
            className="budget-limit h-8 w-24"
            type="number"
            min="0"
            step="0.01"
            value={limit}
            onChange={(e) => setLimit(e.target.value)}
            aria-label={`${label} limit in USD`}
          />
        </label>
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <span>warn at</span>
          <Input
            className="budget-soft h-8 w-16"
            type="number"
            min="1"
            max="100"
            step="1"
            value={softPct}
            onChange={(e) => setSoftPct(e.target.value)}
            aria-label={`${label} soft warning percent`}
          />
          <span>%</span>
        </label>
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground ml-auto">
          <input
            type="checkbox"
            className="budget-enabled"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          on
        </label>
        <Button
          className="btn-budget-save"
          size="sm"
          onClick={save}
          disabled={!valid || !dirty || saving}
        >
          {saving ? "Saving…" : "Save"}
        </Button>
        <button
          type="button"
          className="btn-budget-delete inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
          title="Remove budget"
          aria-label={`Remove ${label} budget`}
          onClick={() => onDelete(budget.id)}
        >
          <IconTrash size={15} />
        </button>
      </div>
      {gauge && (
        <div className="budget-status space-y-1">
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-ink-300">
            <div
              className={`budget-bar budget-bar-${gauge.level} h-full rounded-full transition-all ${LEVEL_BAR[gauge.level]}`}
              style={{ width: `${gauge.fraction * 100}%` }}
            />
          </div>
          <p className="budget-spend text-[11px] text-muted-foreground">
            {fmtUsd(spent ?? 0)} of {fmtUsd(budget.limit_usd)} spent this{" "}
            {budget.window.replace(/ly$/, "")}
            {gauge.level === "hard" && (
              <span className="text-destructive"> · limit reached</span>
            )}
            {gauge.level === "soft" && (
              <span className="text-attention-solid"> · warning threshold</span>
            )}
          </p>
        </div>
      )}
      {!enabled && (
        <p className="budget-disabled text-[11px] text-muted-foreground">
          Disabled — not enforced.
        </p>
      )}
    </div>
  );
}

function AddBudgetRow({
  window,
  label,
  onCreate,
}: {
  window: BudgetWindow;
  label: string;
  onCreate: (window: BudgetWindow, limit: number, soft: number) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [limit, setLimit] = useState("");
  const [softPct, setSoftPct] = useState("80");
  const [saving, setSaving] = useState(false);

  const limitNum = Number(limit);
  const softNum = Number(softPct);
  const valid =
    limit.trim() !== "" &&
    Number.isFinite(limitNum) &&
    limitNum > 0 &&
    Number.isFinite(softNum) &&
    softNum > 0 &&
    softNum <= 100;

  const create = async () => {
    if (!valid) return;
    setSaving(true);
    try {
      await onCreate(window, limitNum, softNum / 100);
      setOpen(false);
      setLimit("");
      setSoftPct("80");
    } catch {
      // error surfaced by parent
    } finally {
      setSaving(false);
    }
  };

  if (!open) {
    return (
      <button
        type="button"
        className={`btn-budget-add budget-add-${window} flex w-full items-center gap-2 rounded-lg border border-dashed border-ink-300 px-3 py-2 text-left text-xs text-muted-foreground transition-colors hover:border-primary/60 hover:text-foreground`}
        onClick={() => setOpen(true)}
      >
        <IconPlus size={14} />
        Add {label.toLowerCase()} budget
      </button>
    );
  }

  return (
    <div
      className={`budget-add-form budget-add-${window} rounded-lg border border-ink-300 bg-ink-100 p-3 flex flex-wrap items-center gap-2`}
    >
      <span className="text-sm font-medium text-foreground w-16 shrink-0">
        {label}
      </span>
      <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <span>$</span>
        <Input
          className="budget-limit h-8 w-24"
          type="number"
          min="0"
          step="0.01"
          placeholder="10.00"
          value={limit}
          onChange={(e) => setLimit(e.target.value)}
          aria-label={`New ${label} limit in USD`}
          autoFocus
        />
      </label>
      <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <span>warn at</span>
        <Input
          className="budget-soft h-8 w-16"
          type="number"
          min="1"
          max="100"
          step="1"
          value={softPct}
          onChange={(e) => setSoftPct(e.target.value)}
          aria-label={`New ${label} soft warning percent`}
        />
        <span>%</span>
      </label>
      <div className="ml-auto flex gap-2">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => {
            setOpen(false);
            setLimit("");
          }}
        >
          Cancel
        </Button>
        <Button
          className="btn-budget-create"
          size="sm"
          onClick={create}
          disabled={!valid || saving}
        >
          {saving ? "Adding…" : "Add"}
        </Button>
      </div>
    </div>
  );
}
