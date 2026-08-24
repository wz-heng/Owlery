import type {
  DeliveryStatus,
  Task,
  TaskDelivery,
  TaskStatus,
  TaskRunState,
} from "../../api/tasks";

export const STATUS_LABEL: Record<TaskStatus, string> = {
  triage: "Triage",
  todo: "Todo",
  ready: "Ready",
  running: "Running",
  blocked: "Blocked",
  done: "Done",
  cancelled: "Cancelled",
};

export const STATUS_ACCENT: Record<TaskStatus, string> = {
  triage: "bg-ink-400",
  todo: "bg-primary-300",
  ready: "bg-primary-700",
  running: "bg-attention",
  blocked: "bg-destructive",
  done: "bg-success",
  // Deliberately terminal-neutral, not destructive — a cancelled task is a
  // decision, not a failure (task-board-gaps.md §3.4).
  cancelled: "bg-ink-600",
};

export const RUN_LABEL: Record<TaskRunState, string> = {
  running: "Running",
  completed: "Completed",
  blocked: "Blocked",
  failed: "Failed",
  cancelled: "Cancelled",
  interrupted: "Interrupted",
};

/** The four semantic tones a delivery chip / pill can take.  One tone → one
 * pair of Tailwind classes, shared by the drawer's status pill and the board
 * card / tree chips so the whole feature reads as one colour language. */
export type DeliveryTone = "neutral" | "attention" | "success" | "destructive";

export const DELIVERY_TONE_PILL: Record<DeliveryTone, string> = {
  neutral: "bg-ink-200 text-muted-foreground",
  attention: "bg-attention-surface text-attention",
  success: "bg-success-surface text-success",
  destructive: "bg-destructive-surface text-destructive",
};

/** Map a delivery lifecycle status to its tone.  This is the single source of
 * the colour semantics the drawer's `statusPill` also consumes. */
export function deliveryStatusTone(status: DeliveryStatus): DeliveryTone {
  switch (status) {
    case "delivered":
      return "success";
    case "delivering":
    case "preparing":
      return "attention";
    case "conflicted":
    case "failed":
    case "blocked":
      return "destructive";
    case "pending":
    case "ready":
      return "neutral";
    default: {
      // Compile-time exhaustiveness: a new DeliveryStatus without a tone is a
      // type error here, not a silent neutral. Runtime keeps the safe default.
      const _exhaustive: never = status;
      void _exhaustive;
      return "neutral";
    }
  }
}

export interface DeliveryChip {
  label: string;
  tone: DeliveryTone;
}

/** Derive the board card / tree delivery chip from a task's enrichment fields.
 *
 * Returns `null` when there is nothing worth showing: no delivery and either no
 * run or a run whose workspace isn't a git worktree (shared/copy runs never get
 * an invented delivery status).  Pure and total over the closed status union so
 * it can be exhaustively unit-tested. */
export function deliveryChip(
  task: Pick<
    Task,
    "delivery" | "latest_run_state" | "latest_run_workspace_mode"
  >
): DeliveryChip | null {
  const delivery = task.delivery;
  if (!delivery) {
    if (
      task.latest_run_workspace_mode === "git_worktree" &&
      task.latest_run_state === "completed"
    ) {
      return { label: "Not accepted", tone: "neutral" };
    }
    return null;
  }
  switch (delivery.status) {
    case "pending":
    case "preparing":
      return { label: "Accepting…", tone: "neutral" };
    case "ready":
      if (delivery.dirty) return { label: "Uncommitted", tone: "attention" };
      if ((delivery.commits_ahead ?? 0) > 0)
        return { label: "Ready to push", tone: "attention" };
      return { label: "Accepted", tone: "neutral" };
    case "delivering":
      return { label: "Delivering…", tone: "attention" };
    case "delivered":
      if (delivery.merge_strategy != null)
        return { label: "Merged", tone: "success" };
      if (delivery.pr_number != null)
        return {
          label: `PR #${delivery.pr_number}${
            delivery.pr_state ? ` · ${delivery.pr_state}` : ""
          }`,
          tone: "success",
        };
      return { label: "Pushed", tone: "success" };
    case "conflicted":
    case "blocked":
    case "failed":
      return { label: delivery.reason_kind ?? delivery.status, tone: "destructive" };
  }
}

