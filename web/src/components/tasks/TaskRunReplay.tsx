import { useEffect, useState } from "react";
import {
  IconAlertTriangle,
  IconBan,
  IconChevronDown,
  IconChevronRight,
  IconClock,
  IconExternalLink,
  IconGitBranch,
  IconHistory,
  IconLoader2,
  IconMessage,
  IconPlayerStop,
  IconTool,
  IconWallet,
} from "@tabler/icons-react";

import type { ReplayEvent, ReplayTimeline } from "../../api/tasks";
import { useTaskStore } from "../../stores/taskStore";
import { cn } from "../../lib/utils";
import { formatDate } from "./taskPresentation";

/** Attempt-replay timeline panel (docs/plans/attempt-replay.md §3.3):
 * renders the merged, time-ordered event stream for one Task Board run,
 * lazily fetched on first expand. Answers the three post-mortem questions
 * the plan is built around — what it was last doing, how it died, and how
 * long each silence lasted and what bracketed it — directly in the UI,
 * without the reader having to cross-reference multiple tables. */
export function TaskRunReplay({
  taskId,
  runId,
  onOpenSession,
}: {
  taskId: string;
  runId: string;
  onOpenSession?: (sessionId: string) => void;
}) {
  const replay = useTaskStore((state) => state.replays[runId]);
  const loading = useTaskStore((state) => state.loadingReplay[runId]);

  useEffect(() => {
    if (!replay) void useTaskStore.getState().loadRunReplay(taskId, runId);
  }, [taskId, runId, replay]);

  if (loading && !replay) {
    return (
      <p className="flex items-center gap-1.5 py-4 text-xs text-muted-foreground">
        <IconLoader2 size={13} className="animate-spin" /> Loading timeline…
      </p>
    );
  }
  if (!replay) {
    return <p className="py-4 text-xs italic text-muted-foreground">No timeline yet — this attempt never started a worker session.</p>;
  }
  return <ReplayBody replay={replay} onOpenSession={onOpenSession} />;
}

function ReplayBody({
  replay,
  onOpenSession,
}: {
  replay: ReplayTimeline;
  onOpenSession?: (sessionId: string) => void;
}) {
  if (replay.timeline.length === 0 && !replay.unobserved_prefix && !replay.untimed_anomalies) {
    return <p className="py-4 text-xs italic text-muted-foreground">No activity recorded yet.</p>;
  }
  return (
    <div className="space-y-1 py-2">
      {replay.unobserved_prefix && (
        <div className="mb-2 rounded-lg bg-ink-100 p-2 text-[11px] italic text-muted-foreground">
          {replay.unobserved_prefix.summary}
        </div>
      )}
      {replay.untimed_anomalies && (
        <div className="mb-2 rounded-lg border border-destructive/30 bg-destructive-surface p-2 text-[11px] text-destructive">
          {replay.untimed_anomalies.summary}
        </div>
      )}
      {replay.timeline.map((event, i) =>
        event.kind === "gap" ? (
          <GapBlock key={`gap-${i}`} event={event} />
        ) : event.kind === "turn_terminal" ? (
          <TerminalRow key={`${event.kind}-${event.ts}-${i}`} event={event} />
        ) : (
          <EventRow key={`${event.kind}-${event.ts}-${i}`} event={event} onOpenSession={onOpenSession} />
        )
      )}
    </div>
  );
}

/** The gap "black hole" block (attempt-replay.md §3.3): duration
 * front-and-center, plus what bracketed the silence on each side — this is
 * the direct answer to "how long was the blank spot, and what's on either
 * end of it." */
function GapBlock({ event }: { event: ReplayEvent }) {
  const duration = Number(event.detail.duration_seconds ?? 0);
  const before = event.detail.before as ReplayEvent | undefined;
  const after = event.detail.after as ReplayEvent | undefined;
  return (
    <div className="my-2 rounded-xl border-2 border-dashed border-ink-400 bg-ink-900 p-3 text-ink-100">
      <div className="flex items-center gap-2 font-mono text-sm font-semibold">
        <IconBan size={16} /> {formatDuration(duration)} of silence
      </div>
      <div className="mt-2 grid grid-cols-1 gap-1 text-[11px] text-ink-300 sm:grid-cols-2">
        <div>
          <span className="text-ink-500">before: </span>
          {before?.summary ?? "—"}
        </div>
        <div>
          <span className="text-ink-500">after: </span>
          {after?.summary ?? "—"}
        </div>
      </div>
    </div>
  );
}

