import {
  IconAlertTriangle,
  IconBan,
  IconCalendar,
  IconGitBranch,
  IconListTree,
  IconMessage,
  IconPlayerPlay,
  IconFolder,
} from "@tabler/icons-react";

import type { Task } from "../../api/tasks";
import type { Agent } from "../../stores/sessionStore";
import { AgentSeal } from "../ui/seal";
import { cn } from "../../lib/utils";
import {
  DELIVERY_TONE_PILL,
  deliveryChip,
  relativeTime,
  RUN_LABEL,
  STATUS_ACCENT,
  verdictBadge,
} from "./taskPresentation";

interface TaskCardProps {
  task: Task;
  agent?: Agent;
  onOpen: (taskId: string) => void;
  draggable?: boolean;
}

export function TaskCard({ task, agent, onOpen, draggable = true }: TaskCardProps) {
  const delivery = deliveryChip(task);
  const verdict = verdictBadge(task);
  // `updated_at` is the closest durable proxy for "time since blocked" —
  // there is no dedicated `blocked_at` column, so any later edit (a
  // comment, a priority change) also resets this age. Acceptable: it still
  // reads as "how long has this sat untouched," which is the staleness
  // signal task-board-overhaul.md §3.4 asks for.
  const ageSource =
    task.status === "running"
      ? task.latest_heartbeat_at ?? task.updated_at
      : task.status === "blocked"
        ? task.updated_at
        : null;

  return (
    <article
      className={cn(
        "group relative cursor-pointer overflow-hidden rounded-xl border border-ink-300 bg-card p-3 shadow-[var(--elevation-raised)] transition-all",
        "hover:-translate-y-0.5 hover:border-primary/40 hover:shadow-[var(--elevation-floating)]",
        task.archived && "opacity-60"
      )}
      tabIndex={0}
      role="button"
      aria-label={`Open task ${task.title}`}
      draggable={draggable && task.status !== "running" && task.status !== "done" && task.status !== "cancelled"}
      onDragStart={(event) => {
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("application/x-owlery-task", task.id);
      }}
      onClick={() => onOpen(task.id)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") onOpen(task.id);
      }}
    >
      <span className={cn("absolute inset-y-0 left-0 w-1", STATUS_ACCENT[task.status])} />
      <div className="flex items-start justify-between gap-2 pl-1">
        <h3 className="line-clamp-2 text-sm font-semibold leading-5 text-foreground">
          {task.title}
        </h3>
        {verdict && (
          <span
            data-testid="task-verdict-badge"
            className={cn(
              "shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
              DELIVERY_TONE_PILL[verdict.tone]
            )}
            title="This task's worker reported verdict=fail — dependents will not unblock on it"
          >
            {verdict.label}
          </span>
        )}
        {task.priority > 0 && (
          <span
            className="shrink-0 rounded-full bg-primary-50 px-1.5 py-0.5 text-[10px] font-bold text-primary-700"
            title={`Priority ${task.priority}`}
          >
            P{task.priority}
          </span>
        )}
      </div>

      {(task.body || task.body_excerpt) && (
        <p className="mt-1.5 line-clamp-2 pl-1 text-xs leading-4 text-muted-foreground">
          {task.body || task.body_excerpt}
        </p>
      )}

      <div className="mt-3 flex min-h-5 items-center justify-between gap-2 pl-1">
        <div className="flex min-w-0 items-center gap-1.5">
          {agent ? (
            <>
              <AgentSeal agent={agent} scale="chip" />
              <span className="truncate text-[11px] text-ink-700">{agent.name}</span>
            </>
          ) : (
            <span className="text-[11px] italic text-muted-foreground">Unassigned</span>
          )}
        </div>
        <span className="font-mono text-[10px] text-muted-foreground">{task.id.slice(0, 6)}</span>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 border-t border-ink-200 pt-2 text-[10px] text-muted-foreground">
        {(task.child_count ?? 0) > 0 && (
          <span className="inline-flex items-center gap-0.5" title="Children">
            <IconListTree size={12} /> {task.child_count}
          </span>
        )}
        {(task.dependency_count ?? task.dependencies?.length ?? 0) > 0 && (
          <span className="inline-flex items-center gap-0.5" title="Dependencies">
            <IconGitBranch size={12} /> {task.dependency_count ?? task.dependencies?.length}
          </span>
        )}
        {task.latest_run_state && (
          <span className="inline-flex items-center gap-0.5">
            <IconPlayerPlay size={12} /> {RUN_LABEL[task.latest_run_state]}
          </span>
        )}
        {delivery && (
          <span
            data-testid="task-delivery-chip"
            className={cn(
              "inline-flex items-center rounded-full px-1.5 py-0.5 font-semibold uppercase tracking-wide",
              DELIVERY_TONE_PILL[delivery.tone]
            )}
          >
            {delivery.label}
          </span>
        )}
        {(task.workspace_mode || task.working_dir_override) && (
          <span className="inline-flex items-center gap-0.5" title={task.working_dir_override ?? "Workspace mode override"}>
            <IconFolder size={12} /> {task.workspace_mode ?? "custom"}
          </span>
        )}
        {task.scheduled_at && (
          <span className="inline-flex items-center gap-0.5" title="Scheduled">
            <IconCalendar size={12} /> {relativeTime(task.scheduled_at)}
          </span>
        )}
        {task.status === "blocked" && (
          <span className="inline-flex min-w-0 items-center gap-0.5 text-destructive" title={`Blocked ${relativeTime(task.updated_at)}`}>
            <IconAlertTriangle size={12} />
            <span className="truncate">
              {task.blocked_kind ?? "blocked"} · {relativeTime(task.updated_at)}
            </span>
          </span>
        )}
        {/* Cancelled is a decisive terminal state, not a wait — no staleness
            age here, unlike the blocked badge above (task-board-gaps.md §3.4). */}
        {task.status === "cancelled" && (
          <span className="inline-flex min-w-0 items-center gap-0.5 text-muted-foreground" title="Cancelled">
            <IconBan size={12} />
            <span className="truncate">Cancelled</span>
          </span>
        )}
        {ageSource && task.status !== "blocked" && (
          <span className="ml-auto inline-flex items-center gap-0.5">
            <IconMessage size={12} /> {relativeTime(ageSource)}
          </span>
        )}
      </div>
    </article>
  );
}
