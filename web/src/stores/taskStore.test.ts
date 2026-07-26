import { beforeEach, describe, expect, it, vi } from "vitest";

import { taskApi, TaskApiError, type Task, type TaskEvent } from "../api/tasks";
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
