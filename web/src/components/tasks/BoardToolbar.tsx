import { useState } from "react";
import {
  IconArchive,
  IconFilter,
  IconLayoutKanban,
  IconListTree,
  IconMenu2,
  IconPlayerPause,
  IconPlayerPlay,
  IconPlus,
  IconSearch,
  IconSettings,
  IconX,
} from "@tabler/icons-react";

import type {
  CreateBoardInput,
  DeliveryRetention,
  DispatcherStatus,
  TaskBoard,
  WorkspaceMode,
} from "../../api/tasks";
import type { Agent } from "../../stores/sessionStore";
import type { TaskBoardView, TaskFilters } from "../../stores/taskStore";
import { cn } from "../../lib/utils";

interface BoardToolbarProps {
  boards: TaskBoard[];
  selectedBoard: TaskBoard | null;
  dispatcher: DispatcherStatus | null;
  view: TaskBoardView;
  filters: TaskFilters;
  agents: Agent[];
  mutating: boolean;
  onSelectBoard: (boardId: string) => void;
  onViewChange: (view: TaskBoardView) => void;
  onFiltersChange: (patch: Partial<TaskFilters>) => void;
  onCreateBoard: (input: CreateBoardInput) => Promise<boolean>;
  onUpdateBoard: (input: Partial<CreateBoardInput>) => Promise<void>;
  onArchiveBoard: () => Promise<void>;
  onToggleDispatcher: () => Promise<void>;
  onNewTask: () => void;
  onToggleSidebar?: () => void;
}

