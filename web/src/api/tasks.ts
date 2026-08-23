/** Task Board HTTP client.
 *
 * These types intentionally live beside the feature instead of importing the
 * generated OpenAPI contract: the Task Board can be developed and tested
 * before the backend schema generation step, while preserving one typed
 * boundary for the eventual generated aliases.
 */

export type TaskStatus =
  | "triage"
  | "todo"
  | "ready"
  | "running"
  | "blocked"
  | "done";

export type TaskRunState =
  | "running"
  | "completed"
  | "blocked"
  | "failed"
  | "cancelled"
  | "interrupted";

export type WorkspaceMode = "shared" | "copy" | "git_worktree";
export type DeliveryRetention =
  | "keep"
  | "remove_worktree_keep_branch"
  | "remove_all";
export type BlockedKind =
  | "input"
  | "capability"
  | "failure"
  | "protocol"
  | "cancelled"
  | "interrupted";

export interface TaskBoard {
  id: string;
  name: string;
  description: string;
  working_dir: string;
  default_workspace_mode: WorkspaceMode;
  max_running: number | null;
  max_running_per_agent: number | null;
  max_tree_depth: number;
  max_children_per_run: number;
  max_open_tasks: number;
  dispatch_enabled: boolean;
  git_delivery_remote: string;
  git_delivery_retention: DeliveryRetention;
  git_delivery_author_name: string;
  git_delivery_author_email: string;
  git_delivery_default_draft_pr: boolean;
  git_delivery_default_merge: "none" | "fast_forward_only";
  allow_local_deploy: boolean;
  deploy_release_ref: string;
  archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface TaskDependency {
  id: string;
  title: string;
  status: TaskStatus;
}

export interface Task {
  id: string;
  board_id: string;
  parent_task_id: string | null;
  title: string;
  // A list-page item (`taskApi.listTasks`) never carries the full body — only
  // `body_excerpt` (first ~200 chars). `TaskDetail` (single-task fetch) sets
  // `body` to the full text and leaves `body_excerpt` unset.
  body?: string;
  body_excerpt?: string;
  status: TaskStatus;
  assignee_agent_id: string | null;
  priority: number;
  origin_session_id: string | null;
  scheduled_at: string | null;
  workspace_mode: WorkspaceMode | null;
  working_dir_override: string | null;
  current_run_id: string | null;
  blocked_kind: BlockedKind | null;
  blocked_reason: string | null;
  result_summary: string | null;
  archived: boolean;
  created_by_kind: "user" | "agent" | "schedule" | "api";
  created_by_agent_id: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  archived_at: string | null;
  child_count?: number;
  dependency_count?: number;
  dependent_count?: number;
  latest_run_state?: TaskRunState | null;
  latest_heartbeat_at?: string | null;
  latest_run_workspace_mode?: WorkspaceMode | null;
  delivery?: TaskDeliverySummary | null;
  dependencies?: TaskDependency[];
  dependents?: TaskDependency[];
  children?: Task[];
}

/** The card-facing subset of a run's git delivery, aggregated server-side.
 * Present only for git_worktree runs that have been accepted; `null` otherwise.
 * Raw facts only — the display label/tone is derived in `taskPresentation`. */
export interface TaskDeliverySummary {
  status: DeliveryStatus;
  dirty: boolean;
  commits_ahead: number | null;
  pushed_ref: string | null;
  pr_number: number | null;
  pr_state: string | null;
  merge_strategy: string | null;
  reason_kind: string | null;
}

export interface TaskRun {
  id: string;
  task_id: string;
  attempt_no: number;
  agent_id: string | null;
  session_id: string | null;
  state: TaskRunState;
  summary: string | null;
  metadata: Record<string, unknown> | null;
  error: string | null;
  workspace_mode: WorkspaceMode;
  workspace_path: string;
  claimed_at: string;
  started_at: string | null;
  last_heartbeat_at: string | null;
  lease_expires_at: string | null;
  finished_at: string | null;
  cost?: number | null;
}

export interface TaskComment {
  id: string;
  task_id: string;
  run_id: string | null;
  author_kind: "user" | "agent" | "system";
  author_agent_id: string | null;
  body: string;
  created_at: string;
}

export interface TaskEvent {
  seq: number;
  board_id: string;
  task_id: string | null;
  run_id: string | null;
  kind: string;
  actor_kind: string;
  actor_agent_id: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface TaskArtifact {
  id: string;
  task_id: string;
  run_id: string;
  name: string;
  source_path: string;
  mime_type: string | null;
  size: number;
  sha256: string;
  created_at: string;
  deleted_at: string | null;
  download_url?: string | null;
}

export type DeliveryStatus =
  | "pending"
  | "preparing"
  | "ready"
  | "delivering"
  | "delivered"
  | "conflicted"
  | "blocked"
  | "failed";

export type DeliveryOpKind =
  | "commit"
  | "push"
  | "pull_request"
  | "merge"
  | "deploy_stage"
  | "deploy_switch"
  | "branch_delete"
  | "worktree_remove";

export type DeliveryOpState =
  | "planned"
  | "running"
  | "succeeded"
  | "failed"
  | "interrupted";

export type MergeStrategy = "fast_forward_only" | "no_conflict_merge";

export interface TaskDelivery {
  id: string;
  task_id: string;
  run_id: string;
  status: DeliveryStatus;
  repository: string;
  base_ref: string | null;
  base_head: string | null;
  attempt_branch: string;
  attempt_head: string | null;
  dirty: boolean;
  commits_ahead: number | null;
  diffstat: { files: number; insertions: number; deletions: number } | null;
  remote_name: string;
  remote_url: string;
  pushed_ref: string;
  pr_number: number | null;
  pr_url: string;
  pr_state: string;
  merge_strategy: string;
  retention: DeliveryRetention;
  reason_kind: string | null;
  reason_detail: string | null;
  deployed_sha: string | null;
  deployed_slot: string | null;
  created_at: string;
  updated_at: string;
}

export type ReleaseDeploymentState =
  | "planned"
  | "staging"
  | "staged"
  | "switching"
  | "live"
  | "rolled_back"
  | "superseded"
  | "failed";

export interface ReleaseDeployment {
  id: string;
  board_id: string;
  version: string;
  source_ref: string;
  sha: string;
  source_repo: string;
  deployment_id: string | null;
  state: ReleaseDeploymentState;
  actor_kind: string;
  actor_agent_id: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export type ReleaseOpKind = "stage" | "switch" | "rollback";

export interface ReleaseDeploymentOp {
  id: string;
  release_id: string;
  kind: ReleaseOpKind;
  state: DeliveryOpState;
  request: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error: string | null;
  journal_ref: string | null;
  actor_kind: string;
  actor_agent_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface ReleaseOpResponse {
  release: ReleaseDeployment | null;
  op: ReleaseDeploymentOp;
}

export interface TaskDeliveryOp {
  id: string;
  delivery_id: string;
  kind: DeliveryOpKind;
  source_key: string;
  external: boolean;
  state: DeliveryOpState;
  request: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error: string | null;
  actor_kind: string;
  actor_agent_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface DispatcherStatus {
  board_id: string;
  enabled: boolean;
  running: number;
  running_by_agent: Record<string, number>;
  last_tick_at: string | null;
  last_error: string | null;
}

export interface TaskDetail extends Task {
  dependencies: TaskDependency[];
  dependents: TaskDependency[];
  children: Task[];
  comments?: TaskComment[];
  runs?: TaskRun[];
  artifacts?: TaskArtifact[];
}

export interface TaskListFilters {
  status?: TaskStatus;
  assignee?: string;
  parent_id?: string;
  include_archived?: boolean;
  search?: string;
  priority?: number;
}

/** The paginated envelope `GET /api/task-boards/{id}/tasks` returns
 * (task-board-overhaul.md §3.5): items are summaries (`body_excerpt`, not
 * `body`); `total` is the full matching count regardless of `limit`. */
export interface TaskListPage {
  items: Task[];
  total: number;
  limit: number;
  offset: number;
}

export interface CreateBoardInput {
  name: string;
  description?: string;
  working_dir: string;
  default_workspace_mode?: WorkspaceMode;
  max_running?: number | null;
  max_running_per_agent?: number | null;
  git_delivery_remote?: string;
  git_delivery_retention?: DeliveryRetention;
  git_delivery_author_name?: string;
  git_delivery_author_email?: string;
  git_delivery_default_draft_pr?: boolean;
  git_delivery_default_merge?: "none" | "fast_forward_only";
  allow_local_deploy?: boolean;
  deploy_release_ref?: string;
}

export type UpdateBoardInput = Partial<CreateBoardInput> & {
  updated_at?: string;
};

export interface CreateTaskInput {
  title: string;
  body?: string;
  parent_task_id?: string | null;
  assignee_agent_id?: string | null;
  priority?: number;
  scheduled_at?: string | null;
  workspace_mode?: WorkspaceMode | null;
  dependencies?: string[];
  idempotency_key?: string;
}

export interface UpdateTaskInput {
  title?: string;
  body?: string;
  priority?: number;
  scheduled_at?: string | null;
  workspace_mode?: WorkspaceMode | null;
  working_dir_override?: string | null;
  parent_task_id?: string | null;
  updated_at?: string;
}

export interface TaskApiErrorInfo {
  code?: string;
  confirmation?: string;
  action?: string;
}

export class TaskApiError extends Error {
  status: number;
  currentTask: Task | null;
  code: string | null;
  confirmation: string | null;
  action: string | null;