/** The turn-termination invariant row (attempt-replay.md §3.1 point 2), the
 * direct answer to "how did it die": reason, exit code / signal, and a
 * stderr tail on demand. Rendered expanded-by-default when the turn didn't
 * cleanly complete, since that's exactly the case someone opened this
 * timeline to investigate. */
function TerminalRow({ event }: { event: ReplayEvent }) {
  const reason = String(event.detail.reason ?? "unknown");
  const abnormal = reason !== "completed";
  const [open, setOpen] = useState(abnormal);
  return (
    <div
      className={cn(
        "rounded-lg border p-2 text-xs",
        abnormal ? "border-destructive-surface bg-destructive-surface/40" : "border-ink-300 bg-ink-100"
      )}
    >
      <button
        type="button"
        className="flex w-full items-center gap-2 text-left"
        onClick={() => setOpen((v) => !v)}
      >
        {open ? <IconChevronDown size={13} /> : <IconChevronRight size={13} />}
        {abnormal ? (
          <IconAlertTriangle size={14} className="text-destructive" />
        ) : (
          <IconPlayerStop size={14} className="text-muted-foreground" />
        )}
        <span className={cn("font-medium", abnormal && "text-destructive")}>{event.summary}</span>
        <span className="ml-auto text-[10px] text-muted-foreground">{formatDate(event.ts)}</span>
      </button>
      {open && (
        <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 pl-5 text-[11px] text-ink-700 sm:grid-cols-3">
          <Field label="reason" value={reason} />
          <Field label="exit code" value={event.detail.exit_code} />
          <Field label="signal" value={event.detail.signal} />
          <Field label="escalation" value={event.detail.escalation} />
          {event.detail.reason_detail != null &&
            Object.keys(event.detail.reason_detail as object).length > 0 && (
              <Field label="detail" value={JSON.stringify(event.detail.reason_detail)} />
            )}
          {typeof event.detail.stderr_tail === "string" && event.detail.stderr_tail && (
            <div className="col-span-full">
              <span className="text-muted-foreground">stderr tail</span>
              <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-ink-900 p-2 font-mono text-[10px] text-ink-100">
                {event.detail.stderr_tail as string}
              </pre>
            </div>
          )}
        </dl>
      )}
    </div>
  );
}

function Field({ label, value }: { label: string; value: unknown }) {
  if (value === null || value === undefined || value === "") return null;
  return (
    <div>
      <span className="text-muted-foreground">{label}: </span>
      <span className="font-mono">{String(value)}</span>
    </div>
  );
}

const KIND_ICON: Record<string, typeof IconMessage> = {
  message: IconMessage,
  tool_call: IconTool,
  tool_result: IconTool,
  turn_usage: IconWallet,
  task_event: IconHistory,
  delegation: IconGitBranch,
  bg_task_started: IconClock,
  bg_task_finished: IconClock,
};

function EventRow({
  event,
  onOpenSession,
}: {
  event: ReplayEvent;
  onOpenSession?: (sessionId: string) => void;
}) {
  const Icon = KIND_ICON[event.kind] ?? IconHistory;
  const delegationId = event.kind === "delegation" ? (event.detail.delegation_id as string | undefined) : undefined;
  return (
    <div className="flex items-start gap-2 rounded px-2 py-1 text-xs hover:bg-ink-100">
      <Icon size={13} className="mt-0.5 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1 truncate" title={event.summary}>
        {event.summary}
      </span>
      {delegationId && onOpenSession && (
        <button
          type="button"
          className="inline-flex shrink-0 items-center gap-1 text-primary-700 hover:underline"
          onClick={() => onOpenSession(delegationId)}
        >
          Open <IconExternalLink size={11} />
        </button>
      )}
      <span className="shrink-0 text-[10px] text-muted-foreground">{formatDate(event.ts)}</span>
    </div>
  );
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${Math.round(seconds % 60)}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}
