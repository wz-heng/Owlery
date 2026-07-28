import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { TaskRun } from "../../api/tasks";
import type { Agent } from "../../stores/sessionStore";
import { TaskRunTimeline } from "./TaskRunTimeline";

afterEach(cleanup);

function makeRun(overrides: Partial<TaskRun> = {}): TaskRun {
  return {
    id: "run-1", task_id: "task-1", attempt_no: 2, agent_id: "agent-1", session_id: "session-1",
    state: "completed", summary: "Implemented and verified", metadata: { tests: 42 }, error: null,
    workspace_mode: "git_worktree", workspace_path: "/tmp/task-1", claimed_at: "2026-07-26T00:00:00Z",
    started_at: "2026-07-26T00:00:01Z", last_heartbeat_at: "2026-07-26T00:01:00Z",
    lease_expires_at: null, finished_at: "2026-07-26T00:02:00Z", cost: 0.12,
    ...overrides,
  };
}

describe("TaskRunTimeline", () => {
  it("shows historical attempts, evidence and session navigation", () => {
    const open = vi.fn();
    render(<TaskRunTimeline runs={[makeRun()]} agents={[{ id: "agent-1", name: "Snape" } as Agent]} onOpenSession={open} />);
    expect(screen.getByText("Attempt 2")).toBeInTheDocument();
    expect(screen.getByText("Implemented and verified")).toBeInTheDocument();
    expect(screen.getByText("Snape")).toBeInTheDocument();
    expect(screen.getByText("Structured evidence")).toBeInTheDocument();
    screen.getByRole("button", { name: /Open session/ }).click();
    expect(open).toHaveBeenCalledWith("session-1");
  });

  it("copies the workspace path and shows a Copied acknowledgement", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<TaskRunTimeline runs={[makeRun({ workspace_path: "/tmp/task-1" })]} agents={[]} />);
    const copyButton = screen.getByRole("button", { name: "Copy workspace path" });
    fireEvent.click(copyButton);
    expect(writeText).toHaveBeenCalledWith("/tmp/task-1");
    expect(await screen.findByText("Copied")).toBeInTheDocument();
  });

  it("does not render the copy control when workspace_path is empty", () => {
    render(<TaskRunTimeline runs={[makeRun({ workspace_path: "" })]} agents={[]} />);
    expect(screen.queryByRole("button", { name: "Copy workspace path" })).not.toBeInTheDocument();
  });
});
