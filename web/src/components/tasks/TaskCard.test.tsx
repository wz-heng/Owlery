import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Task, TaskDeliverySummary } from "../../api/tasks";
import { TaskCard } from "./TaskCard";

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: "task-abcdef",
    board_id: "board-1",
    parent_task_id: null,
    title: "Ship the feature",
    body: "",
    status: "done",
    assignee_agent_id: null,
    priority: 0,
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

function delivery(overrides: Partial<TaskDeliverySummary> = {}): TaskDeliverySummary {
  return {
    status: "ready",
    dirty: false,
    commits_ahead: 0,
    pushed_ref: null,
    pr_number: null,
    pr_state: null,
    merge_strategy: null,
    reason_kind: null,
    ...overrides,
  };
}

afterEach(cleanup);

describe("TaskCard delivery chip", () => {
  it("renders an attention chip for a dirty, accepted worktree run", () => {
    render(
      <TaskCard
        task={task({
          latest_run_state: "completed",
          latest_run_workspace_mode: "git_worktree",
          delivery: delivery({ status: "ready", dirty: true }),
        })}
        onOpen={vi.fn()}
      />
    );
    const chip = screen.getByTestId("task-delivery-chip");
    expect(chip).toHaveTextContent("Uncommitted");
    expect(chip.className).toContain("text-attention");
  });

  it("renders a success chip once merged", () => {
    render(
      <TaskCard
        task={task({
          latest_run_state: "completed",
          latest_run_workspace_mode: "git_worktree",
          delivery: delivery({ status: "delivered", merge_strategy: "fast_forward_only" }),
        })}
        onOpen={vi.fn()}
      />
    );
    const chip = screen.getByTestId("task-delivery-chip");
    expect(chip).toHaveTextContent("Merged");
    expect(chip.className).toContain("text-success");
  });

  it("shows 'Not accepted' for a completed worktree run with no delivery", () => {
    render(
      <TaskCard
        task={task({
          latest_run_state: "completed",
          latest_run_workspace_mode: "git_worktree",
        })}
        onOpen={vi.fn()}
      />
    );
    expect(screen.getByTestId("task-delivery-chip")).toHaveTextContent("Not accepted");
  });

  it("renders no chip for a shared run", () => {
    render(
      <TaskCard
        task={task({ latest_run_state: "completed", latest_run_workspace_mode: "shared" })}
        onOpen={vi.fn()}
      />
    );
    expect(screen.queryByTestId("task-delivery-chip")).toBeNull();
  });
});
