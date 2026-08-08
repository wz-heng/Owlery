import {
  IconAlertTriangle,
  IconCircleCheck,
  IconClock,
  IconExternalLink,
  IconFolder,
  IconPlayerPlay,
} from "@tabler/icons-react";

import type { TaskRun } from "../../api/tasks";
import type { Agent } from "../../stores/sessionStore";
import { AgentSeal } from "../ui/seal";
import { cn } from "../../lib/utils";
import { formatDate, RUN_LABEL } from "./taskPresentation";
import { TaskDeliveryPanel } from "./TaskDeliveryPanel";

interface TaskRunTimelineProps {
  runs: TaskRun[];
  agents: Agent[];
  onOpenSession?: (sessionId: string) => void;
}
export function TaskRunTimeline({ runs, agents, onOpenSession }: TaskRunTimelineProps) {
  const agentMap = new Map(agents.map((agent) => [agent.id, agent]));
  return (
    <section aria-labelledby="task-runs-title">
      <h3 id="task-runs-title" className="mb-3 flex items-center gap-2 font-serif text-base font-semibold">
        <IconPlayerPlay size={17} /> Attempts <span className="text-xs font-normal text-muted-foreground">{runs.length}</span>
      </h3>
      <div className="relative space-y-3 before:absolute before:bottom-4 before:left-[15px] before:top-4 before:w-px before:bg-ink-300">
        {[...runs].sort((a, b) => b.attempt_no - a.attempt_no).map((run) => {
          const agent = run.agent_id ? agentMap.get(run.agent_id) : undefined;
          const successful = run.state === "completed";
          const active = run.state === "running";
          return (
            <article key={run.id} className="relative ml-8 rounded-xl border border-ink-300 bg-card p-3 shadow-[var(--elevation-raised)]">
              <span className={cn("absolute -left-[25px] top-4 z-10 flex h-4 w-4 items-center justify-center rounded-full ring-4 ring-background", successful ? "bg-success text-white" : active ? "bg-attention text-white" : "bg-destructive text-white")}>
                {successful ? <IconCircleCheck size={11} /> : active ? <IconClock size={10} /> : <IconAlertTriangle size={10} />}
              </span>
              <header className="flex flex-wrap items-center gap-2">
                <strong className="text-sm">Attempt {run.attempt_no}</strong>
                <span className={cn("rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase", successful ? "bg-success-surface text-success" : active ? "bg-attention-surface text-attention" : "bg-destructive-surface text-destructive")}>{RUN_LABEL[run.state]}</span>
                <span className="ml-auto text-[11px] text-muted-foreground">{formatDate(run.claimed_at)}</span>
              </header>
              <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                {agent && <span className="inline-flex items-center gap-1.5"><AgentSeal agent={agent} scale="chip" /> {agent.name}</span>}
                <span className="inline-flex items-center gap-1"><IconFolder size={13} /> {run.workspace_mode}</span>
                {run.cost != null && <span>${run.cost.toFixed(4)}</span>}
                {run.last_heartbeat_at && <span>Heartbeat {formatDate(run.last_heartbeat_at)}</span>}
              </div>
              {run.summary && <p className="mt-3 whitespace-pre-wrap text-sm leading-5 text-ink-800">{run.summary}</p>}
              {run.error && <p className="mt-2 rounded-lg bg-destructive-surface p-2 text-xs text-destructive">{run.error}</p>}
              {run.metadata && Object.keys(run.metadata).length > 0 && (
                <details className="mt-2 rounded-lg bg-ink-100 p-2 text-xs">
                  <summary className="cursor-pointer font-medium text-ink-700">Structured evidence</summary>
                  <pre className="mt-2 overflow-auto whitespace-pre-wrap font-mono text-[11px] text-ink-800">{JSON.stringify(run.metadata, null, 2)}</pre>
                </details>
              )}
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <code className="max-w-full truncate rounded bg-ink-100 px-2 py-1 text-[10px] text-muted-foreground" title={run.workspace_path}>{run.workspace_path}</code>
                {run.session_id && onOpenSession && (
                  <button type="button" className="ml-auto inline-flex items-center gap-1 text-xs font-medium text-primary-700 hover:underline" onClick={() => onOpenSession(run.session_id!)}>
                    Open session <IconExternalLink size={13} />
                  </button>
                )}
              </div>
              {run.workspace_mode === "git_worktree" && run.state === "completed" && (
                <TaskDeliveryPanel run={run} />
              )}
            </article>
          );
        })}
        {runs.length === 0 && <p className="ml-8 rounded-xl bg-ink-100 p-4 text-center text-xs italic text-muted-foreground">No worker has claimed this task.</p>}
      </div>
    </section>
  );
}
