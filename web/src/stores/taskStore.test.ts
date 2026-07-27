import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  taskApi,
  TaskApiError,
  type Task,
  type TaskDelivery,
  type TaskDeliveryOp,
  type TaskEvent,
} from "../api/tasks";
import { dragOperation, filterTasks, resetTaskStore, useTaskStore } from "./taskStore";

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: "task-1",
    board_id: "board-1",
    parent_task_id: null,
    title: "Durable task board",
    body: "Build the visible coordination layer",
    status: "todo",
    assignee_agent_id: "agent-1",
    priority: 2,
    origin_session_id: null,
    scheduled_at: null,
    workspace_mode: null,
    working_dir_override: null,
    current_run_id: null,
    blocked_kind: null,
    blocked_reason: null,
    result_summary: null,
    archived: false,
    created_by_kind: "user",
    created_by_agent_id: null,
    created_at: "2026-07-26T00:00:00Z",
    updated_at: "2026-07-26T00:00:00Z",
    completed_at: null,
    archived_at: null,
    ...overrides,
  };
}

function delivery(overrides: Partial<TaskDelivery> = {}): TaskDelivery {
  return {
    id: "del-1",
    task_id: "task-1",
    run_id: "run-1",
    status: "ready",
    repository: "/repo",
    base_ref: "main",
    base_head: "aaaaaaaaaaaa",
    attempt_branch: "owlery/task-1",
    attempt_head: "bbbbbbbbbbbb",
    dirty: false,
    commits_ahead: 2,
    diffstat: { files: 3, insertions: 40, deletions: 5 },
    remote_name: "origin",
    remote_url: "git@example.com:acme/repo.git",
    pushed_ref: "",
    pr_number: null,
    pr_url: "",
    pr_state: "",
    merge_strategy: "fast_forward_only",
    retention: "keep",
    reason_kind: null,
    reason_detail: null,
    created_at: "2026-07-26T00:00:00Z",
    updated_at: "2026-07-26T00:00:00Z",
    ...overrides,
  };
}

function op(overrides: Partial<TaskDeliveryOp> = {}): TaskDeliveryOp {
  return {
    id: "op-1",
    delivery_id: "del-1",
    kind: "commit",
    source_key: "commit:1",
    external: false,
    state: "running",
    request: {},
    result: null,
    error: null,
    actor_kind: "system",
    actor_agent_id: null,
    started_at: "2026-07-26T00:00:01Z",
    finished_at: null,
    created_at: "2026-07-26T00:00:01Z",
    ...overrides,
  };
}

beforeEach(() => {
  resetTaskStore();
  vi.restoreAllMocks();
});
describe("taskStore transitions", () => {
  it("maps only legal drag shortcuts to guarded lifecycle operations", () => {
    expect(dragOperation("triage", "todo")).toBe("specify");
    expect(dragOperation("todo", "ready")).toBe("ready");
    expect(dragOperation("blocked", "ready")).toBe("unblock");
    expect(dragOperation("ready", "triage")).toBe("triage");
    expect(dragOperation("running", "done")).toBeNull();
    expect(dragOperation("todo", "blocked")).toBeNull();
    expect(dragOperation("done", "ready")).toBeNull();
  });

  it("invokes the lifecycle endpoint and stores the server-authoritative task", async () => {
    const current = task();
    const ready = task({ status: "ready", updated_at: "2026-07-26T00:01:00Z" });
    useTaskStore.setState({ token: "tok", tasksById: { [current.id]: current }, taskOrder: [current.id] });
    const lifecycle = vi.spyOn(taskApi, "lifecycle").mockResolvedValue(ready);

    expect(await useTaskStore.getState().moveTask(current.id, "ready")).toBe(true);
    expect(lifecycle).toHaveBeenCalledWith("tok", current.id, "ready", {});
    expect(useTaskStore.getState().tasksById[current.id].status).toBe("ready");
  });

  it("does not let a delayed task event overwrite a newer REST snapshot", () => {
    const triage = task({
      status: "triage",
      updated_at: "2026-07-26T00:00:00Z",
    });
    const specified = task({
      status: "todo",
      updated_at: "2026-07-26T00:01:00Z",
    });
    useTaskStore.setState({
      tasksById: { [specified.id]: specified },
      taskOrder: [specified.id],
    });

    useTaskStore.getState().applyTaskEvent("board-1", triage.id, {
      seq: 1,
      board_id: "board-1",
      task_id: triage.id,
      run_id: null,
      kind: "task_created",
      actor_kind: "user",
      actor_agent_id: null,
      payload: { task: triage },
      created_at: "2026-07-26T00:00:00Z",
    });

    expect(useTaskStore.getState().tasksById[specified.id].status).toBe("todo");
    expect(useTaskStore.getState().lastEventSeq["board-1"]).toBe(1);
  });

  it("uses the current task returned by a lost CAS conflict", async () => {
    const current = task();
    const claimed = task({ status: "running", current_run_id: "run-1" });
    useTaskStore.setState({ token: "tok", tasksById: { [current.id]: current }, taskOrder: [current.id] });
    vi.spyOn(taskApi, "lifecycle").mockRejectedValue(new TaskApiError("Already claimed", 409, claimed));

    expect(await useTaskStore.getState().moveTask(current.id, "ready")).toBe(false);
    expect(useTaskStore.getState().tasksById[current.id]).toEqual(claimed);
    expect(useTaskStore.getState().error).toBe("Already claimed");
  });
});

