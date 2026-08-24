import { useEffect, useMemo, useState } from "react";
import {
  IconArchive,
  IconCalendar,
  IconCircleCheck,
  IconGitBranch,
  IconPaperclip,
  IconRefresh,
  IconTrash,
  IconX,
} from "@tabler/icons-react";

import type { Task, TaskArtifact, TaskDetail, TaskRun, TaskComment, WorkspaceMode } from "../../api/tasks";
import type { Agent } from "../../stores/sessionStore";
import { cn } from "../../lib/utils";
import { STATUS_ACCENT, STATUS_LABEL, formatBytes, formatDate } from "./taskPresentation";
import { TaskComments } from "./TaskComments";
import { TaskRunTimeline } from "./TaskRunTimeline";

interface TaskDrawerProps {
  task: Task;
  detail?: TaskDetail;
  runs: TaskRun[];
  comments: TaskComment[];
  artifacts: TaskArtifact[];
  allTasks: Task[];
  agents: Agent[];
  loading: boolean;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onSave: (input: { title: string; body: string; priority: number; assignee_agent_id: string | null; scheduled_at: string | null; workspace_mode: WorkspaceMode | null }) => Promise<void>;
  onLifecycle: (operation: "triage" | "specify" | "ready" | "unblock" | "cancel") => Promise<boolean>;
  onArchive: (archived: boolean) => Promise<void>;
  onComment: (body: string) => Promise<boolean>;
  onAddDependency: (dependencyId: string) => Promise<boolean>;
  onRemoveDependency: (dependencyId: string) => Promise<boolean>;
  /** Orchestrator-side container-card close (task-board-gaps.md §3.3) —
   * distinct from the worker `complete`/`block` protocol; see `closeEligible`
   * below for when this is even offered. */
  onCloseTask: (summary: string) => Promise<boolean>;
  onRefresh: () => void;
  onOpenTask: (taskId: string) => void;
  onOpenSession?: (sessionId: string) => void;
}

