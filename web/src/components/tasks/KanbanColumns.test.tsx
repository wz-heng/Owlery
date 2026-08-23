import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { taskApi, type Task } from "../../api/tasks";
import type { Agent } from "../../stores/sessionStore";
import { resetTaskStore, useTaskStore } from "../../stores/taskStore";
import { KanbanColumns } from "./KanbanColumns";

function task(id: string, status: Task["status"], title: string, overrides: Partial<Task> = {}): Task {
  return {
    id,
    board_id: "board-1",
    parent_task_id: null,
    title,
    body: "",
    status,
    assignee_agent_id: id === "one" ? "agent-1" : null,
    priority: 0,
    origin_session_id: null,
    scheduled_at: null,
    workspace_mode: null,
    working_dir_override: null,
    current_run_id: null,
    blocked_kind: status === "blocked" ? "input" : null,
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
  useTaskStore.setState({ token: "token" });
});

afterEach(cleanup);

describe("KanbanColumns", () => {
  it("renders all six lifecycle columns, counts and task ownership", () => {
    const agent = { id: "agent-1", name: "Albus" } as Agent;
    render(
      <KanbanColumns
        tasks={[task("one", "todo", "Implement board"), task("two", "blocked", "Need decision")]}
        agents={[agent]}
        onOpenTask={vi.fn()}
        onMoveTask={vi.fn().mockResolvedValue(true)}
      />
    );
    for (const label of ["Triage", "Todo", "Ready", "Running", "Blocked", "Done"]) {
      expect(screen.getByRole("region", { name: `${label} tasks` })).toBeInTheDocument();
    }
    expect(screen.getByText("Implement board")).toBeInTheDocument();
    expect(screen.getByText("Need decision")).toBeInTheDocument();
    expect(screen.getByText("Albus")).toBeInTheDocument();
    expect(screen.getByText(/input/)).toBeInTheDocument();
  });

  it("caps the Done column at 15 by default and reveals more on demand", () => {
    const doneTasks = Array.from({ length: 20 }, (_, i) => task(`done-${i}`, "done", `Done ${i}`));
    render(
      <KanbanColumns
        tasks={doneTasks}
        agents={[]}
        onOpenTask={vi.fn()}
        onMoveTask={vi.fn().mockResolvedValue(true)}
      />
    );
    const doneColumn = screen.getByRole("region", { name: "Done tasks" });
    expect(doneColumn.querySelectorAll("article")).toHaveLength(15);
    expect(screen.getByRole("button", { name: /Show 5 more \(5 older\)/ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Show 5 more/ }));
    expect(doneColumn.querySelectorAll("article")).toHaveLength(20);
    expect(screen.queryByRole("button", { name: /Show.*more/ })).not.toBeInTheDocument();
  });

  it("offers a batch archive for delivered done tasks in view, and skips still-active deliveries", async () => {
    const archive = vi.spyOn(taskApi, "archiveTask").mockImplementation(
      async (_token, taskId) => task(taskId, "done", taskId, { archived: true })
    );
    const tasks = [
      task("plain", "done", "No delivery"),
      task("shipped", "done", "Delivered", {
        delivery: {
          status: "delivered", dirty: false, commits_ahead: 0, pushed_ref: "refs/heads/x",
          pr_number: null, pr_state: null, merge_strategy: null, reason_kind: null,
        },
      }),
      task("mid-flight", "done", "Still delivering", {
        delivery: {
          status: "ready", dirty: false, commits_ahead: 1, pushed_ref: null,
          pr_number: null, pr_state: null, merge_strategy: null, reason_kind: null,
        },
      }),
    ];
    render(
      <KanbanColumns tasks={tasks} agents={[]} onOpenTask={vi.fn()} onMoveTask={vi.fn().mockResolvedValue(true)} />
    );

    const archiveButton = screen.getByRole("button", { name: /Archive 2 delivered/ });
    fireEvent.click(archiveButton);

    await vi.waitFor(() => expect(archive).toHaveBeenCalledTimes(2));
    expect(archive).toHaveBeenCalledWith("token", "plain", true);
    expect(archive).toHaveBeenCalledWith("token", "shipped", true);
    expect(archive).not.toHaveBeenCalledWith("token", "mid-flight", true);
  });
});
