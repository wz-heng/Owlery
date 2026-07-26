import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Task } from "../../api/tasks";
import type { Agent } from "../../stores/sessionStore";
import { KanbanColumns } from "./KanbanColumns";

function task(id: string, status: Task["status"], title: string): Task {
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
  };
}

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
    expect(screen.getByText("input")).toBeInTheDocument();
  });
});