export function TaskDrawer(props: TaskDrawerProps) {
  const { task, detail, agents, allTasks } = props;
  // A board-list card only carries `body_excerpt` (task-board-overhaul.md
  // §3.5); the full text lands separately via `detail` once it loads. Fall
  // back through detail -> full body -> excerpt so the field is never
  // outright empty, then upgrade to the full text below without clobbering
  // an in-progress edit.
  const fallbackBody = task.body ?? task.body_excerpt ?? "";
  const [title, setTitle] = useState(task.title);
  const [body, setBody] = useState(detail?.body ?? fallbackBody);
  const [priority, setPriority] = useState(task.priority);
  const [assignee, setAssignee] = useState(task.assignee_agent_id ?? "");
  const [scheduledAt, setScheduledAt] = useState(task.scheduled_at ? task.scheduled_at.slice(0, 16) : "");
  const [workspaceMode, setWorkspaceMode] = useState<WorkspaceMode | "">(task.workspace_mode ?? "");
  const [dependency, setDependency] = useState("");
  const [closing, setClosing] = useState(false);
  const [closeSummary, setCloseSummary] = useState("");

  useEffect(() => {
    if (detail?.body !== undefined) {
      const fullBody = detail.body;
      setBody((current) => (current === fallbackBody ? fullBody : current));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail?.body]);

  const dependencies = useMemo(
    () => detail?.dependencies ?? task.dependencies ?? [],
    [detail?.dependencies, task.dependencies]
  );
  const dependents = detail?.dependents ?? task.dependents ?? [];
  const children = detail?.children ?? allTasks.filter((item) => item.parent_task_id === task.id);
  // Container-card close (task-board-gaps.md §3.3): offered only for a
  // decomposition card (has children) that was never itself dispatched a
  // run — mirrors the server's three rejections exactly, so the tooltip
  // never claims something the backend would actually accept.
  const isTerminal = task.status === "done" || task.status === "cancelled";
  // `props.runs` defaults to `[]` before `loadTaskDetail` resolves, which is
  // indistinguishable from "genuinely has no runs" — enabling Close during
  // that window would let a previously-run container pass the client check
  // and still 409 against the server's run-history rejection (Snape review).
  // `detail` lands in the same store write as `runs` (taskStore.ts
  // `loadTaskDetail`), so its presence is a race-free "runs are current" signal.
  const runsLoaded = detail !== undefined;
  const hasRunHistory = props.runs.length > 0;
  const openChildren = children.filter((child) => child.status !== "done" && child.status !== "cancelled");
  const closeDisabledReason = !runsLoaded
    ? "Loading task history…"
    : isTerminal
      ? "Task is already terminal"
      : task.current_run_id != null
        ? "A running task cannot be closed"
        : hasRunHistory
          ? "Has run history — close through the worker completion protocol instead"
          : openChildren.length > 0
            ? `${openChildren.length} child task${openChildren.length === 1 ? "" : "s"} not yet terminal`
            : null;
  const dependencyCandidates = useMemo(
    () => allTasks.filter((item) => item.id !== task.id && item.board_id === task.board_id && !dependencies.some((dep) => dep.id === item.id)),
    [allTasks, dependencies, task.board_id, task.id]
  );
  const bodyBaseline = detail?.body ?? fallbackBody;
  const dirty = title !== task.title || body !== bodyBaseline || priority !== task.priority || assignee !== (task.assignee_agent_id ?? "") || scheduledAt !== (task.scheduled_at ? task.scheduled_at.slice(0, 16) : "") || workspaceMode !== (task.workspace_mode ?? "");
  // A list card's `body` field is only an excerpt; saving before the full
  // text lands would silently truncate it (whatever the excerpt-derived
  // `body` state currently holds becomes the ENTIRE saved body, since save
  // always round-trips every field). Block editing and saving until the
  // single-task detail fetch resolves — typically near-instant, and the
  // field auto-upgrades to the full text the moment it does.
  const bodyReady = detail?.body !== undefined;

  return (
    <div className="fixed inset-0 z-40 bg-ink-950/25 backdrop-blur-[1px]" onMouseDown={(event) => { if (event.target === event.currentTarget) props.onClose(); }}>
      <aside className="absolute inset-0 flex flex-col bg-background shadow-[var(--elevation-overlay)] sm:inset-y-0 sm:left-auto sm:w-[min(46rem,92vw)] sm:border-l sm:border-ink-300" role="dialog" aria-modal="true" aria-label={`Task: ${task.title}`}>
        <header className="flex shrink-0 items-center gap-3 border-b border-ink-300 px-4 py-3 sm:px-6">
          <span className={cn("h-3 w-3 rounded-full", STATUS_ACCENT[task.status])} />
          <div className="min-w-0 flex-1"><p className="truncate font-serif text-lg font-semibold">{task.title}</p><p className="text-[11px] text-muted-foreground">{STATUS_LABEL[task.status]} · {task.id}</p></div>
          <button type="button" className="rounded-lg p-2 text-muted-foreground hover:bg-ink-200" onClick={props.onRefresh} aria-label="Refresh task"><IconRefresh size={17} className={cn(props.loading && "animate-spin")} /></button>
          <button type="button" className="rounded-lg p-2 text-muted-foreground hover:bg-ink-200" onClick={props.onClose} aria-label="Close task"><IconX size={19} /></button>
        </header>

        {props.error && <div className="mx-4 mt-3 rounded-lg border border-destructive/30 bg-destructive-surface px-3 py-2 text-xs text-destructive sm:mx-6">{props.error}</div>}

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-5 sm:px-6">
          <section className="grid gap-3 sm:grid-cols-2">
            <label className="sm:col-span-2"><span className="task-label">Title</span><input className="task-input" value={title} disabled={isTerminal} onChange={(event) => setTitle(event.target.value)} /></label>
            <label className="sm:col-span-2"><span className="task-label">Description{!bodyReady && " (loading full text…)"}</span><textarea className="task-input min-h-28 resize-y py-2" value={body} disabled={isTerminal || !bodyReady} onChange={(event) => setBody(event.target.value)} /></label>
            <label><span className="task-label">Assignee</span><select className="task-input" value={assignee} disabled={task.status === "running" || isTerminal} onChange={(event) => setAssignee(event.target.value)}><option value="">Unassigned</option>{agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select></label>
            <label><span className="task-label">Priority</span><select className="task-input" value={priority} disabled={isTerminal} onChange={(event) => setPriority(Number(event.target.value))}>{[3, 2, 1, 0].map((p) => <option key={p} value={p}>P{p}</option>)}</select></label>
            <label><span className="task-label">Schedule</span><span className="relative block"><IconCalendar size={15} className="absolute left-2.5 top-2.5 text-muted-foreground" /><input type="datetime-local" className="task-input pl-8" value={scheduledAt} disabled={isTerminal} onChange={(event) => setScheduledAt(event.target.value)} /></span></label>
            <label><span className="task-label">Workspace</span><select className="task-input" value={workspaceMode} disabled={task.status === "running" || isTerminal} onChange={(event) => setWorkspaceMode(event.target.value as WorkspaceMode | "")}><option value="">Board default</option><option value="shared">Shared</option><option value="copy">Copy</option><option value="git_worktree">Git worktree</option></select></label>
          </section>

          {task.status === "blocked" && <div className="mt-4 rounded-xl border border-destructive/30 bg-destructive-surface p-3"><p className="text-xs font-semibold uppercase tracking-wide text-destructive">Blocked · {task.blocked_kind}</p><p className="mt-1 whitespace-pre-wrap text-sm text-ink-800">{task.blocked_reason ?? "No reason recorded."}</p></div>}
          {task.status === "done" && task.verdict === "fail" && (
            <div className="mt-4 rounded-xl border border-destructive/30 bg-destructive-surface p-3">
              <p className="text-xs font-semibold uppercase tracking-wide text-destructive">Review failed</p>
              <p className="mt-1 text-sm text-ink-800">This task completed with verdict=fail — dependents will not unblock on it, even though the task itself is done.</p>
            </div>
          )}
          {task.result_summary && <div className="mt-4 rounded-xl border border-success/30 bg-success-surface p-3"><p className="text-xs font-semibold uppercase tracking-wide text-success">Outcome</p><p className="mt-1 whitespace-pre-wrap text-sm text-ink-800">{task.result_summary}</p></div>}

          <div className="mt-5 flex flex-wrap gap-2 border-y border-ink-300 py-3">
            {task.status === "triage" && <Action label="Specify" onClick={() => props.onLifecycle("specify")} />}
            {task.status === "todo" && <><Action label="Mark ready" onClick={() => props.onLifecycle("ready")} /><Action label="Move to triage" subtle onClick={() => props.onLifecycle("triage")} /></>}
            {task.status === "ready" && <Action label="Move to triage" subtle onClick={() => props.onLifecycle("triage")} />}
            {task.status === "blocked" && <Action label="Unblock" onClick={() => props.onLifecycle("unblock")} />}
            {/* cancelled is reachable directly from triage/todo/ready/blocked
                (task-board-gaps.md §3.4) — a running task must stop its run
                first, so it's deliberately excluded here. */}
            {(["triage", "todo", "ready", "blocked"] as string[]).includes(task.status) && <Action label="Cancel" destructive onClick={() => props.onLifecycle("cancel")} />}
            {children.length > 0 && !closing && (
              <button
                type="button"
                className="inline-flex h-8 items-center gap-1.5 rounded-lg px-3 text-xs font-medium text-muted-foreground hover:bg-ink-200 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
                disabled={closeDisabledReason !== null}
                title={closeDisabledReason ?? "Close this container once every child task has settled"}
                onClick={() => setClosing(true)}
              >
                <IconCircleCheck size={14} /> Close
              </button>
            )}
            <button type="button" className="ml-auto inline-flex h-8 items-center gap-1.5 rounded-lg px-3 text-xs text-muted-foreground hover:bg-ink-200" onClick={() => props.onArchive(!task.archived)}><IconArchive size={14} /> {task.archived ? "Unarchive" : "Archive"}</button>
            {dirty && !isTerminal && <button type="button" className="h-8 rounded-lg bg-primary-700 px-3 text-xs font-semibold text-white disabled:opacity-50" disabled={props.busy || !title.trim() || !bodyReady} title={!bodyReady ? "Waiting for the full task text to load" : undefined} onClick={() => void props.onSave({ title: title.trim(), body, priority, assignee_agent_id: assignee || null, scheduled_at: scheduledAt ? new Date(scheduledAt).toISOString() : null, workspace_mode: workspaceMode || null })}>Save changes</button>}
            {closing && (
              <form
                className="flex w-full flex-col gap-2 rounded-lg border border-ink-300 bg-card p-2.5"
                onSubmit={async (event) => {
                  event.preventDefault();
                  if (await props.onCloseTask(closeSummary.trim())) {
                    setClosing(false);
                    setCloseSummary("");
                  }
                }}
              >
                <textarea
                  autoFocus
                  className="task-input min-h-16 resize-y py-2 text-xs"
                  value={closeSummary}
                  onChange={(event) => setCloseSummary(event.target.value)}
                  placeholder="Summarize why this container is done…"
                  aria-label="Close summary"
                />
                <div className="flex justify-end gap-2">
                  <button type="button" className="h-8 rounded-lg px-3 text-xs text-muted-foreground hover:bg-ink-200" onClick={() => { setClosing(false); setCloseSummary(""); }}>Cancel</button>
                  <button type="submit" className="h-8 rounded-lg bg-primary-700 px-3 text-xs font-semibold text-white disabled:opacity-50" disabled={props.busy || !closeSummary.trim()}>Confirm close</button>
                </div>
              </form>
            )}
          </div>

          <section className="mt-6">
            <h3 className="mb-3 flex items-center gap-2 font-serif text-base font-semibold"><IconGitBranch size={17} /> Relationships</h3>
            <Relation label="Parent" tasks={task.parent_task_id ? allTasks.filter((item) => item.id === task.parent_task_id) : []} onOpen={props.onOpenTask} />
            <Relation label="Children" tasks={children} onOpen={props.onOpenTask} />
            <div className="mt-3"><p className="task-label">Dependencies (execution prerequisites)</p><div className="flex flex-wrap gap-1.5">{dependencies.map((item) => <span key={item.id} className="inline-flex items-center gap-1 rounded-full border border-ink-300 bg-card px-2 py-1 text-xs"><button type="button" onClick={() => props.onOpenTask(item.id)}>{item.title}</button><button type="button" className="text-muted-foreground hover:text-destructive disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:text-muted-foreground" disabled={isTerminal} title={isTerminal ? "Task is already terminal" : undefined} onClick={() => void props.onRemoveDependency(item.id)} aria-label={`Remove dependency ${item.title}`}><IconX size={12} /></button></span>)}{dependencies.length === 0 && <span className="text-xs italic text-muted-foreground">None</span>}</div>{!isTerminal && <div className="mt-2 flex gap-2"><select className="task-input h-8 flex-1 py-0 text-xs" value={dependency} onChange={(event) => setDependency(event.target.value)}><option value="">Add dependency…</option>{dependencyCandidates.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select><button type="button" className="h-8 rounded-lg border border-ink-300 px-3 text-xs disabled:opacity-50" disabled={!dependency} onClick={async () => { if (await props.onAddDependency(dependency)) setDependency(""); }}>Add</button></div>}</div>
            <Relation label="Dependents" tasks={dependents.map((item) => allTasks.find((taskItem) => taskItem.id === item.id) ?? ({ ...task, ...item, id: item.id } as Task))} onOpen={props.onOpenTask} />
          </section>

          <div className="mt-8"><TaskRunTimeline runs={props.runs} agents={agents} onOpenSession={props.onOpenSession} onOpenTask={props.onOpenTask} /></div>
          <div className="mt-8"><Artifacts artifacts={props.artifacts} taskId={task.id} /></div>
          <div className="mt-8 pb-8"><TaskComments comments={props.comments} agents={agents} busy={props.busy} onComment={props.onComment} /></div>
        </div>
      </aside>
    </div>
  );
}

function Action({ label, onClick, subtle, destructive }: { label: string; onClick: () => Promise<boolean>; subtle?: boolean; destructive?: boolean }) {
  return <button type="button" className={cn("h-8 rounded-lg px-3 text-xs font-medium", destructive ? "text-destructive hover:bg-destructive-surface" : subtle ? "text-muted-foreground hover:bg-ink-200" : "bg-primary-50 text-primary-700 hover:bg-primary-100")} onClick={() => void onClick()}>{label}</button>;
}

function Relation({ label, tasks, onOpen }: { label: string; tasks: Task[]; onOpen: (id: string) => void }) {
  return <div className="mt-3"><p className="task-label">{label}</p><div className="flex flex-wrap gap-1.5">{tasks.map((item) => <button type="button" key={item.id} className="rounded-full border border-ink-300 bg-card px-2.5 py-1 text-xs hover:border-primary/40 hover:text-primary-700" onClick={() => onOpen(item.id)}>{item.title}</button>)}{tasks.length === 0 && <span className="text-xs italic text-muted-foreground">None</span>}</div></div>;
}

function Artifacts({ artifacts, taskId }: { artifacts: TaskArtifact[]; taskId: string }) {
  return <section><h3 className="mb-3 flex items-center gap-2 font-serif text-base font-semibold"><IconPaperclip size={17} /> Artifacts <span className="text-xs font-normal text-muted-foreground">{artifacts.filter((item) => !item.deleted_at).length}</span></h3><div className="space-y-2">{artifacts.map((artifact) => <div key={artifact.id} className={cn("flex items-center gap-3 rounded-xl border border-ink-300 bg-card p-3", artifact.deleted_at && "opacity-50")}><IconPaperclip size={16} className="text-muted-foreground" /><div className="min-w-0 flex-1"><p className="truncate text-sm font-medium">{artifact.name}</p><p className="truncate font-mono text-[10px] text-muted-foreground">{formatBytes(artifact.size)} · {artifact.sha256.slice(0, 12)} · {formatDate(artifact.created_at)}</p></div>{!artifact.deleted_at && <a className="text-xs font-medium text-primary-700 hover:underline" href={artifact.download_url ?? `/api/tasks/${taskId}/artifacts/${artifact.id}`} target="_blank" rel="noreferrer">Open</a>}{artifact.deleted_at && <span className="inline-flex items-center gap-1 text-[10px] text-muted-foreground"><IconTrash size={12} /> deleted</span>}</div>)}{artifacts.length === 0 && <p className="rounded-xl bg-ink-100 p-4 text-center text-xs italic text-muted-foreground">No artifacts declared.</p>}</div></section>;
}
