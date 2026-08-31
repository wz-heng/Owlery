import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { TaskRun } from "../../api/tasks";
import type { Agent } from "../../stores/sessionStore";
import { TaskRunTimeline } from "./TaskRunTimeline";

afterEach(cleanup);

describe("TaskRunTimeline", () => {
  it("shows historical attempts, evidence and session navigation", () => {
    const run: TaskRun = {
      id: "run-1", task_id: "task-1", attempt_no: 2, agent_id: "agent-1", session_id: "session-1",
      state: "completed", summary: "Implemented and verified", metadata: { tests: 42 }, error: null,
      workspace_mode: "git_worktree", workspace_path: "/tmp/task-1", claimed_at: "2026-07-26T00:00:00Z",
      started_at: "2026-07-26T00:00:01Z", last_heartbeat_at: "2026-07-26T00:01:00Z",
      lease_expires_at: null, finished_at: "2026-07-26T00:02:00Z", cost: 0.12,
    };
    const open = vi.fn();
    render(<TaskRunTimeline runs={[run]} agents={[{ id: "agent-1", name: "Snape" } as Agent]} onOpenSession={open} />);
    expect(screen.getByText("Attempt 2")).toBeInTheDocument();
    expect(screen.getByText("Implemented and verified")).toBeInTheDocument();
    expect(screen.getByText("Snape")).toBeInTheDocument();
    expect(screen.getByText("Structured evidence")).toBeInTheDocument();
    screen.getByRole("button", { name: /Open session/ }).click();
    expect(open).toHaveBeenCalledWith("session-1");
  });

  it("expands the attempt-replay timeline panel on demand", () => {
    const run: TaskRun = {
      id: "run-1", task_id: "task-1", attempt_no: 1, agent_id: null, session_id: "session-1",
      state: "failed", summary: null, metadata: null, error: "crashed",
      workspace_mode: "copy", workspace_path: "/tmp/task-1", claimed_at: "2026-07-26T00:00:00Z",
      started_at: "2026-07-26T00:00:01Z", last_heartbeat_at: null,
      lease_expires_at: null, finished_at: "2026-07-26T00:02:00Z", cost: null,
    };
    render(<TaskRunTimeline runs={[run]} agents={[]} />);
    expect(screen.queryByText(/No timeline yet/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Timeline/ }));
    expect(screen.getByText(/No timeline yet/)).toBeInTheDocument();
  });
});
