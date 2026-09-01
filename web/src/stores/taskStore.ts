import { create } from "zustand";

import {
  taskApi,
  TaskApiError,
  type CreateBoardInput,
  type CreateTaskInput,
  type DeliveryChain,
  type DispatcherStatus,
  type MergeStrategy,
  type ReleaseDeployment,
  type ReleaseOpResponse,
  type ReplayTimeline,
  type Task,
  type TaskArtifact,
  type TaskBoard,
  type TaskComment,
  type TaskDelivery,
  type TaskDeliveryOp,
  type TaskDetail,
  type TaskEvent,
  type TaskListFilters,
  type TaskRun,
  type TaskStatus,
  type UpdateBoardInput,
  type UpdateTaskInput,
} from "../api/tasks";
import { RELEASES_EXPANDED_KEY, readStored } from "../lib/storage";

/** History page size for the Releases panel's expanded view
 * (task-board-overhaul.md §3.2). */
const RELEASES_PAGE_SIZE = 10;

export type DeliveryActionKind =
  | "accept"
  | "commit"
  | "push"
  | "pull_request"
  | "merge"
  | "teardown";

export interface DeliveryConfirmation {
  taskId: string;
  runId: string;
  action: DeliveryActionKind;
  confirmation: string;
  verb: string;
  message: string;
}

export interface DeliveryActionOptions {
  confirmations?: Record<string, boolean>;
  mergeStrategy?: MergeStrategy;
  connectorInstallationId?: string;
  draft?: boolean;
}

export type ReleaseActionKind = "stage" | "switch" | "rollback";

export interface ReleaseConfirmation {
  boardId: string;
  action: ReleaseActionKind;
  confirmation: string;
  verb: string;
  message: string;
}

export type TaskBoardView = "kanban" | "tree";

export interface TaskFilters {
  text: string;
  assignee: string;
  priority: number | null;
  includeArchived: boolean;
  mine: boolean;
}

const EMPTY_FILTERS: TaskFilters = {
  text: "",
  assignee: "",
  priority: null,
  includeArchived: false,
  mine: false,
};

export const TASK_STATUSES: TaskStatus[] = [
  "triage",
  "todo",
  "ready",
  "running",
  "blocked",
  "done",
];

/** Dragging is merely a shortcut for an existing guarded lifecycle verb. */
export function dragOperation(
  from: TaskStatus,
  to: TaskStatus
): "triage" | "specify" | "ready" | "unblock" | null {
  if (from === to) return null;
  if ((from === "todo" || from === "ready") && to === "triage") return "triage";
  if (from === "triage" && to === "todo") return "specify";
  if (from === "todo" && to === "ready") return "ready";
  if (from === "blocked" && (to === "todo" || to === "ready")) return "unblock";
  return null;
}

export function filterTasks(
  tasks: Task[],
  filters: TaskFilters,
  activeAgentId: string | null
): Task[] {
  const needle = filters.text.trim().toLocaleLowerCase();
  return tasks.filter((task) => {
    if (!filters.includeArchived && task.archived) return false;
    if (filters.assignee && task.assignee_agent_id !== filters.assignee) return false;
    if (filters.mine && (!activeAgentId || task.assignee_agent_id !== activeAgentId)) return false;
    if (filters.priority !== null && task.priority !== filters.priority) return false;
    if (
      needle &&
      !`${task.title}\n${task.body ?? task.body_excerpt ?? ""}`
        .toLocaleLowerCase()
        .includes(needle)
    ) {
      return false;
    }
    return true;
  });
}

