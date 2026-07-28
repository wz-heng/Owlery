import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { TaskRun } from "../../api/tasks";
import type { Agent } from "../../stores/sessionStore";
import { TaskRunTimeline } from "./TaskRunTimeline";

let restoreClipboard: (() => void) | null = null;

function installClipboard(writeText: ReturnType<typeof vi.fn>) {
  const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  restoreClipboard = () => {
    if (original) Object.defineProperty(navigator, "clipboard", original);
    else delete (navigator as { clipboard?: unknown }).clipboard;
    restoreClipboard = null;
  };
}

afterEach(() => {
  restoreClipboard?.();
  vi.useRealTimers();
  cleanup();
});

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
    installClipboard(writeText);
    render(<TaskRunTimeline runs={[makeRun({ workspace_path: "/tmp/task-1" })]} agents={[]} />);
    fireEvent.click(screen.getByRole("button", { name: "Copy workspace path" }));
    expect(writeText).toHaveBeenCalledWith("/tmp/task-1");
    expect(await screen.findByText("Copied")).toBeInTheDocument();
  });

  it("returns to the normal state after the acknowledgement timeout", async () => {
    vi.useFakeTimers();
    const writeText = vi.fn().mockResolvedValue(undefined);
    installClipboard(writeText);
    render(<TaskRunTimeline runs={[makeRun()]} agents={[]} />);
    fireEvent.click(screen.getByRole("button", { name: "Copy workspace path" }));
    await act(async () => {}); // flush the awaited clipboard promise + state update
    expect(screen.getByText("Copied")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(1400);
    });
    expect(screen.queryByText("Copied")).not.toBeInTheDocument();
  });

  it("acknowledges each run's copy independently", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    installClipboard(writeText);
    const runs = [
      makeRun({ id: "run-a", attempt_no: 1, workspace_path: "/tmp/a" }),
      makeRun({ id: "run-b", attempt_no: 2, workspace_path: "/tmp/b" }),
    ];
    render(<TaskRunTimeline runs={runs} agents={[]} />);
    const buttons = screen.getAllByRole("button", { name: "Copy workspace path" });
    // Sorted by attempt_no desc: buttons[0] is run-b (/tmp/b), buttons[1] is run-a (/tmp/a).
    fireEvent.click(buttons[0]);
    fireEvent.click(buttons[1]);
    await waitFor(() => expect(screen.getAllByText("Copied")).toHaveLength(2));
    expect(writeText).toHaveBeenCalledWith("/tmp/b");
    expect(writeText).toHaveBeenCalledWith("/tmp/a");
  });

  it("does not render the copy control when workspace_path is empty", () => {
    render(<TaskRunTimeline runs={[makeRun({ workspace_path: "" })]} agents={[]} />);
    expect(screen.queryByRole("button", { name: "Copy workspace path" })).not.toBeInTheDocument();
  });
});
