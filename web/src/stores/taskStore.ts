import { create } from "zustand";

import {
  taskApi,
  TaskApiError,
  type CreateBoardInput,
  type CreateTaskInput,
  type DispatcherStatus,
  type MergeStrategy,
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

export type DeliveryActionKind =
  | "accept"
  | "commit"
  | "push"
  | "pull_request"
  | "merge"
  | "deploy_stage"
  | "deploy_switch"
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
  drain?: boolean;
  switchWhenIdle?: boolean;
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
    if (needle && !`${task.title}\n${task.body}`.toLocaleLowerCase().includes(needle)) {
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
  addComment(taskId: string, body: string): Promise<boolean>;
  addDependency(taskId: string, dependencyId: string): Promise<boolean>;
  removeDependency(taskId: string, dependencyId: string): Promise<boolean>;
  setDispatcherEnabled(boardId: string, enabled: boolean): Promise<void>;

  loadDelivery(taskId: string, runId: string): Promise<void>;
  acceptDelivery(
    taskId: string,
    runId: string,
    baseRef?: string,
    confirmations?: Record<string, boolean>
  ): Promise<boolean>;
  deliveryAction(
    taskId: string,
    runId: string,
    action: "commit" | "push" | "pull_request" | "merge" | "deploy_stage" | "deploy_switch",
    options?: DeliveryActionOptions
  ): Promise<boolean>;
  teardownDelivery(
    taskId: string,
    runId: string,
    options?: { retention?: string; confirmations?: Record<string, boolean> }
  ): Promise<boolean>;
  clearDeliveryConfirmation(): void;
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
  call: () => Promise<TaskDelivery>
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
  selectBoard: (boardId) =>
    set({
      selectedBoardId: boardId,
      selectedTaskId: null,
      tasksById: {},
      taskOrder: [],
      error: null,
    }),
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
        taskApi.listTasks(token, boardId, { include_archived: true }),
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
      const filters: TaskListFilters = { include_archived: true };
      const tasks = await taskApi.listTasks(token, boardId, filters);
      if (get().selectedBoardId === boardId) get().setTaskSnapshot(tasks);
    } catch (error) {
      set({ error: message(error) });
    } finally {
      set({ loadingTasks: false });
    }
  },
  loadTaskDetail: async (taskId) => {
    const { token } = get();
    set({ loadingDetail: true, error: null });
    try {
      const [detail, runs, events, artifacts] = await Promise.all([
        taskApi.getTask(token, taskId),
        taskApi.runs(token, taskId),
        taskApi.events(token, taskId),
        taskApi.artifacts(token, taskId),
      ]);
      if (get().selectedTaskId !== taskId) return;
      set((state) => ({
        details: { ...state.details, [taskId]: detail },
        comments: { ...state.comments, [taskId]: detail.comments ?? [] },
        runs: { ...state.runs, [taskId]: runs },
        events: { ...state.events, [taskId]: events },
        artifacts: { ...state.artifacts, [taskId]: artifacts },
        ...mergeTask(state, detail),
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
        await get().loadBoards(true);
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
        case "deploy_stage":
          return taskApi.deployStage(token, taskId, runId);
        case "deploy_switch":
          return taskApi.deploySwitch(token, taskId, runId, {
            drain: options.drain,
            switch_when_idle: options.switchWhenIdle,
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
