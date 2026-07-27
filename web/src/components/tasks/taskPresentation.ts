import type { TaskStatus, TaskRunState } from "../../api/tasks";

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