  constructor(
    message: string,
    status: number,
    currentTask: Task | null = null,
    info: TaskApiErrorInfo = {}
  ) {
    super(message);
    this.name = "TaskApiError";
    this.status = status;
    this.currentTask = currentTask;
    this.code = info.code ?? null;
    this.confirmation = info.confirmation ?? null;
    this.action = info.action ?? null;
  }
}

const API = window.location.origin;

function headers(token: string, jsonBody = false): HeadersInit {
  return {
    Authorization: `Bearer ${token}`,
    ...(jsonBody ? { "Content-Type": "application/json" } : {}),
  };
}

async function decode<T>(response: Response): Promise<T> {
  if (response.ok) {
    if (response.status === 204) return undefined as T;
    return response.json() as Promise<T>;
  }
  const payload = (await response.json().catch(() => null)) as {
    detail?:
      | string
      | {
          message?: string;
          current_task?: Task;
          code?: string;
          confirmation?: string;
          action?: string;
        };
    current_task?: Task;
  } | null;
  const detail = payload?.detail;
  const asObject = typeof detail === "object" && detail !== null ? detail : null;
  const message =
    typeof detail === "string"
      ? detail
      : asObject?.message ?? `Task Board request failed (${response.status})`;
  throw new TaskApiError(
    message,
    response.status,
    payload?.current_task ?? asObject?.current_task ?? null,
    {
      code: asObject?.code,
      confirmation: asObject?.confirmation,
      action: asObject?.action,
    }
  );
}

type QueryValue = string | number | boolean | undefined;

// Serialize a params object into a query string. Constrained on the object's
// own value types (all must be QueryValue) rather than requiring a string
// index signature, so domain interfaces like TaskListFilters can be passed
// directly without a cast while still rejecting non-serializable values.
function query<T extends Partial<Record<keyof T, QueryValue>>>(values: T): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== "") params.set(key, String(value));
  }
  const encoded = params.toString();
  return encoded ? `?${encoded}` : "";
}

async function get<T>(token: string, path: string): Promise<T> {
  return decode(await fetch(`${API}${path}`, { headers: headers(token) }));
}

async function mutate<T>(
  token: string,
  path: string,
  method: "POST" | "PATCH" | "DELETE",
  body?: unknown
): Promise<T> {
  return decode(
    await fetch(`${API}${path}`, {
      method,
      headers: headers(token, body !== undefined),
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    })
  );
}

export const taskApi = {
  listBoards: (token: string, includeArchived = false) =>
    get<TaskBoard[]>(token, `/api/task-boards${query({ include_archived: includeArchived })}`),
  getBoard: (token: string, boardId: string) =>
    get<TaskBoard>(token, `/api/task-boards/${boardId}`),
  createBoard: (token: string, input: CreateBoardInput) =>
    mutate<TaskBoard>(token, "/api/task-boards", "POST", input),
  updateBoard: (token: string, boardId: string, input: UpdateBoardInput) =>
    mutate<TaskBoard>(token, `/api/task-boards/${boardId}`, "PATCH", input),
  archiveBoard: (token: string, boardId: string, archived: boolean) =>
    mutate<TaskBoard>(
      token,
      `/api/task-boards/${boardId}/${archived ? "archive" : "unarchive"}`,
      "POST"
    ),
  dispatcher: (token: string, boardId: string) =>
    get<DispatcherStatus>(token, `/api/task-boards/${boardId}/dispatcher`),
  setDispatcher: (token: string, boardId: string, enabled: boolean) =>
    mutate<DispatcherStatus>(
      token,
      `/api/task-boards/${boardId}/dispatcher/${enabled ? "resume" : "pause"}`,
      "POST"
    ),
  boardEvents: (token: string, boardId: string, afterSeq = 0) =>
    get<TaskEvent[]>(token, `/api/task-boards/${boardId}/events${query({ after_seq: afterSeq })}`),
  listTasks: (token: string, boardId: string, filters: TaskListFilters = {}) =>
    get<TaskListPage>(token, `/api/task-boards/${boardId}/tasks${query(filters)}`),
  createTask: (token: string, boardId: string, input: CreateTaskInput) =>
    mutate<Task>(token, `/api/task-boards/${boardId}/tasks`, "POST", input),
  tree: (token: string, boardId: string) =>
    get<Task[]>(token, `/api/task-boards/${boardId}/tree`),
  getTask: (token: string, taskId: string) =>
    get<TaskDetail>(token, `/api/tasks/${taskId}`),
  updateTask: (token: string, taskId: string, input: UpdateTaskInput) =>
    mutate<Task>(token, `/api/tasks/${taskId}`, "PATCH", input),
  assign: (token: string, taskId: string, agentId: string | null) =>
    mutate<Task>(token, `/api/tasks/${taskId}/assign`, "POST", { agent_id: agentId }),
  addDependency: (token: string, taskId: string, dependencyId: string) =>
    mutate<Task>(token, `/api/tasks/${taskId}/dependencies`, "POST", {
      depends_on_task_id: dependencyId,
    }),
  removeDependency: (token: string, taskId: string, dependencyId: string) =>
    mutate<void>(token, `/api/tasks/${taskId}/dependencies/${dependencyId}`, "DELETE"),
  comment: (token: string, taskId: string, body: string) =>
    mutate<TaskComment>(token, `/api/tasks/${taskId}/comments`, "POST", { body }),
  lifecycle: (
    token: string,
    taskId: string,
    operation: "triage" | "specify" | "ready" | "block" | "unblock" | "cancel",
    body: Record<string, unknown> = {}
  ) => mutate<Task>(token, `/api/tasks/${taskId}/${operation}`, "POST", body),
  archiveTask: (token: string, taskId: string, archived: boolean) =>
    mutate<Task>(token, `/api/tasks/${taskId}/${archived ? "archive" : "unarchive"}`, "POST"),
  runs: (token: string, taskId: string) =>
    get<TaskRun[]>(token, `/api/tasks/${taskId}/runs`),
  events: (token: string, taskId: string) =>
    get<TaskEvent[]>(token, `/api/tasks/${taskId}/events`),
  artifacts: (token: string, taskId: string) =>
    get<TaskArtifact[]>(token, `/api/tasks/${taskId}/artifacts`),
  deleteArtifact: (token: string, taskId: string, artifactId: string) =>
    mutate<void>(token, `/api/tasks/${taskId}/artifacts/${artifactId}`, "DELETE"),
  getDelivery: (token: string, taskId: string, runId: string) =>
    get<{ delivery: TaskDelivery; ops: TaskDeliveryOp[] }>(
      token,
      `/api/tasks/${taskId}/runs/${runId}/delivery`
    ),
  deliveryOps: (token: string, taskId: string, runId: string) =>
    get<TaskDeliveryOp[]>(token, `/api/tasks/${taskId}/runs/${runId}/delivery/ops`),
  acceptDelivery: (
    token: string,
    taskId: string,
    runId: string,
    body: { base_ref?: string; confirmations?: Record<string, boolean> } = {}
  ) => mutate<TaskDelivery>(token, `/api/tasks/${taskId}/runs/${runId}/delivery/accept`, "POST", body),
  commitDelivery: (
    token: string,
    taskId: string,
    runId: string,
    body: { confirmations?: Record<string, boolean> } = {}
  ) => mutate<TaskDelivery>(token, `/api/tasks/${taskId}/runs/${runId}/delivery/commit`, "POST", body),
  pushDelivery: (
    token: string,
    taskId: string,
    runId: string,
    body: { confirmations?: Record<string, boolean> } = {}
  ) =>
    mutate<TaskDelivery>(token, `/api/tasks/${taskId}/runs/${runId}/delivery/push`, "POST", {
      confirmations: body.confirmations ?? {},
    }),
  pullRequestDelivery: (
    token: string,
    taskId: string,
    runId: string,
    body: {
      connector_installation_id?: string;
      draft?: boolean;
      confirmations?: Record<string, boolean>;
    } = {}
  ) =>
    mutate<TaskDelivery>(
      token,
      `/api/tasks/${taskId}/runs/${runId}/delivery/pull-request`,
      "POST",
      body
    ),
  mergeDelivery: (
    token: string,
    taskId: string,
    runId: string,
    body: { merge_strategy?: MergeStrategy; confirmations?: Record<string, boolean> } = {}
  ) => mutate<TaskDelivery>(token, `/api/tasks/${taskId}/runs/${runId}/delivery/merge`, "POST", body),
  teardownDelivery: (
    token: string,
    taskId: string,
    runId: string,
    body: { retention?: string; confirmations?: Record<string, boolean> } = {}
  ) =>
    mutate<TaskDelivery>(token, `/api/tasks/${taskId}/runs/${runId}/delivery/teardown`, "POST", {
      retention: body.retention,
      confirmations: body.confirmations ?? {},
    }),
  releaseStage: (token: string, boardId: string) =>
    mutate<ReleaseOpResponse>(token, `/api/task-boards/${boardId}/releases/stage`, "POST"),
  releaseSwitch: (token: string, boardId: string, body: { drain?: boolean } = {}) =>
    mutate<ReleaseOpResponse>(
      token,
      `/api/task-boards/${boardId}/releases/switch`,
      "POST",
      body
    ),
  releases: (token: string, boardId: string) =>
    get<{
      releases: ReleaseDeployment[];
      live: ReleaseDeployment | null;
      staged: ReleaseDeployment | null;
      remote_tip: string | null;
    }>(token, `/api/task-boards/${boardId}/releases`),
  releaseRollback: (token: string, boardId: string, confirmRollback = false) =>
    mutate<ReleaseOpResponse>(token, `/api/task-boards/${boardId}/releases/rollback`, "POST", {
      confirm_rollback: confirmRollback,
    }),
};