/** Whether a finished task is safe to offer the Done column's batch
 * "archive" entry (task-board-overhaul.md §3.4; widened to `cancelled` by
 * task-board-gaps.md §3.4 — a decisively closed-out card, same as `done`).
 * A task with no delivery (non-git-worktree run, never accepted, or never
 * even run before being cancelled) has nothing left to track. A task WITH a
 * delivery is eligible only once that delivery actually reached `delivered`
 * — `failed`/`blocked`/`conflicted` are terminal in the state-machine sense
 * but still need a human's eyes; batching them under "Archive N finished"
 * would hide a real problem, not close out a finished one (Snape review). */
export function isArchivableDoneTask(task: Pick<Task, "status" | "archived" | "delivery">): boolean {
  if ((task.status !== "done" && task.status !== "cancelled") || task.archived) return false;
  if (!task.delivery) return true;
  return task.delivery.status === "delivered";
}

/** The explicit "did not pass" badge for a `done` task with `verdict: "fail"`
 * (task-board-gaps.md §3.1) — visually distinct from an ordinary done card so
 * nobody mistakes a failed review for a finished one. `null` for every other
 * case (no verdict, verdict: "pass", or non-done statuses). */
export function verdictBadge(task: Pick<Task, "status" | "verdict">): DeliveryChip | null {
  if (task.status !== "done" || task.verdict !== "fail") return null;
  return { label: "Review failed", tone: "destructive" };
}

export type DeliveryButtonKind =
  | "accept"
  | "commit"
  | "push"
  | "pull_request"
  | "merge"
  | "teardown";

export interface DeliveryButtonState {
  enabled: boolean;
  /** Why the action is unavailable right now — every disabled delivery
   * action must be able to answer "why" (task-board-gaps.md §3.5). `null`
   * when `enabled` is true, or when unavailability is solely because another
   * action is in flight (the caller renders that reason once, panel-wide). */
  reason: string | null;
}

type DeliveryButtonInput = Pick<
  TaskDelivery,
  "status" | "dirty" | "commits_ahead" | "pr_number" | "pushed_ref"
>;

/** Single source of truth for whether each delivery action button is
 * clickable and, if not, why — shared by the enable/disable logic and the
 * tooltip text so the two can never drift apart. `delivery` is `null` before
 * `Accept` has ever been called (task-board-gaps.md §3.5). */
export function deliveryButtonState(
  kind: DeliveryButtonKind,
  delivery: DeliveryButtonInput | null
): DeliveryButtonState {
  if (!delivery) {
    return kind === "accept"
      ? { enabled: true, reason: null }
      : { enabled: false, reason: "Accept the delivery first" };
  }
  const { status, dirty, commits_ahead, pr_number, pushed_ref } = delivery;
  switch (kind) {
    case "accept":
      return status === "pending"
        ? { enabled: true, reason: null }
        : { enabled: false, reason: "Already accepted" };
    case "commit":
      if (status !== "ready") return { enabled: false, reason: "Not ready to commit yet" };
      if (!dirty) return { enabled: false, reason: "Nothing to commit — the worktree is clean" };
      return { enabled: true, reason: null };
    case "push":
      if (status !== "ready") return { enabled: false, reason: "Not ready to push yet" };
      if (dirty) return { enabled: false, reason: "Commit the pending changes first" };
      if ((commits_ahead ?? 0) <= 0)
        return { enabled: false, reason: "Nothing to push — no commits ahead of base" };
      return { enabled: true, reason: null };
    case "pull_request":
      if (!pushed_ref) return { enabled: false, reason: "Push the branch first" };
      if (pr_number != null)
        return { enabled: false, reason: "Pull request already open — see the link above" };
      return { enabled: true, reason: null };
    case "merge":
      if (pr_number == null) return { enabled: false, reason: "Open a pull request first" };
      if (status === "delivered")
        return { enabled: false, reason: "Already delivered — merge on the platform" };
      return { enabled: true, reason: null };
    case "teardown": {
      const terminal = (
        ["delivered", "failed", "blocked", "conflicted"] as DeliveryStatus[]
      ).includes(status);
      return terminal
        ? { enabled: true, reason: null }
        : { enabled: false, reason: "Delivery must reach a terminal state before teardown" };
    }
  }
}

export function relativeTime(value: string | null | undefined): string {
  if (!value) return "—";
  const delta = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(delta)) return "—";
  const future = delta < 0;
  const seconds = Math.abs(delta) / 1000;
  const amount =
    seconds < 60
      ? `${Math.max(1, Math.round(seconds))}s`
      : seconds < 3600
        ? `${Math.round(seconds / 60)}m`
        : seconds < 86400
          ? `${Math.round(seconds / 3600)}h`
          : `${Math.round(seconds / 86400)}d`;
  return future ? `in ${amount}` : `${amount} ago`;
}
export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
