import type {
  DeliveryStatus,
  Task,
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
};

export const STATUS_ACCENT: Record<TaskStatus, string> = {
  triage: "bg-ink-400",
  todo: "bg-primary-300",
  ready: "bg-primary-700",
  running: "bg-attention",
  blocked: "bg-destructive",
  done: "bg-success",
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

const TERMINAL_DELIVERY_STATUSES: DeliveryStatus[] = ["delivered", "failed", "blocked", "conflicted"];

/** Whether a `done` task is safe to offer the Done column's batch "archive"
 * entry (task-board-overhaul.md §3.4): nothing about it is still mid-flight.
 * A task with no delivery (non-git-worktree run, or never accepted) has
 * nothing left to track; one with a delivery is eligible once that delivery
 * reached a terminal status — `preparing`/`ready`/`delivering` still have an
 * actionable panel a bulk archive must not bury. */
export function isArchivableDoneTask(task: Pick<Task, "status" | "archived" | "delivery">): boolean {
  if (task.status !== "done" || task.archived) return false;
  if (!task.delivery) return true;
  return TERMINAL_DELIVERY_STATUSES.includes(task.delivery.status);
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
