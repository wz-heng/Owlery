import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Task } from "../../api/tasks";
import { TaskTree } from "./TaskTree";

function task(id: string, title: string, parent: string | null, deps = 0): Task {
  return {
    id,
    board_id: "board-1",
    parent_task_id: parent,
    title,
    body: "",
    status: "todo",
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
    dependency_count: deps,
  };
}

afterEach(cleanup);

describe("TaskTree", () => {
  it("renders decomposition nesting separately from dependency counts", () => {
    const open = vi.fn();
    render(<TaskTree tasks={[task("root", "Ship board", null), task("child", "Backend", "root", 2)]} agents={[]} onOpenTask={open} />);
    expect(screen.getByText("Ship board")).toBeInTheDocument();
    expect(screen.getByText("Backend")).toBeInTheDocument();
    expect(screen.getAllByTitle(/Execution dependencies/).some((node) => node.textContent?.includes("2"))).toBe(true);
    expect(screen.getAllByTitle(/Tree children/).some((node) => node.textContent?.includes("1"))).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Collapse children" }));
    expect(screen.queryByText("Backend")).not.toBeInTheDocument();
  });

  it("keeps an orphan visible as a root", () => {
    render(<TaskTree tasks={[task("orphan", "Orphaned child", "archived-parent")]} agents={[]} onOpenTask={vi.fn()} />);
    expect(screen.getByText("Orphaned child")).toBeInTheDocument();
  });

  it("the dependency badge opens that task without toggling tree collapse", () => {
    const open = vi.fn();
    render(
      <TaskTree
        tasks={[task("root", "Ship board", null), task("child", "Backend", "root", 2)]}
        agents={[]}
        onOpenTask={open}
      />
    );
    fireEvent.click(screen.getByRole("button", { name: /2/ }));
    expect(open).toHaveBeenCalledWith("child");
    // Still expanded — the badge click must not also trigger the row's
    // collapse toggle (task-board-overhaul.md §3.3: a dependency edge is a
    // distinct affordance from tree nesting, not a re-skinned toggle).
    expect(screen.getByText("Ship board")).toBeInTheDocument();
  });
});