interface TaskState {
  token: string;
  boards: TaskBoard[];
  selectedBoardId: string | null;
  tasksById: Record<string, Task>;
  taskOrder: string[];
  selectedTaskId: string | null;
  details: Record<string, TaskDetail>;
  runs: Record<string, TaskRun[]>;
  comments: Record<string, TaskComment[]>;
  events: Record<string, TaskEvent[]>;
  artifacts: Record<string, TaskArtifact[]>;
  deliveries: Record<string, TaskDelivery>;
  deliveryOps: Record<string, TaskDeliveryOp[]>;
  deliveryConfirmation: DeliveryConfirmation | null;
  /** Keyed by run_id, mirroring `deliveries` — the supersede-chain context
   * for that run's delivery panel (task-board-overhaul.md §3.1). */
  deliveryChains: Record<string, DeliveryChain>;
  /** Keyed by run_id — the attempt-replay timeline (attempt-replay.md §3.3),
   * loaded lazily when a run's timeline panel is first expanded. */
  replays: Record<string, ReplayTimeline>;
  loadingReplay: Record<string, boolean>;
  releases: Record<string, ReleaseDeployment[]>;
  releasesTotal: Record<string, number>;
  /** The board's current live/staged rows, resolved server-side independent
   * of `releases`' page window (Snape review: deriving these from the first
   * page via `.find()` goes stale the moment either row ages off page 1). */
  releaseLive: Record<string, ReleaseDeployment | null>;
  releaseStaged: Record<string, ReleaseDeployment | null>;
  releaseRemoteTip: Record<string, string | null>;
  releaseConfirmation: ReleaseConfirmation | null;
  /** Releases panel collapse state (task-board-overhaul.md §3.2), persisted
   * via `readStored` like `integrationsExpanded`. */
  releasesExpanded: boolean;
  dispatcher: Record<string, DispatcherStatus>;
  lastEventSeq: Record<string, number>;
  filters: TaskFilters;
  view: TaskBoardView;
  loadingBoards: boolean;
  loadingTasks: boolean;
  loadingDetail: boolean;
  mutating: boolean;
  error: string | null;

  setToken(token: string): void;
  setView(view: TaskBoardView): void;
  setFilters(patch: Partial<TaskFilters>): void;
  resetFilters(): void;
  selectBoard(boardId: string | null): void;
  selectTask(taskId: string | null): void;
  setTaskSnapshot(tasks: Task[]): void;
  upsertTask(task: Task): void;
  applyTaskEvent(boardId: string, taskId: string | null, event: TaskEvent): void;
  clearError(): void;

  loadBoards(includeArchived?: boolean): Promise<void>;
  createBoard(input: CreateBoardInput): Promise<TaskBoard | null>;
  updateBoard(boardId: string, input: UpdateBoardInput): Promise<void>;
  setBoardArchived(boardId: string, archived: boolean): Promise<void>;
  loadBoard(boardId: string): Promise<void>;
  loadTasks(boardId?: string): Promise<void>;
  loadTaskDetail(taskId: string): Promise<void>;
  catchUp(boardId: string): Promise<void>;
  createTask(input: CreateTaskInput): Promise<Task | null>;
  updateTask(taskId: string, input: UpdateTaskInput): Promise<void>;
  assignTask(taskId: string, agentId: string | null): Promise<void>;
  moveTask(taskId: string, target: TaskStatus): Promise<boolean>;
  lifecycle(
    taskId: string,
    operation: "triage" | "specify" | "ready" | "block" | "unblock" | "cancel",
    body?: Record<string, unknown>
  ): Promise<boolean>;
  setTaskArchived(taskId: string, archived: boolean): Promise<void>;
  closeTask(taskId: string, summary: string): Promise<boolean>;
  addComment(taskId: string, body: string): Promise<boolean>;
  addDependency(taskId: string, dependencyId: string): Promise<boolean>;
  removeDependency(taskId: string, dependencyId: string): Promise<boolean>;
  setDispatcherEnabled(boardId: string, enabled: boolean): Promise<void>;

  loadDelivery(taskId: string, runId: string): Promise<void>;
  loadRunReplay(taskId: string, runId: string): Promise<void>;
  acceptDelivery(
    taskId: string,
    runId: string,
    baseRef?: string,
    confirmations?: Record<string, boolean>
  ): Promise<boolean>;
  deliveryAction(
    taskId: string,
    runId: string,
    action: "commit" | "push" | "pull_request" | "merge",
    options?: DeliveryActionOptions
  ): Promise<boolean>;
  teardownDelivery(
    taskId: string,
    runId: string,
    options?: { retention?: string; confirmations?: Record<string, boolean> }
  ): Promise<boolean>;
  clearDeliveryConfirmation(): void;
  loadDeliveryChain(taskId: string, runId: string): Promise<void>;
  /** Tear down every delivery this run's tip has collapsed, one at a time,
   * reusing the existing single-delivery teardown op — no new batch op
   * (task-board-overhaul.md §3.1). Stops at the first failure so a partial
   * batch surfaces the same `error` state a single failed teardown would. */
  teardownSuperseded(
    taskId: string,
    runId: string,
    options?: { retention?: string }
  ): Promise<boolean>;