describe("taskStore event replay and filters", () => {
  it("deduplicates event seq and applies an embedded task snapshot", () => {
    const original = task();
    const updated = task({ status: "ready" });
    useTaskStore.setState({ tasksById: { [original.id]: original }, taskOrder: [original.id] });
    const event: TaskEvent = {
      seq: 4,
      board_id: "board-1",
      task_id: original.id,
      run_id: null,
      kind: "task_ready",
      actor_kind: "system",
      actor_agent_id: null,
      payload: { task: updated },
      created_at: "2026-07-26T00:01:00Z",
    };

    useTaskStore.getState().applyTaskEvent("board-1", original.id, event);
    useTaskStore.getState().applyTaskEvent("board-1", original.id, event);
    expect(useTaskStore.getState().tasksById[original.id].status).toBe("ready");
    expect(useTaskStore.getState().events[original.id]).toHaveLength(1);
    expect(useTaskStore.getState().lastEventSeq["board-1"]).toBe(4);
  });

  it("combines text, mine, priority and archived filters", () => {
    const rows = [
      task(),
      task({ id: "task-2", title: "Review API", assignee_agent_id: "agent-2", priority: 1 }),
      task({ id: "task-3", title: "Old board", archived: true }),
    ];
    expect(filterTasks(rows, { text: "durable", assignee: "", priority: 2, includeArchived: false, mine: true }, "agent-1").map((row) => row.id)).toEqual(["task-1"]);
    expect(filterTasks(rows, { text: "", assignee: "", priority: null, includeArchived: true, mine: false }, null)).toHaveLength(3);
  });
});

describe("taskStore git delivery", () => {
  function deliveryEvent(seq: number, payload: Record<string, unknown>): TaskEvent {
    return {
      seq,
      board_id: "board-1",
      task_id: "task-1",
      run_id: "run-1",
      kind: "delivery_op_finished",
      actor_kind: "system",
      actor_agent_id: null,
      payload,
      created_at: "2026-07-26T00:01:00Z",
    };
  }

  it("upserts deliveries[run_id] and appends deliveryOps[delivery_id] with seq dedup", () => {
    const event = deliveryEvent(7, {
      delivery: delivery(),
      op: op({ state: "succeeded", finished_at: "2026-07-26T00:00:02Z" }),
    });
    useTaskStore.getState().applyTaskEvent("board-1", "task-1", event);
    useTaskStore.getState().applyTaskEvent("board-1", "task-1", event); // same seq → ignored

    const state = useTaskStore.getState();
    expect(state.deliveries["run-1"].id).toBe("del-1");
    expect(state.deliveryOps["del-1"]).toHaveLength(1);
    expect(state.deliveryOps["del-1"][0].state).toBe("succeeded");
    expect(state.lastEventSeq["board-1"]).toBe(7);
  });

  it("upserts an op in place on a later state change", () => {
    useTaskStore.getState().applyTaskEvent(
      "board-1",
      "task-1",
      deliveryEvent(4, { delivery: delivery(), op: op({ state: "running" }) })
    );
    useTaskStore.getState().applyTaskEvent(
      "board-1",
      "task-1",
      deliveryEvent(5, { delivery: delivery(), op: op({ state: "succeeded" }) })
    );
    const ops = useTaskStore.getState().deliveryOps["del-1"];
    expect(ops).toHaveLength(1);
    expect(ops[0].state).toBe("succeeded");
  });

  it("surfaces a requires_confirmation error as a pending confirmation", async () => {
    useTaskStore.setState({ token: "tok" });
    vi.spyOn(taskApi, "pushDelivery").mockRejectedValue(
      new TaskApiError("Force push required", 409, null, {
        code: "requires_confirmation",
        confirmation: "allow_force_push",
        action: "push",
      })
    );

    const ok = await useTaskStore.getState().deliveryAction("task-1", "run-1", "push");
    expect(ok).toBe(false);
    const confirmation = useTaskStore.getState().deliveryConfirmation;
    expect(confirmation?.confirmation).toBe("allow_force_push");
    expect(confirmation?.action).toBe("push");
    expect(confirmation?.verb).toBe("push");
    expect(useTaskStore.getState().mutating).toBe(false);
    expect(useTaskStore.getState().error).toBeNull();
  });

  it("upserts the returned delivery on a successful action", async () => {
    useTaskStore.setState({ token: "tok" });
    vi.spyOn(taskApi, "commitDelivery").mockResolvedValue(
      delivery({ status: "ready", dirty: false, commits_ahead: 3 })
    );
    const ok = await useTaskStore.getState().deliveryAction("task-1", "run-1", "commit");
    expect(ok).toBe(true);
    expect(useTaskStore.getState().deliveries["run-1"].commits_ahead).toBe(3);
  });
});
