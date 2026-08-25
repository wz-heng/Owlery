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
    verdict: null,
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

  it("offers a batch archive for delivered done tasks in view, and skips still-active or failed deliveries", async () => {
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
      // A terminal-but-unhappy delivery status must NOT be swept into a
      // batch archive labeled "finished" — it still needs a human's eyes
      // (Snape review: failed/blocked/conflicted aren't "delivered").
      task("problem", "done", "Failed delivery", {
        delivery: {
          status: "failed", dirty: false, commits_ahead: 0, pushed_ref: null,
          pr_number: null, pr_state: null, merge_strategy: null, reason_kind: "op_failed",
        },
      }),
    ];
    render(
      <KanbanColumns tasks={tasks} agents={[]} onOpenTask={vi.fn()} onMoveTask={vi.fn().mockResolvedValue(true)} />
    );

    const archiveButton = screen.getByRole("button", { name: /Archive 2 finished/ });
    fireEvent.click(archiveButton);

    await vi.waitFor(() => expect(archive).toHaveBeenCalledTimes(2));
    expect(archive).toHaveBeenCalledWith("token", "plain", true);
    expect(archive).toHaveBeenCalledWith("token", "shipped", true);
    expect(archive).not.toHaveBeenCalledWith("token", "mid-flight", true);
    expect(archive).not.toHaveBeenCalledWith("token", "problem", true);
  });

  it("folds cancelled tasks into the Done column tail instead of a seventh column (task-board-gaps.md §3.4)", () => {
    render(
      <KanbanColumns
        tasks={[
          task("d1", "done", "Shipped"),
          task("c1", "cancelled", "Superseded by a redo"),
        ]}
        agents={[]}
        onOpenTask={vi.fn()}
        onMoveTask={vi.fn().mockResolvedValue(true)}
      />
    );
    // Still exactly six columns — cancelled never gets its own, and never
    // reappears under Blocked.
    for (const label of ["Triage", "Todo", "Ready", "Running", "Blocked", "Done"]) {
      expect(screen.getByRole("region", { name: `${label} tasks` })).toBeInTheDocument();
    }
    const doneColumn = screen.getByRole("region", { name: "Done tasks" });
    expect(doneColumn).toHaveTextContent("Shipped");
    expect(doneColumn).toHaveTextContent("Superseded by a redo");
    expect(doneColumn).toHaveTextContent("Cancelled");
    // The header count reflects everything actually shown in the column.
    expect(doneColumn.querySelector("header")).toHaveTextContent("2");
  });

  it("batch-archives a cancelled task alongside done ones", async () => {
    const archive = vi.spyOn(taskApi, "archiveTask").mockImplementation(
      async (_token, taskId) => task(taskId, "cancelled", taskId, { archived: true })
    );
    render(
      <KanbanColumns
        tasks={[task("cancelled-1", "cancelled", "Abandoned")]}
        agents={[]}
        onOpenTask={vi.fn()}
        onMoveTask={vi.fn().mockResolvedValue(true)}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /Archive 1 finished/ }));
    await vi.waitFor(() => expect(archive).toHaveBeenCalledWith("token", "cancelled-1", true));
  });
});