  loadReleases(boardId: string): Promise<void>;
  loadMoreReleases(boardId: string): Promise<void>;
  setReleasesExpanded(expanded: boolean): void;
  stageRelease(boardId: string): Promise<boolean>;
  switchRelease(boardId: string, drain?: boolean): Promise<boolean>;
  rollbackRelease(boardId: string, confirm?: boolean): Promise<boolean>;
  clearReleaseConfirmation(): void;
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Task Board request failed";
}

function mergeTask(state: TaskState, task: Task): Partial<TaskState> {
  const current = state.tasksById[task.id];
  // REST mutations and WebSocket events race in normal operation.  A delayed
  // event must not roll an already-observed authoritative task snapshot back
  // to an older lifecycle state.
  const merged =
    current && Date.parse(current.updated_at) > Date.parse(task.updated_at)
      ? current
      : task;
  return {
    tasksById: { ...state.tasksById, [task.id]: merged },
    taskOrder: state.taskOrder.includes(merged.id)
      ? state.taskOrder
      : [...state.taskOrder, merged.id],
    details: state.details[merged.id]
      ? { ...state.details, [merged.id]: { ...state.details[merged.id], ...merged } }
      : state.details,
  };
}

function mergeDeliveryState(
  state: TaskState,
  delivery: TaskDelivery,
  ops?: TaskDeliveryOp[]
): Partial<TaskState> {
  return {
    deliveries: { ...state.deliveries, [delivery.run_id]: delivery },
    ...(ops ? { deliveryOps: { ...state.deliveryOps, [delivery.id]: ops } } : {}),
  };
}

/** Append-or-upsert a single op into an append-only log, deduped by op id. */
function upsertOp(existing: TaskDeliveryOp[] | undefined, op: TaskDeliveryOp): TaskDeliveryOp[] {
  const list = existing ?? [];
  const index = list.findIndex((item) => item.id === op.id);
  if (index === -1) return [...list, op];
  const next = [...list];
  next[index] = op;
  return next;
}

type DeliverySet = (
  partial: Partial<TaskState> | ((state: TaskState) => Partial<TaskState>)
) => void;

/** Shared reconcile for the delivery mutations: mutating/finally + confirmation decode. */
async function runDeliveryCall(
  set: DeliverySet,
  taskId: string,
  runId: string,
  action: DeliveryActionKind,
  call: () => Promise<TaskDelivery>,
): Promise<boolean> {
  set({ mutating: true, error: null });
  try {
    const delivery = await call();
    set((state) => mergeDeliveryState(state, delivery));
    return true;
  } catch (error) {
    if (
      error instanceof TaskApiError &&
      error.code === "requires_confirmation" &&
      error.confirmation
    ) {
      set({
        deliveryConfirmation: {
          taskId,
          runId,
          action,
          confirmation: error.confirmation,
          verb: error.action ?? action,
          message: error.message,
        },
      });
      return false;
    }
    set({ error: message(error) });
    return false;
  } finally {
    set({ mutating: false });
  }
}

/** REST's max page size (`le=1000` in `server/routers/task_boards.py`),
 * matching `get_tree`'s existing precedent for a single-page fetch. */
const TASK_PAGE_LIMIT = 1000;

/** Exhaustively page a board's task list (task-board-overhaul.md §3.4/§6
 * acceptance #4): a fixed single-page fetch — even at REST's max limit —
 * silently truncates the Kanban/Tree's active columns once a board crosses
 * that count (Snape review). Loops on the server's authoritative `total`
 * until every item is collected, so no board size ever drops tasks from the
 * default view; the Done column's OWN 15-card cap is a separate client-side
 * render window over this fully-loaded set, not a fetch cap. */
async function fetchAllTasks(
  token: string,
  boardId: string,
  filters: Omit<TaskListFilters, "limit" | "offset">
): Promise<Task[]> {
  const items: Task[] = [];
  let total = Infinity;
  while (items.length < total) {
    const page = await taskApi.listTasks(token, boardId, {
      ...filters,
      limit: TASK_PAGE_LIMIT,
      offset: items.length,
    });
    total = page.total;
    if (page.items.length === 0) break; // guards a stalled/inconsistent total
    items.push(...page.items);
  }
  return items;
}

type TaskGet = () => TaskState;

/** Shared reconcile for the release mutations: mutating/finally + confirmation
 * decode, mirroring `runDeliveryCall`. A release op response carries only the
 * one release row it touched, so success also reloads the board's full
 * history — the UI needs the whole list (staged/live/superseded), not a
 * single row. A busy-census refusal (e.g. "nothing running" not idle) does
 * NOT raise — the coordinator settles the op `failed` with the census in
 * `op.error` and returns 200 — so that must be surfaced explicitly here,
 * never silently swallowed by a lone `loadReleases` refresh. */
