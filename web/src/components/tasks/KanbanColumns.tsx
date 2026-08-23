import { useMemo, useState } from "react";
import { IconArchive } from "@tabler/icons-react";

import type { Task, TaskStatus } from "../../api/tasks";
import type { Agent } from "../../stores/sessionStore";
import { cn } from "../../lib/utils";
import { dragOperation, TASK_STATUSES, useTaskStore } from "../../stores/taskStore";
import { TaskCard } from "./TaskCard";
import { isArchivableDoneTask, STATUS_ACCENT, STATUS_LABEL } from "./taskPresentation";

/** Default render window for the Done column (task-board-overhaul.md §3.4);
 * "show more" reveals this many additional cards per click, all already
 * loaded client-side (see the board-fetch limit comment in taskStore). */
const DONE_PAGE_SIZE = 15;

interface KanbanColumnsProps {
  tasks: Task[];
  agents: Agent[];
  onOpenTask: (taskId: string) => void;
  onMoveTask: (taskId: string, target: TaskStatus) => Promise<boolean>;
}
export function KanbanColumns({ tasks, agents, onOpenTask, onMoveTask }: KanbanColumnsProps) {
  const [draggedId, setDraggedId] = useState<string | null>(null);
  const [over, setOver] = useState<TaskStatus | null>(null);
  const [doneVisible, setDoneVisible] = useState(DONE_PAGE_SIZE);
  const [archiving, setArchiving] = useState(false);
  const grouped = useMemo(
    () =>
      Object.fromEntries(
        TASK_STATUSES.map((status) => [
          status,
          tasks
            .filter((task) => task.status === status)
            .sort((a, b) => b.priority - a.priority || a.created_at.localeCompare(b.created_at)),
        ])
      ) as Record<TaskStatus, Task[]>,
    [tasks]
  );
  const agentMap = useMemo(() => new Map(agents.map((agent) => [agent.id, agent])), [agents]);
  const dragged = draggedId ? tasks.find((task) => task.id === draggedId) : null;
  const doneTasks = grouped.done;
  const doneShown = doneTasks.slice(0, doneVisible);
  const doneHiddenCount = doneTasks.length - doneShown.length;
  const archivableInView = doneShown.filter(isArchivableDoneTask);

  return (
    <div className="task-kanban flex min-h-0 flex-1 snap-x gap-3 overflow-x-auto px-4 pb-4 md:px-6" data-testid="task-kanban">
      {TASK_STATUSES.map((status) => {
        const canDrop = Boolean(dragged && dragOperation(dragged.status, status));
        return (
          <section
            key={status}
            className={cn(
              "flex w-[82vw] max-w-[21rem] shrink-0 snap-start flex-col rounded-2xl border border-ink-300 bg-ink-100/70 md:w-[18rem]",
              over === status && canDrop && "border-primary bg-primary-50/70 ring-2 ring-primary/20",
              over === status && !canDrop && "border-destructive/50"
            )}
            aria-label={`${STATUS_LABEL[status]} tasks`}
            onDragEnter={(event) => {
              event.preventDefault();
              setOver(status);
              const id = event.dataTransfer.getData("application/x-owlery-task");
              if (id) setDraggedId(id);
            }}
            onDragOver={(event) => {
              if (canDrop) event.preventDefault();
            }}
            onDragLeave={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget as Node)) setOver(null);
            }}
            onDrop={(event) => {
              event.preventDefault();
              const id = event.dataTransfer.getData("application/x-owlery-task") || draggedId;
              setOver(null);
              setDraggedId(null);
              if (id && canDrop) void onMoveTask(id, status);
            }}
          >
            <header className="flex h-11 items-center gap-2 border-b border-ink-300 px-3">
              <span className={cn("h-2 w-2 rounded-full", STATUS_ACCENT[status])} />
              <h2 className="font-serif text-sm font-semibold text-foreground">{STATUS_LABEL[status]}</h2>
              <span className="ml-auto rounded-full bg-card px-2 py-0.5 text-[11px] tabular-nums text-muted-foreground shadow-[var(--elevation-inset)]">
                {grouped[status].length}
              </span>
            </header>
            <div className="flex min-h-28 flex-1 flex-col gap-2.5 overflow-y-auto p-2.5">
              {(status === "done" ? doneShown : grouped[status]).map((task) => (
                <div
                  key={task.id}
                  onDragStart={() => setDraggedId(task.id)}
                  onDragEnd={() => {
                    setDraggedId(null);
                    setOver(null);
                  }}
                >
                  <TaskCard
                    task={task}
                    agent={task.assignee_agent_id ? agentMap.get(task.assignee_agent_id) : undefined}
                    onOpen={onOpenTask}
                  />
                </div>
              ))}
              {grouped[status].length === 0 && (
                <div className="flex min-h-24 items-center justify-center rounded-xl text-xs italic text-muted-foreground">
                  No {STATUS_LABEL[status].toLocaleLowerCase()} tasks
                </div>
              )}
              {status === "done" && doneHiddenCount > 0 && (
                <button
                  type="button"
                  className="h-8 shrink-0 rounded-lg border border-ink-300 text-[11px] font-medium text-ink-700 hover:bg-ink-200"
                  onClick={() => setDoneVisible((current) => current + DONE_PAGE_SIZE)}
                >
                  Show {Math.min(DONE_PAGE_SIZE, doneHiddenCount)} more ({doneHiddenCount} older)
                </button>
              )}
              {status === "done" && archivableInView.length > 0 && (
                <button
                  type="button"
                  className="inline-flex h-8 shrink-0 items-center justify-center gap-1.5 rounded-lg border border-ink-300 text-[11px] font-medium text-ink-700 hover:bg-ink-200 disabled:opacity-50"
                  disabled={archiving}
                  onClick={async () => {
                    setArchiving(true);
                    try {
                      const store = useTaskStore.getState();
                      for (const task of archivableInView) {
                        await store.setTaskArchived(task.id, true);
                      }
                    } finally {
                      setArchiving(false);
                    }
                  }}
                >
                  <IconArchive size={13} /> Archive {archivableInView.length} delivered
                </button>
              )}
            </div>
          </section>
        );
      })}
    </div>
  );
}