export function BoardToolbar({
  boards,
  selectedBoard,
  dispatcher,
  view,
  filters,
  agents,
  mutating,
  onSelectBoard,
  onViewChange,
  onFiltersChange,
  onCreateBoard,
  onUpdateBoard,
  onArchiveBoard,
  onToggleDispatcher,
  onNewTask,
  onToggleSidebar,
}: BoardToolbarProps) {
  const [showFilters, setShowFilters] = useState(false);
  const [dialog, setDialog] = useState<"create" | "settings" | null>(null);

  return (
    <>
      <header className="shrink-0 border-b border-ink-300 bg-background/95 px-4 py-3 backdrop-blur md:px-6">
        <div className="flex flex-wrap items-center gap-2">
          {onToggleSidebar && (
            <button
              type="button"
              className="inline-flex h-9 w-9 items-center justify-center rounded-lg text-muted-foreground hover:bg-ink-200 md:hidden"
              onClick={onToggleSidebar}
              aria-label="Open sidebar"
            >
              <IconMenu2 size={18} />
            </button>
          )}
          <select
            className="h-9 min-w-44 max-w-64 rounded-lg border border-ink-400 bg-card px-3 text-sm font-semibold text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/20"
            value={selectedBoard?.id ?? ""}
            onChange={(event) => onSelectBoard(event.target.value)}
            aria-label="Task board"
          >
            {boards.map((board) => (
              <option key={board.id} value={board.id}>{board.name}{board.archived ? " (archived)" : ""}</option>
            ))}
          </select>

          <button
            type="button"
            className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-ink-300 bg-card px-3 text-xs font-medium text-foreground hover:border-primary/50 hover:bg-primary-50"
            onClick={() => setDialog("create")}
          >
            <IconPlus size={15} /> Board
          </button>

          {selectedBoard && (
            <button
              type="button"
              className="inline-flex h-9 w-9 items-center justify-center rounded-lg text-muted-foreground hover:bg-ink-200 hover:text-foreground"
              onClick={() => setDialog("settings")}
              aria-label="Board settings"
            >
              <IconSettings size={17} />
            </button>
          )}

          <div className="mx-1 hidden h-6 w-px bg-ink-300 sm:block" />
          <div className="inline-flex rounded-lg border border-ink-300 bg-ink-100 p-0.5">
            <ViewButton active={view === "kanban"} onClick={() => onViewChange("kanban")} label="Board">
              <IconLayoutKanban size={15} />
            </ViewButton>
            <ViewButton active={view === "tree"} onClick={() => onViewChange("tree")} label="Tree">
              <IconListTree size={15} />
            </ViewButton>
          </div>

          <button
            type="button"
            className={cn(
              "inline-flex h-9 items-center gap-1.5 rounded-lg px-3 text-xs font-medium",
              showFilters || filters.text || filters.assignee || filters.priority !== null || filters.mine
                ? "bg-primary-50 text-primary-700"
                : "text-muted-foreground hover:bg-ink-200"
            )}
            onClick={() => setShowFilters((value) => !value)}
          >
            <IconFilter size={15} /> Filters
          </button>

          <div className="ml-auto flex items-center gap-2">
            {dispatcher && (
              <>
                <div className="hidden items-center gap-1 lg:flex" aria-label="Running tasks by agent">
                  {Object.entries(dispatcher.running_by_agent)
                    .filter(([, count]) => count > 0)
                    .slice(0, 4)
                    .map(([agentId, count]) => (
                      <span key={agentId} className="rounded-full bg-ink-100 px-2 py-1 text-[10px] text-muted-foreground" title={`${agents.find((agent) => agent.id === agentId)?.name ?? agentId}: ${count} running`}>
                        {agents.find((agent) => agent.id === agentId)?.name ?? agentId.slice(0, 5)} · {count}
                      </span>
                    ))}
                </div>
                <button
                  type="button"
                  className={cn(
                    "inline-flex h-9 items-center gap-1.5 rounded-lg border px-3 text-xs font-medium",
                    dispatcher.enabled
                      ? "border-success/30 bg-success-surface text-success"
                      : "border-attention/30 bg-attention-surface text-attention"
                  )}
                  disabled={mutating}
                  onClick={() => void onToggleDispatcher()}
                  title={dispatcher.last_error ?? undefined}
                >
                  {dispatcher.enabled ? <IconPlayerPause size={15} /> : <IconPlayerPlay size={15} />}
                  <span className="hidden sm:inline">{dispatcher.enabled ? `${dispatcher.running} running` : "Paused"}</span>
                </button>
              </>
            )}
            <button
              type="button"
              className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-primary-700 px-3.5 text-xs font-semibold text-white shadow-[var(--elevation-raised)] hover:bg-primary-600 disabled:opacity-50"
              onClick={onNewTask}
              disabled={!selectedBoard || selectedBoard.archived}
            >
              <IconPlus size={16} /> New task
            </button>
          </div>
        </div>

        {showFilters && (
          <div className="mt-3 flex flex-wrap items-center gap-2 rounded-xl border border-ink-300 bg-ink-100 p-2">
            <label className="relative min-w-48 flex-1">
              <IconSearch size={15} className="absolute left-2.5 top-2.5 text-muted-foreground" />
              <input
                className="h-9 w-full rounded-lg border border-ink-300 bg-card pl-8 pr-3 text-sm outline-none focus:border-primary"
                value={filters.text}
                onChange={(event) => onFiltersChange({ text: event.target.value })}
                placeholder="Search title or description"
                aria-label="Search tasks"
              />
            </label>
            <select
              className="h-9 rounded-lg border border-ink-300 bg-card px-2 text-xs"
              value={filters.assignee}
              onChange={(event) => onFiltersChange({ assignee: event.target.value, mine: false })}
              aria-label="Filter by assignee"
            >
              <option value="">All assignees</option>
              {agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
            </select>
            <select
              className="h-9 rounded-lg border border-ink-300 bg-card px-2 text-xs"
              value={filters.priority ?? ""}
              onChange={(event) => onFiltersChange({ priority: event.target.value ? Number(event.target.value) : null })}
              aria-label="Filter by priority"
            >
              <option value="">All priorities</option>
              {[3, 2, 1, 0].map((priority) => <option key={priority} value={priority}>P{priority}</option>)}
            </select>
            <label className="inline-flex h-9 items-center gap-1.5 rounded-lg px-2 text-xs text-muted-foreground">
              <input type="checkbox" checked={filters.mine} onChange={(event) => onFiltersChange({ mine: event.target.checked, assignee: "" })} /> Mine
            </label>
            <label className="inline-flex h-9 items-center gap-1.5 rounded-lg px-2 text-xs text-muted-foreground">
              <input type="checkbox" checked={filters.includeArchived} onChange={(event) => onFiltersChange({ includeArchived: event.target.checked })} /> Archived
            </label>
          </div>
        )}
      </header>

      {dialog && (
        <BoardFormDialog
          mode={dialog}
          board={dialog === "settings" ? selectedBoard : null}
          mutating={mutating}
          onClose={() => setDialog(null)}
          onSave={async (input) => {
            if (dialog === "create") {
              if (await onCreateBoard(input)) setDialog(null);
            } else {
              await onUpdateBoard(input);
              setDialog(null);
            }
          }}
          onArchive={
            dialog === "settings" && selectedBoard
              ? async () => {
                  await onArchiveBoard();
                  setDialog(null);
                }
              : undefined
          }
        />
      )}
    </>
  );
}

function ViewButton({ active, onClick, label, children }: { active: boolean; onClick: () => void; label: string; children: React.ReactNode }) {
  return (
    <button type="button" className={cn("inline-flex h-7 items-center gap-1 rounded-md px-2.5 text-xs", active ? "bg-card text-foreground shadow-sm" : "text-muted-foreground")} onClick={onClick} aria-pressed={active}>
      {children}{label}
    </button>
  );
}

function BoardFormDialog({ mode, board, mutating, onClose, onSave, onArchive }: {
  mode: "create" | "settings";
  board: TaskBoard | null;
  mutating: boolean;
  onClose: () => void;
  onSave: (input: CreateBoardInput) => Promise<void>;
  onArchive?: () => Promise<void>;
}) {
  const [name, setName] = useState(board?.name ?? "");
  const [description, setDescription] = useState(board?.description ?? "");
  const [workingDir, setWorkingDir] = useState(board?.working_dir ?? "");
  const [workspace, setWorkspace] = useState<WorkspaceMode>(board?.default_workspace_mode ?? "shared");
  const [deliveryRemote, setDeliveryRemote] = useState(board?.git_delivery_remote ?? "origin");
  const [deliveryRetention, setDeliveryRetention] = useState<DeliveryRetention>(
    board?.git_delivery_retention ?? "keep"
  );
  const [deliveryAuthorName, setDeliveryAuthorName] = useState(
    board?.git_delivery_author_name ?? "Owlery Task"
  );
  const [deliveryAuthorEmail, setDeliveryAuthorEmail] = useState(
    board?.git_delivery_author_email ?? "owlery-tasks@localhost"
  );
  const [draftPr, setDraftPr] = useState(board?.git_delivery_default_draft_pr ?? true);
  const [defaultMerge, setDefaultMerge] = useState<"none" | "fast_forward_only">(
    board?.git_delivery_default_merge ?? "none"
  );
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-ink-950/35 p-0 backdrop-blur-[1px] sm:items-center sm:p-4" role="dialog" aria-modal="true" aria-label={mode === "create" ? "Create board" : "Board settings"}>
      <form
        className="w-full rounded-t-2xl border border-ink-300 bg-background p-5 shadow-[var(--elevation-overlay)] sm:max-w-lg sm:rounded-2xl"
        onSubmit={(event) => {
          event.preventDefault();
          void onSave({
            name: name.trim(),
            description: description.trim(),
            working_dir: workingDir.trim(),
            default_workspace_mode: workspace,
            git_delivery_remote: deliveryRemote.trim(),
            git_delivery_retention: deliveryRetention,
            git_delivery_author_name: deliveryAuthorName.trim(),
            git_delivery_author_email: deliveryAuthorEmail.trim(),
            git_delivery_default_draft_pr: draftPr,
            git_delivery_default_merge: defaultMerge,
          });
        }}
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="font-serif text-lg font-semibold">{mode === "create" ? "Create task board" : "Board settings"}</h2>
          <button type="button" className="rounded-md p-1 text-muted-foreground hover:bg-ink-200" onClick={onClose} aria-label="Close"><IconX size={18} /></button>
        </div>
        <div className="space-y-3">
          <Field label="Name"><input required className="task-input" value={name} onChange={(event) => setName(event.target.value)} /></Field>
          <Field label="Description"><textarea className="task-input min-h-20 resize-y py-2" value={description} onChange={(event) => setDescription(event.target.value)} /></Field>
          <Field label="Working directory"><input required className="task-input font-mono text-xs" value={workingDir} onChange={(event) => setWorkingDir(event.target.value)} placeholder="/absolute/project/path" /></Field>
          <Field label="Default workspace"><select className="task-input" value={workspace} onChange={(event) => setWorkspace(event.target.value as WorkspaceMode)}><option value="shared">Shared directory</option><option value="copy">Durable copy</option><option value="git_worktree">Git worktree</option></select></Field>
          <fieldset className="rounded-xl border border-ink-300 p-3">
            <legend className="px-1 text-xs font-semibold text-ink-700">Git delivery defaults</legend>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Remote"><input required className="task-input font-mono text-xs" value={deliveryRemote} onChange={(event) => setDeliveryRemote(event.target.value)} /></Field>
              <Field label="Retention"><select className="task-input" value={deliveryRetention} onChange={(event) => setDeliveryRetention(event.target.value as DeliveryRetention)}><option value="keep">Keep worktree and branch</option><option value="remove_worktree_keep_branch">Remove worktree, keep branch</option><option value="remove_all">Remove worktree and branch</option></select></Field>
              <Field label="Commit author"><input required className="task-input" value={deliveryAuthorName} onChange={(event) => setDeliveryAuthorName(event.target.value)} /></Field>
              <Field label="Author email"><input required type="email" className="task-input" value={deliveryAuthorEmail} onChange={(event) => setDeliveryAuthorEmail(event.target.value)} /></Field>
              <Field label="Default merge"><select className="task-input" value={defaultMerge} onChange={(event) => setDefaultMerge(event.target.value as "none" | "fast_forward_only")}><option value="none">No automatic merge</option><option value="fast_forward_only">Fast-forward only</option></select></Field>
              <label className="flex items-center gap-2 self-end pb-2 text-xs text-ink-700"><input type="checkbox" checked={draftPr} onChange={(event) => setDraftPr(event.target.checked)} /> Open pull requests as drafts</label>
            </div>
          </fieldset>
        </div>
        <div className="mt-5 flex items-center gap-2">
          {onArchive && <button type="button" className="inline-flex h-9 items-center gap-1.5 rounded-lg px-3 text-xs text-destructive hover:bg-destructive-surface" onClick={() => void onArchive()}><IconArchive size={15} /> {board?.archived ? "Unarchive" : "Archive"}</button>}
          <button type="button" className="ml-auto h-9 rounded-lg px-3 text-xs text-muted-foreground hover:bg-ink-200" onClick={onClose}>Cancel</button>
          <button type="submit" className="h-9 rounded-lg bg-primary-700 px-4 text-xs font-semibold text-white disabled:opacity-50" disabled={mutating || !name.trim() || !workingDir.trim() || !deliveryRemote.trim() || !deliveryAuthorName.trim() || !deliveryAuthorEmail.trim()}>{mode === "create" ? "Create" : "Save"}</button>
        </div>
      </form>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="block"><span className="mb-1 block text-xs font-medium text-ink-700">{label}</span>{children}</label>;
}
