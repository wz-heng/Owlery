import { useEffect, useMemo, useState } from "react";
import { IconAlertTriangle, IconClipboardList, IconPlus, IconX } from "@tabler/icons-react";

import type { CreateBoardInput, CreateTaskInput, TaskEvent, WorkspaceMode } from "../../api/tasks";
import type { Agent } from "../../stores/sessionStore";
import { filterTasks, useTaskStore } from "../../stores/taskStore";
import { BoardToolbar } from "./BoardToolbar";
import { KanbanColumns } from "./KanbanColumns";
import { TaskDrawer } from "./TaskDrawer";
import { TaskTree } from "./TaskTree";
import "./tasks.css";

export interface TaskEventMessage {
  board_id: string;
  task_id: string | null;
  event: TaskEvent;
}

interface TaskBoardPageProps {
  token: string;
  agents: Agent[];
  activeAgentId: string | null;
  liveEvent?: TaskEventMessage | null;
  onOpenSession?: (sessionId: string) => void;
  onToggleSidebar?: () => void;
}

/** Full main-area Task Board surface. App.tsx only needs to pass identity and
 * route switching; all feature state and HTTP orchestration stays here. */
export function TaskBoardPage({ token, agents, activeAgentId, liveEvent, onOpenSession, onToggleSidebar }: TaskBoardPageProps) {
  const store = useTaskStore();
  const [showCreateTask, setShowCreateTask] = useState(false);
  const selectedBoard = store.boards.find((board) => board.id === store.selectedBoardId) ?? null;
  const allTasks = store.taskOrder.map((id) => store.tasksById[id]).filter(Boolean);
  const visibleTasks = useMemo(
    () => filterTasks(allTasks, store.filters, activeAgentId),
    [allTasks, store.filters, activeAgentId]
  );
  const selectedTask = store.selectedTaskId ? store.tasksById[store.selectedTaskId] : null;

  useEffect(() => {
    store.setToken(token);
    if (token) void store.loadBoards(true);
    // The store's action identities are stable; token is the only trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  useEffect(() => {
    if (store.selectedBoardId && token) void store.loadBoard(store.selectedBoardId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [store.selectedBoardId, token]);

  useEffect(() => {
    if (!liveEvent) return;
    store.applyTaskEvent(liveEvent.board_id, liveEvent.task_id, liveEvent.event);
    if (!liveEvent.event.payload.task && liveEvent.board_id === store.selectedBoardId) {
      void store.catchUp(liveEvent.board_id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveEvent]);

  useEffect(() => {
    if (store.selectedTaskId) void store.loadTaskDetail(store.selectedTaskId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [store.selectedTaskId]);

  const selectBoard = (boardId: string) => store.selectBoard(boardId);
  const openTask = (taskId: string) => store.selectTask(taskId);

  if (!store.loadingBoards && store.boards.length === 0) {
    return (
      <main className="task-board-page flex h-full min-h-0 flex-1 items-center justify-center bg-background p-5">
        <EmptyBoard onCreate={async (input) => Boolean(await store.createBoard(input))} busy={store.mutating} error={store.error} />
      </main>
    );
  }

  return (
    <main className="task-board-page flex h-full min-h-0 flex-1 flex-col overflow-hidden bg-background">
      <BoardToolbar
        boards={store.boards}
        selectedBoard={selectedBoard}
        dispatcher={selectedBoard ? store.dispatcher[selectedBoard.id] ?? null : null}
        view={store.view}
        filters={store.filters}
        agents={agents}
        mutating={store.mutating}
        onSelectBoard={selectBoard}
        onViewChange={store.setView}
        onFiltersChange={store.setFilters}
        onCreateBoard={async (input) => Boolean(await store.createBoard(input))}
        onUpdateBoard={async (input) => { if (selectedBoard) await store.updateBoard(selectedBoard.id, input); }}
        onArchiveBoard={async () => { if (selectedBoard) await store.setBoardArchived(selectedBoard.id, !selectedBoard.archived); }}
        onToggleDispatcher={async () => { if (selectedBoard) await store.setDispatcherEnabled(selectedBoard.id, !(store.dispatcher[selectedBoard.id]?.enabled ?? selectedBoard.dispatch_enabled)); }}
        onNewTask={() => setShowCreateTask(true)}
        onToggleSidebar={onToggleSidebar}
      />

      {selectedBoard && (
        <div className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2 text-[11px] text-muted-foreground md:px-6">
          <span className="truncate">{selectedBoard.description || selectedBoard.working_dir}</span>
          <span className="ml-auto tabular-nums">{visibleTasks.length} shown · {allTasks.filter((task) => task.status === "running").length} running</span>
        </div>
      )}

      {store.error && (
        <div className="mx-4 mb-2 flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive-surface px-3 py-2 text-xs text-destructive md:mx-6">
          <IconAlertTriangle size={15} className="mt-0.5 shrink-0" />
          <span className="flex-1">{store.error}</span>
          <button type="button" onClick={store.clearError} aria-label="Dismiss error"><IconX size={14} /></button>
        </div>
      )}

      {store.loadingTasks && allTasks.length === 0 ? (
        <div className="flex flex-1 items-center justify-center font-serif text-sm text-muted-foreground">Loading task board…</div>
      ) : store.view === "kanban" ? (
        <KanbanColumns tasks={visibleTasks} agents={agents} onOpenTask={openTask} onMoveTask={store.moveTask} />
      ) : (
        <TaskTree tasks={visibleTasks} agents={agents} onOpenTask={openTask} />
      )}

      {showCreateTask && selectedBoard && (
        <CreateTaskDialog
          agents={agents}
          tasks={allTasks}
          busy={store.mutating}
          onClose={() => setShowCreateTask(false)}
          onCreate={async (input) => {
            const task = await store.createTask(input);
            if (task) { setShowCreateTask(false); store.selectTask(task.id); }
          }}
        />
      )}

      {selectedTask && (
        <TaskDrawer
          key={selectedTask.id}
          task={selectedTask}
          detail={store.details[selectedTask.id]}
          runs={store.runs[selectedTask.id] ?? []}
          comments={store.comments[selectedTask.id] ?? []}
          artifacts={store.artifacts[selectedTask.id] ?? []}
          allTasks={allTasks}
          agents={agents}
          loading={store.loadingDetail}
          busy={store.mutating}
          error={store.error}
          onClose={() => store.selectTask(null)}
          onSave={async ({ assignee_agent_id, ...input }) => {
            await store.updateTask(selectedTask.id, {
              ...input,
              updated_at: selectedTask.updated_at,
            });
            if (assignee_agent_id !== selectedTask.assignee_agent_id) {
              await store.assignTask(selectedTask.id, assignee_agent_id);
            }
          }}
          onLifecycle={(operation) => store.lifecycle(selectedTask.id, operation)}
          onArchive={(archived) => store.setTaskArchived(selectedTask.id, archived)}
          onComment={(body) => store.addComment(selectedTask.id, body)}
          onAddDependency={(dependencyId) => store.addDependency(selectedTask.id, dependencyId)}
          onRemoveDependency={(dependencyId) => store.removeDependency(selectedTask.id, dependencyId)}
          onRefresh={() => void store.loadTaskDetail(selectedTask.id)}
          onOpenTask={openTask}
          onOpenSession={onOpenSession}
        />
      )}
    </main>
  );
}

function EmptyBoard({ onCreate, busy, error }: { onCreate: (input: CreateBoardInput) => Promise<boolean>; busy: boolean; error: string | null }) {
  const [workingDir, setWorkingDir] = useState("");
  const [name, setName] = useState("");
  return (
    <form className="w-full max-w-lg rounded-2xl border border-ink-300 bg-card p-6 text-center shadow-[var(--elevation-floating)]" onSubmit={(event) => { event.preventDefault(); void onCreate({ name: name.trim(), working_dir: workingDir.trim(), default_workspace_mode: "shared" }); }}>
      <IconClipboardList size={32} className="mx-auto text-primary-700" />
      <h1 className="mt-3 font-serif text-xl font-semibold">Create your first task board</h1>
      <p className="mt-2 text-sm text-muted-foreground">Give durable multi-agent work a project boundary, an owner, and a visible history.</p>
      {error && <p className="mt-3 text-xs text-destructive">{error}</p>}
      <div className="mt-5 space-y-2 text-left"><label><span className="task-label">Board name</span><input className="task-input" required value={name} onChange={(event) => setName(event.target.value)} placeholder="Owlery" /></label><label><span className="task-label">Working directory</span><input className="task-input font-mono text-xs" required value={workingDir} onChange={(event) => setWorkingDir(event.target.value)} placeholder="/absolute/project/path" /></label></div>
      <button type="submit" disabled={busy || !name.trim() || !workingDir.trim()} className="mt-4 inline-flex h-9 items-center gap-1.5 rounded-lg bg-primary-700 px-4 text-xs font-semibold text-white disabled:opacity-50"><IconPlus size={15} /> Create board</button>
    </form>
  );
}

function CreateTaskDialog({ agents, tasks, busy, onClose, onCreate }: { agents: Agent[]; tasks: import("../../api/tasks").Task[]; busy: boolean; onClose: () => void; onCreate: (input: CreateTaskInput) => Promise<void> }) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [assignee, setAssignee] = useState("");
  const [parent, setParent] = useState("");
  const [priority, setPriority] = useState(0);
  const [workspace, setWorkspace] = useState<WorkspaceMode | "">("");
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-ink-950/35 sm:items-center sm:p-4" role="dialog" aria-modal="true" aria-label="Create task">
      <form className="w-full rounded-t-2xl border border-ink-300 bg-background p-5 shadow-[var(--elevation-overlay)] sm:max-w-xl sm:rounded-2xl" onSubmit={(event) => { event.preventDefault(); void onCreate({ title: title.trim(), body: body.trim(), assignee_agent_id: assignee || null, parent_task_id: parent || null, priority, workspace_mode: workspace || null }); }}>
        <div className="mb-4 flex items-center justify-between"><h2 className="font-serif text-lg font-semibold">New task</h2><button type="button" onClick={onClose} className="rounded-md p-1 text-muted-foreground hover:bg-ink-200" aria-label="Close"><IconX size={18} /></button></div>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="sm:col-span-2"><span className="task-label">Outcome</span><input autoFocus required className="task-input" value={title} onChange={(event) => setTitle(event.target.value)} placeholder="What should be true when this is done?" /></label>
          <label className="sm:col-span-2"><span className="task-label">Description</span><textarea className="task-input min-h-24 resize-y py-2" value={body} onChange={(event) => setBody(event.target.value)} /></label>
          <label><span className="task-label">Assignee</span><select className="task-input" value={assignee} onChange={(event) => setAssignee(event.target.value)}><option value="">Unassigned</option>{agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select></label>
          <label><span className="task-label">Parent task</span><select className="task-input" value={parent} onChange={(event) => setParent(event.target.value)}><option value="">Top level</option>{tasks.filter((task) => !task.archived && task.status !== "done").map((task) => <option key={task.id} value={task.id}>{task.title}</option>)}</select></label>
          <label><span className="task-label">Priority</span><select className="task-input" value={priority} onChange={(event) => setPriority(Number(event.target.value))}>{[3, 2, 1, 0].map((p) => <option key={p} value={p}>P{p}</option>)}</select></label>
          <label><span className="task-label">Workspace</span><select className="task-input" value={workspace} onChange={(event) => setWorkspace(event.target.value as WorkspaceMode | "")}><option value="">Board default</option><option value="shared">Shared</option><option value="copy">Copy</option><option value="git_worktree">Git worktree</option></select></label>
        </div>
        <div className="mt-5 flex justify-end gap-2"><button type="button" className="h-9 rounded-lg px-3 text-xs text-muted-foreground hover:bg-ink-200" onClick={onClose}>Cancel</button><button type="submit" disabled={busy || !title.trim()} className="h-9 rounded-lg bg-primary-700 px-4 text-xs font-semibold text-white disabled:opacity-50">Create task</button></div>
      </form>
    </div>
  );
}
