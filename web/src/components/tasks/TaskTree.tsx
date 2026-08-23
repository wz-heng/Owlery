import { useMemo, useState } from "react";
import { IconChevronRight, IconGitBranch, IconHierarchy } from "@tabler/icons-react";

import type { Task } from "../../api/tasks";
import type { Agent } from "../../stores/sessionStore";
import { cn } from "../../lib/utils";
import { AgentSeal } from "../ui/seal";
import {
  DELIVERY_TONE_PILL,
  deliveryChip,
  STATUS_ACCENT,
  STATUS_LABEL,
} from "./taskPresentation";

interface TaskTreeProps {
  tasks: Task[];
  agents: Agent[];
  onOpenTask: (taskId: string) => void;
}

export function TaskTree({ tasks, agents, onOpenTask }: TaskTreeProps) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const agentMap = useMemo(() => new Map(agents.map((agent) => [agent.id, agent])), [agents]);
  const taskIds = useMemo(() => new Set(tasks.map((task) => task.id)), [tasks]);
  const children = useMemo(() => {
    const map = new Map<string | null, Task[]>();
    for (const task of tasks) {
      const parent = task.parent_task_id && taskIds.has(task.parent_task_id) ? task.parent_task_id : null;
      map.set(parent, [...(map.get(parent) ?? []), task]);
    }
    for (const list of map.values()) {
      list.sort((a, b) => b.priority - a.priority || a.created_at.localeCompare(b.created_at));
    }
    return map;
  }, [tasks, taskIds]);

  const toggle = (id: string) =>
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const row = (task: Task, depth: number): React.ReactNode => {
    const descendants = children.get(task.id) ?? [];
    const isCollapsed = collapsed.has(task.id);
    const agent = task.assignee_agent_id ? agentMap.get(task.assignee_agent_id) : undefined;
    const delivery = deliveryChip(task);
    const dependencyCount = task.dependency_count ?? task.dependencies?.length ?? 0;
    return (
      <div key={task.id}>
        <div
          className={cn(
            "group grid min-w-[42rem] grid-cols-[minmax(18rem,1fr)_7rem_10rem_9rem] items-center border-b border-ink-200 py-2 pr-3 hover:bg-primary-50/40",
            task.archived && "opacity-60"
          )}
        >
          <div className="flex min-w-0 items-center" style={{ paddingLeft: `${depth * 24 + 8}px` }}>
            {descendants.length > 0 ? (
              <button
                type="button"
                className="mr-1 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-ink-200"
                onClick={() => toggle(task.id)}
                aria-label={isCollapsed ? "Expand children" : "Collapse children"}
              >
                <IconChevronRight size={14} className={cn("transition-transform", !isCollapsed && "rotate-90")} />
              </button>
            ) : (
              <span className="mr-1 h-6 w-6 shrink-0" aria-hidden />
            )}
            <button
              type="button"
              className="min-w-0 truncate text-left text-sm font-medium text-foreground hover:text-primary-700"
              onClick={() => onOpenTask(task.id)}
            >
              {task.title}
            </button>
          </div>
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <span className={cn("h-2 w-2 rounded-full", STATUS_ACCENT[task.status])} />
            {STATUS_LABEL[task.status]}
          </div>
          <div className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
            {agent ? (
              <>
                <AgentSeal agent={agent} scale="chip" />
                <span className="truncate">{agent.name}</span>
              </>
            ) : (
              <span className="italic">Unassigned</span>
            )}
          </div>
          <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
            <span className="inline-flex items-center gap-1" title="Tree children (decomposition — task-board.md §3.3)">
              <IconHierarchy size={13} /> {descendants.length}
            </span>
            {dependencyCount > 0 && (
              // A visually distinct pill (not a plain count, like "Tree
              // children") so a dependency edge is never mistaken for tree
              // nesting (task-board-overhaul.md §3.3): parent_task_id is the
              // skeleton, dependencies are ordering, drawn differently.
              <button
                type="button"
                className="inline-flex items-center gap-1 rounded-full bg-attention-surface px-1.5 py-0.5 font-semibold text-attention hover:brightness-95"
                title="Execution dependencies (must finish first) — open to see which"
                onClick={(event) => {
                  event.stopPropagation();
                  onOpenTask(task.id);
                }}
              >
                <IconGitBranch size={13} /> {dependencyCount}
              </button>
            )}
            {delivery && (
              <span
                data-testid="task-delivery-chip"
                className={cn(
                  "inline-flex items-center rounded-full px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
                  DELIVERY_TONE_PILL[delivery.tone]
                )}
              >
                {delivery.label}
              </span>
            )}
          </div>
        </div>
        {!isCollapsed && descendants.map((child) => row(child, depth + 1))}
      </div>
    );
  };

  const roots = children.get(null) ?? [];
  return (
    <div className="min-h-0 flex-1 overflow-auto px-4 pb-4 md:px-6" data-testid="task-tree">
      <div className="min-w-[42rem] overflow-hidden rounded-2xl border border-ink-300 bg-card shadow-[var(--elevation-raised)]">
        <div className="grid grid-cols-[minmax(18rem,1fr)_7rem_10rem_9rem] border-b border-ink-300 bg-ink-100 px-3 py-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
          <span>Task tree</span><span>Status</span><span>Assignee</span><span>Links</span>
        </div>
        {roots.map((task, index) => (
          // One root = one battle (task-board-overhaul.md §3.3): a zebra tint
          // per root group makes each root's whole flat subtree read as one
          // cluster at a glance, distinct from the next root's.
          <div key={task.id} className={index % 2 === 1 ? "bg-ink-50/60" : undefined}>
            {row(task, 0)}
          </div>
        ))}
        {roots.length === 0 && (
          <div className="p-10 text-center font-serif text-sm text-muted-foreground">No tasks match these filters.</div>
        )}
      </div>
    </div>
  );
}