async function runReleaseCall(
  set: DeliverySet,
  get: TaskGet,
  boardId: string,
  action: ReleaseActionKind,
  call: () => Promise<ReleaseOpResponse>,
): Promise<boolean> {
  set({ mutating: true, error: null });
  try {
    const { op } = await call();
    await get().loadReleases(boardId);
    if (op.state === "failed") {
      set({ error: op.error ?? `release ${action} failed` });
      return false;
    }
    return true;
  } catch (error) {
    if (
      error instanceof TaskApiError &&
      error.code === "requires_confirmation" &&
      error.confirmation
    ) {
      set({
        releaseConfirmation: {
          boardId,
          action,
          confirmation: error.confirmation,
          verb: error.action ?? action,
          message: error.message,
        },
      });
      return false;
    }
    set({ error: message(error) });
    return false;
  } finally {
    set({ mutating: false });
  }
}

export const useTaskStore = create<TaskState>((set, get) => ({
  token: "",
  boards: [],
  selectedBoardId: null,
  tasksById: {},
  taskOrder: [],
  selectedTaskId: null,
  details: {},
  runs: {},
  comments: {},
  events: {},
  artifacts: {},
  deliveries: {},
  deliveryOps: {},
  deliveryConfirmation: null,
  deliveryChains: {},
  replays: {},
  loadingReplay: {},
  releases: {},
  releasesTotal: {},
  releaseLive: {},
  releaseStaged: {},
  releaseRemoteTip: {},
  releaseConfirmation: null,
  releasesExpanded: readStored(RELEASES_EXPANDED_KEY) === "true",
  dispatcher: {},
  lastEventSeq: {},
  filters: EMPTY_FILTERS,
  view: "kanban",
  loadingBoards: false,
  loadingTasks: false,
  loadingDetail: false,
  mutating: false,
  error: null,

  setToken: (token) => set({ token }),
  setView: (view) => set({ view }),
  setFilters: (patch) => set((state) => ({ filters: { ...state.filters, ...patch } })),
  resetFilters: () => set({ filters: EMPTY_FILTERS }),
  selectBoard: (boardId) => {
    // Re-selecting the board that's already active is a no-op: the caller
    // (a real re-pick, or a redundant re-dispatch of the same `<select>`
    // value — e.g. Playwright's selectOption() always fires a change event
    // even when the value doesn't change) must not blow away tasks already
    // loaded for it. Clearing unconditionally here previously left the
    // board stuck empty forever, since the board-load effect keys off the
    // boardId *value* and never re-fires for an unchanged id.
    if (boardId === get().selectedBoardId) return;
    set({
      selectedBoardId: boardId,
      selectedTaskId: null,
      tasksById: {},
      taskOrder: [],
      error: null,
    });
  },
  selectTask: (taskId) => set({ selectedTaskId: taskId }),
  setTaskSnapshot: (tasks) =>
    set({
      tasksById: Object.fromEntries(tasks.map((task) => [task.id, task])),
      taskOrder: tasks.map((task) => task.id),
    }),
  upsertTask: (task) => set((state) => mergeTask(state, task)),
  applyTaskEvent: (boardId, taskId, event) => {
    if (event.seq <= (get().lastEventSeq[boardId] ?? 0)) return;
    const payloadTask = event.payload.task;
    const payloadDelivery = event.payload.delivery;
    const payloadOp = event.payload.op;
    set((state) => {
      const patch: Partial<TaskState> = {
        lastEventSeq: { ...state.lastEventSeq, [boardId]: event.seq },
        events: {
          ...state.events,
          ...(taskId
            ? { [taskId]: [...(state.events[taskId] ?? []), event] }
            : {}),
        },
        ...(payloadTask && typeof payloadTask === "object"
          ? mergeTask(state, payloadTask as Task)
          : {}),
      };
      if (payloadDelivery && typeof payloadDelivery === "object") {
        const delivery = payloadDelivery as TaskDelivery;
        patch.deliveries = { ...state.deliveries, [delivery.run_id]: delivery };
      }
      if (payloadOp && typeof payloadOp === "object") {
        const op = payloadOp as TaskDeliveryOp;
        patch.deliveryOps = {
          ...state.deliveryOps,
          [op.delivery_id]: upsertOp(state.deliveryOps[op.delivery_id], op),
        };
      }
      return patch;
    });
  },
  clearError: () => set({ error: null }),

  loadBoards: async (includeArchived = false) => {
    const { token } = get();
    if (!token) return;
    set({ loadingBoards: true, error: null });
    try {
      const boards = await taskApi.listBoards(token, includeArchived);
      const selected = get().selectedBoardId;
      set({
        boards,
        selectedBoardId:
          selected && boards.some((board) => board.id === selected)
            ? selected
            : boards.find((board) => !board.archived)?.id ?? boards[0]?.id ?? null,
      });
    } catch (error) {
      set({ error: message(error) });
    } finally {
      set({ loadingBoards: false });
    }
  },
  createBoard: async (input) => {
    const { token } = get();
    set({ mutating: true, error: null });
    try {
      const board = await taskApi.createBoard(token, input);
      set((state) => ({ boards: [...state.boards, board], selectedBoardId: board.id }));
      return board;
    } catch (error) {
      set({ error: message(error) });
      return null;
    } finally {
      set({ mutating: false });
    }
  },
  updateBoard: async (boardId, input) => {
    const { token } = get();
    set({ mutating: true, error: null });
    try {
      const current = get().boards.find((board) => board.id === boardId);
      const board = await taskApi.updateBoard(token, boardId, {
        ...input,
        updated_at: current?.updated_at,
      });
      set((state) => ({ boards: state.boards.map((item) => (item.id === board.id ? board : item)) }));
    } catch (error) {
      set({ error: message(error) });
    } finally {
      set({ mutating: false });
    }
  },
  setBoardArchived: async (boardId, archived) => {
    const { token } = get();
    set({ mutating: true, error: null });
    try {
      const board = await taskApi.archiveBoard(token, boardId, archived);
      set((state) => ({ boards: state.boards.map((item) => (item.id === board.id ? board : item)) }));
    } catch (error) {
      set({ error: message(error) });
    } finally {
      set({ mutating: false });
    }
  },
  loadBoard: async (boardId) => {
    const { token } = get();
    set({ loadingTasks: true, error: null });
    try {
      const [tasks, dispatcher] = await Promise.all([
        fetchAllTasks(token, boardId, { include_archived: true }),
        taskApi.dispatcher(token, boardId),
      ]);
      if (get().selectedBoardId !== boardId) return;
      set({
        tasksById: Object.fromEntries(tasks.map((task) => [task.id, task])),
        taskOrder: tasks.map((task) => task.id),
        dispatcher: { ...get().dispatcher, [boardId]: dispatcher },
      });
    } catch (error) {
      set({ error: message(error) });
    } finally {
      set({ loadingTasks: false });
    }
  },
  loadTasks: async (boardId = get().selectedBoardId ?? "") => {
    if (!boardId) return;
    const { token } = get();
    set({ loadingTasks: true, error: null });
    try {
      const tasks = await fetchAllTasks(token, boardId, { include_archived: true });
      if (get().selectedBoardId === boardId) get().setTaskSnapshot(tasks);
    } catch (error) {
      set({ error: message(error) });
    } finally {
      set({ loadingTasks: false });
    }
  },
  loadTaskDetail: async (taskId) => {
    const { token } = get();
    // Drop this task's cached `details`/`runs` before the fetch starts, not
    // just overwrite them once it resolves. Reopening a task whose run
    // history changed since it was last cached would otherwise render with
    // stale data for the whole fetch window — e.g. TaskDrawer's Close button
    // treats `detail !== undefined` as "runs are current" (taskStore-derived
    // race-free signal), which stale cache defeats: a task cached earlier
    // with no runs, then dispatched a run, then reopened, would show Close
    // as clickable until the fresh response lands (Snape review).
    set((state) => {
      const details = { ...state.details };
      delete details[taskId];
      const runs = { ...state.runs };
      delete runs[taskId];
      return { loadingDetail: true, error: null, details, runs };
    });
    try {
      const [detail, runs, events, artifacts] = await Promise.all([
        taskApi.getTask(token, taskId),
        taskApi.runs(token, taskId),
        taskApi.events(token, taskId),
        taskApi.artifacts(token, taskId),
      ]);
      if (get().selectedTaskId !== taskId) return;
      set((state) => ({
        // `mergeTask` spreads first: its own `details` field only exists to
        // keep an *already-cached* detail's base Task fields in sync with a
        // racing WS event, and must not be allowed to win a key collision
        // against the detail this call just authoritatively fetched — object
        // spread order previously put it last, so on a task's first-ever
        // load (nothing cached yet) `mergeTask` returned `details:
        // state.details` unchanged, silently discarding the fetch result and
        // leaving `details[taskId]` undefined forever (caught by the
        // loadTaskDetail store test below, not by any prior test).
        ...mergeTask(state, detail),
        details: { ...state.details, [taskId]: detail },
        comments: { ...state.comments, [taskId]: detail.comments ?? [] },
        runs: { ...state.runs, [taskId]: runs },
        events: { ...state.events, [taskId]: events },
        artifacts: { ...state.artifacts, [taskId]: artifacts },
      }));
    } catch (error) {
      set({ error: message(error) });
    } finally {
      set({ loadingDetail: false });
    }
  },
  catchUp: async (boardId) => {
    const { token, lastEventSeq } = get();
    try {
      const events = await taskApi.boardEvents(token, boardId, lastEventSeq[boardId] ?? 0);
      for (const event of events) get().applyTaskEvent(boardId, event.task_id, event);
      if (events.some((event) => event.task_id === null)) {
        // A board-level event: board settings, dispatcher, AND release-line
        // deploy ops all publish through here (publish_board_update), so the
        // Releases panel must catch up the same way boards/tasks do — never
        // stall on a stale history after another client's stage/switch or a
        // WS-reconnect replay.
        await Promise.all([get().loadBoards(true), get().loadReleases(boardId)]);
      }
      if (events.some((event) => !(event.payload.task && typeof event.payload.task === "object"))) {
        await get().loadTasks(boardId);
      }
    } catch (error) {
      set({ error: message(error) });
      await get().loadTasks(boardId);
    }
  },
  createTask: async (input) => {
    const { token, selectedBoardId } = get();
    if (!selectedBoardId) return null;
    set({ mutating: true, error: null });
    try {
      const task = await taskApi.createTask(token, selectedBoardId, input);
      get().upsertTask(task);
      return task;
    } catch (error) {
      set({ error: message(error) });
      return null;
    } finally {
      set({ mutating: false });
    }
  },
  updateTask: async (taskId, input) => {
    const { token } = get();
    set({ mutating: true, error: null });
    try {
      const task = await taskApi.updateTask(token, taskId, input);
      get().upsertTask(task);
    } catch (error) {
      const current = error instanceof TaskApiError ? error.currentTask : null;
      if (current) get().upsertTask(current);
      set({ error: message(error) });
    } finally {
      set({ mutating: false });
    }
  },
  assignTask: async (taskId, agentId) => {
    const { token } = get();
    set({ mutating: true, error: null });
    try {
      get().upsertTask(await taskApi.assign(token, taskId, agentId));
    } catch (error) {
      set({ error: message(error) });
    } finally {
      set({ mutating: false });
    }
  },
  moveTask: async (taskId, target) => {
    const task = get().tasksById[taskId];
    if (!task) return false;
    const operation = dragOperation(task.status, target);
    if (!operation) return false;
    return get().lifecycle(taskId, operation);
  },
  lifecycle: async (taskId, operation, body = {}) => {
    const { token } = get();
    set({ mutating: true, error: null });
    try {
      get().upsertTask(await taskApi.lifecycle(token, taskId, operation, body));
      return true;
    } catch (error) {
      const current = error instanceof TaskApiError ? error.currentTask : null;
      if (current) get().upsertTask(current);
      set({ error: message(error) });
      return false;
    } finally {
      set({ mutating: false });
    }
  },
  setTaskArchived: async (taskId, archived) => {
    const { token } = get();
    set({ mutating: true, error: null });
    try {
      get().upsertTask(await taskApi.archiveTask(token, taskId, archived));
    } catch (error) {
      set({ error: message(error) });
    } finally {
      set({ mutating: false });
    }
  },
  closeTask: async (taskId, summary) => {
    const { token } = get();
    set({ mutating: true, error: null });
    try {
      get().upsertTask(await taskApi.close(token, taskId, summary));
      return true;
    } catch (error) {
      const current = error instanceof TaskApiError ? error.currentTask : null;
      if (current) get().upsertTask(current);
      set({ error: message(error) });
      return false;
    } finally {
      set({ mutating: false });
    }
  },
  addComment: async (taskId, body) => {
    const trimmed = body.trim();
    if (!trimmed) return false;
    const { token } = get();
    set({ mutating: true, error: null });
    try {
      const comment = await taskApi.comment(token, taskId, trimmed);
      set((state) => ({
        comments: { ...state.comments, [taskId]: [...(state.comments[taskId] ?? []), comment] },
      }));
      return true;
    } catch (error) {
      set({ error: message(error) });
      return false;
    } finally {
      set({ mutating: false });
    }
  },
  addDependency: async (taskId, dependencyId) => {
    const { token } = get();
    set({ mutating: true, error: null });
    try {
      const task = await taskApi.addDependency(token, taskId, dependencyId);
      get().upsertTask(task);
      await get().loadTaskDetail(taskId);
      return true;
    } catch (error) {
      set({ error: message(error) });
      return false;
    } finally {
      set({ mutating: false });
    }
  },
  removeDependency: async (taskId, dependencyId) => {
    const { token } = get();
    set({ mutating: true, error: null });
    try {
      await taskApi.removeDependency(token, taskId, dependencyId);
      await get().loadTaskDetail(taskId);
      return true;
    } catch (error) {
      set({ error: message(error) });
      return false;
    } finally {
      set({ mutating: false });
    }
  },
  setDispatcherEnabled: async (boardId, enabled) => {
    const { token } = get();
    set({ mutating: true, error: null });
    try {
      const status = await taskApi.setDispatcher(token, boardId, enabled);
      set((state) => ({ dispatcher: { ...state.dispatcher, [boardId]: status } }));
    } catch (error) {
      set({ error: message(error) });
    } finally {
      set({ mutating: false });
    }
  },

  loadDelivery: async (taskId, runId) => {
    const { token } = get();
    if (!token) return;
    try {
      const { delivery, ops } = await taskApi.getDelivery(token, taskId, runId);
      set((state) => mergeDeliveryState(state, delivery, ops));
    } catch (error) {
      if (error instanceof TaskApiError && error.status === 404) return;
      set({ error: message(error) });
    }
  },

  loadRunReplay: async (taskId, runId) => {
    const { token } = get();
    if (!token) return;
    set((state) => ({ loadingReplay: { ...state.loadingReplay, [runId]: true } }));
    try {
      const replay = await taskApi.runReplay(token, taskId, runId);
      set((state) => ({ replays: { ...state.replays, [runId]: replay } }));
    } catch (error) {
      // A run with no worker session yet (setup failed before spawn) 404s —
      // there is nothing to replay, not an error worth surfacing globally.
      if (error instanceof TaskApiError && error.status === 404) return;
      set({ error: message(error) });
    } finally {
      set((state) => ({ loadingReplay: { ...state.loadingReplay, [runId]: false } }));
    }
  },
  acceptDelivery: (taskId, runId, baseRef, confirmations) =>
    runDeliveryCall(set, taskId, runId, "accept", () =>
      taskApi.acceptDelivery(get().token, taskId, runId, { base_ref: baseRef, confirmations })
    ),
  deliveryAction: (taskId, runId, action, options = {}) => {
    const { token } = get();
    return runDeliveryCall(set, taskId, runId, action, () => {
      switch (action) {
        case "commit":
          return taskApi.commitDelivery(token, taskId, runId, {
            confirmations: options.confirmations,
          });
        case "push":
          return taskApi.pushDelivery(token, taskId, runId, {
            confirmations: options.confirmations,
          });
        case "pull_request":
          return taskApi.pullRequestDelivery(token, taskId, runId, {
            connector_installation_id: options.connectorInstallationId,
            draft: options.draft,
            confirmations: options.confirmations,
          });
        case "merge":
          return taskApi.mergeDelivery(token, taskId, runId, {
            merge_strategy: options.mergeStrategy,
            confirmations: options.confirmations,
          });
      }
      throw new Error(`Unknown delivery action: ${action}`);
    });
  },
  teardownDelivery: (taskId, runId, options = {}) =>
    runDeliveryCall(set, taskId, runId, "teardown", () =>
      taskApi.teardownDelivery(get().token, taskId, runId, {
        retention: options.retention,
        confirmations: options.confirmations,
      })
    ),
  clearDeliveryConfirmation: () => set({ deliveryConfirmation: null }),
  loadDeliveryChain: async (taskId, runId) => {
    const { token } = get();
    if (!token) return;
    try {
      const chain = await taskApi.deliveryChain(token, taskId, runId);
      set((state) => ({ deliveryChains: { ...state.deliveryChains, [runId]: chain } }));
    } catch (error) {
      if (error instanceof TaskApiError && error.status === 404) return;
      set({ error: message(error) });
    }
  },
  teardownSuperseded: async (taskId, runId, options = {}) => {
    const chain = get().deliveryChains[runId];
    if (!chain || chain.superseded.length === 0) return true;
    let allOk = true;
    try {
      for (const entry of chain.superseded) {
        const ok = await get().teardownDelivery(entry.task_id, entry.run_id, options);
        if (!ok) {
          // A single entry needing a typed confirmation (e.g. deleting an
          // unmerged branch) stops the walk there rather than skipping it —
          // the pending confirmation is the same dialog a solo teardown
          // would show; re-running the batch after resolving it picks up
          // where this left off.
          allOk = false;
          break;
        }
      }
    } finally {
      // `superseded_by_delivery_id` is a permanent git fact — a successful
      // teardown does NOT remove an entry from `chain.superseded` (only a
      // history rewrite that breaks the ancestry would). Refresh anyway so
      // any OTHER change the teardown made (op ledger, retention) is current
      // the next time this panel reads the chain.
      await get().loadDeliveryChain(taskId, runId);
    }
    return allOk;
  },

  loadReleases: async (boardId) => {
    const { token } = get();
    if (!token) return;
    try {
      const result = await taskApi.releases(token, boardId, { limit: RELEASES_PAGE_SIZE, offset: 0 });
      set((state) => ({
        releases: { ...state.releases, [boardId]: result.releases },
        releasesTotal: { ...state.releasesTotal, [boardId]: result.total },
        releaseLive: { ...state.releaseLive, [boardId]: result.live },
        releaseStaged: { ...state.releaseStaged, [boardId]: result.staged },
        releaseRemoteTip: { ...state.releaseRemoteTip, [boardId]: result.remote_tip },
      }));
    } catch (error) {
      set({ error: message(error) });
    }
  },
  loadMoreReleases: async (boardId) => {
    const { token } = get();
    if (!token) return;
    const current = get().releases[boardId] ?? [];
    try {
      const result = await taskApi.releases(token, boardId, {
        limit: RELEASES_PAGE_SIZE,
        offset: current.length,
      });
      if (get().token !== token) return;
      set((state) => ({
        releases: { ...state.releases, [boardId]: [...current, ...result.releases] },
        releasesTotal: { ...state.releasesTotal, [boardId]: result.total },
        releaseLive: { ...state.releaseLive, [boardId]: result.live },
        releaseStaged: { ...state.releaseStaged, [boardId]: result.staged },
        releaseRemoteTip: { ...state.releaseRemoteTip, [boardId]: result.remote_tip },
      }));
    } catch (error) {
      set({ error: message(error) });
    }
  },
  setReleasesExpanded: (expanded) => {
    if (expanded) localStorage.setItem(RELEASES_EXPANDED_KEY, "true");
    else localStorage.removeItem(RELEASES_EXPANDED_KEY);
    set({ releasesExpanded: expanded });
  },
  stageRelease: (boardId) =>
    runReleaseCall(set, get, boardId, "stage", () => taskApi.releaseStage(get().token, boardId)),
  switchRelease: (boardId, drain = false) =>
    runReleaseCall(set, get, boardId, "switch", () =>
      taskApi.releaseSwitch(get().token, boardId, { drain })
    ),
  rollbackRelease: (boardId, confirm = false) =>
    runReleaseCall(set, get, boardId, "rollback", () =>
      taskApi.releaseRollback(get().token, boardId, confirm)
    ),
  clearReleaseConfirmation: () => set({ releaseConfirmation: null }),
}));

export function resetTaskStore(): void {
  useTaskStore.setState({
    token: "",
    boards: [],
    selectedBoardId: null,
    tasksById: {},
    taskOrder: [],
    selectedTaskId: null,
    details: {},
    runs: {},
    comments: {},
    events: {},
    artifacts: {},
    deliveries: {},
    deliveryOps: {},
    deliveryConfirmation: null,
    deliveryChains: {},
    replays: {},
    loadingReplay: {},
    releases: {},
    releasesTotal: {},
    releaseLive: {},
    releaseStaged: {},
    releaseRemoteTip: {},
    releaseConfirmation: null,
    dispatcher: {},
    lastEventSeq: {},
    filters: EMPTY_FILTERS,
    view: "kanban",
    loadingBoards: false,
    loadingTasks: false,
    loadingDetail: false,
    mutating: false,
    error: null,
  });
}
