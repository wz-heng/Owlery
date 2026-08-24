import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Task, TaskDetail } from "../../api/tasks";
import { TaskDrawer } from "./TaskDrawer";

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: "task-1",
    board_id: "board-1",
    parent_task_id: null,
    title: "Ship the feature",
    body: "Do the thing",
    status: "triage",
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

// `detail` landing (vs. staying `undefined`) is the store's race-free signal
// that `runs` has been fetched for this task at least once — see the
// `runsLoaded` comment in TaskDrawer.tsx. Tests exercising the Close button's
// run-history-dependent branches must supply this, or they'd only be
// exercising the "still loading" branch.
function taskDetail(t: Task, children: Task[] = []): TaskDetail {
  return { ...t, dependencies: [], dependents: [], children };
}

function baseProps(overrides: Partial<Parameters<typeof TaskDrawer>[0]> = {}) {
  return {
    task: task(),
    detail: undefined,
    runs: [],
    comments: [],
    artifacts: [],
    allTasks: [task()],
    agents: [],
    loading: false,
    busy: false,
    error: null,
    onClose: vi.fn(),
    onSave: vi.fn().mockResolvedValue(undefined),
    onLifecycle: vi.fn().mockResolvedValue(true),
    onArchive: vi.fn().mockResolvedValue(undefined),
    onComment: vi.fn().mockResolvedValue(true),
    onAddDependency: vi.fn().mockResolvedValue(true),
    onRemoveDependency: vi.fn().mockResolvedValue(true),
    onCloseTask: vi.fn().mockResolvedValue(true),
    onRefresh: vi.fn(),
    onOpenTask: vi.fn(),
    ...overrides,
  };
}

afterEach(cleanup);

describe("TaskDrawer verdict", () => {
  it("shows a Review failed banner for a done task with verdict=fail", () => {
    render(<TaskDrawer {...baseProps({ task: task({ status: "done", verdict: "fail", result_summary: "Found issues." }) })} />);
    expect(screen.getByText("Review failed")).toBeInTheDocument();
    expect(screen.getByText(/dependents will not unblock/)).toBeInTheDocument();
    // The ordinary outcome banner still renders alongside it.
    expect(screen.getByText("Found issues.")).toBeInTheDocument();
  });

  it("renders no verdict banner for an ordinary done task", () => {
    render(<TaskDrawer {...baseProps({ task: task({ status: "done", verdict: "pass" }) })} />);
    expect(screen.queryByText("Review failed")).not.toBeInTheDocument();
  });
});

describe("TaskDrawer cancel", () => {
  it("offers Cancel for a blocked task (task-board-gaps.md §3.4 widens cancel to blocked)", () => {
    const onLifecycle = vi.fn().mockResolvedValue(true);
    render(<TaskDrawer {...baseProps({ task: task({ status: "blocked", blocked_kind: "input" }), onLifecycle })} />);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onLifecycle).toHaveBeenCalledWith("cancel");
    // Unblock is still offered alongside it — cancel doesn't replace it.
    expect(screen.getByRole("button", { name: "Unblock" })).toBeInTheDocument();
  });

  it("offers no Cancel for a running task", () => {
    render(<TaskDrawer {...baseProps({ task: task({ status: "running" }) })} />);
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
  });
});

describe("TaskDrawer close", () => {
  it("renders no Close button for a childless task", () => {
    render(<TaskDrawer {...baseProps()} />);
    expect(screen.queryByRole("button", { name: "Close" })).not.toBeInTheDocument();
  });

  it("enables Close for a never-run container whose children are all terminal", async () => {
    const root = task({ id: "root", status: "triage" });
    const child = task({ id: "child", parent_task_id: "root", status: "done" });
    const onCloseTask = vi.fn().mockResolvedValue(true);
    render(
      <TaskDrawer
        {...baseProps({
          task: root,
          allTasks: [root, child],
          detail: taskDetail(root, [child]),
          onCloseTask,
        })}
      />
    );

    const closeButton = screen.getByRole("button", { name: "Close" });
    expect(closeButton).toBeEnabled();
    fireEvent.click(closeButton);

    fireEvent.change(screen.getByLabelText("Close summary"), { target: { value: "Battle won." } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm close" }));

    await vi.waitFor(() => expect(onCloseTask).toHaveBeenCalledWith("Battle won."));
  });

  it("disables Close with a loading reason before task detail (and thus run history) has loaded — even if children look terminal and runs looks empty", () => {
    // The exact race Snape's review caught: `runs` defaults to `[]` before
    // `loadTaskDetail` resolves, indistinguishable from "genuinely no runs"
    // unless something else (here, `detail` being undefined) says so.
    const root = task({ id: "root", status: "triage" });
    const child = task({ id: "child", parent_task_id: "root", status: "done" });
    render(<TaskDrawer {...baseProps({ task: root, allTasks: [root, child], detail: undefined, runs: [] })} />);

    const closeButton = screen.getByRole("button", { name: "Close" });
    expect(closeButton).toBeDisabled();
    expect(closeButton.getAttribute("title")).toMatch(/loading/i);
  });

  it("disables Close with a reason when a child is still open", () => {
    const root = task({ id: "root", status: "triage" });
    const child = task({ id: "child", parent_task_id: "root", status: "todo" });
    render(
      <TaskDrawer {...baseProps({ task: root, allTasks: [root, child], detail: taskDetail(root, [child]) })} />
    );

    const closeButton = screen.getByRole("button", { name: "Close" });
    expect(closeButton).toBeDisabled();
    expect(closeButton.getAttribute("title")).toMatch(/not yet terminal/i);
  });

  it("disables Close with a reason when the container has run history", () => {
    const root = task({ id: "root", status: "blocked" });
    const child = task({ id: "child", parent_task_id: "root", status: "done" });
    render(
      <TaskDrawer
        {...baseProps({
          task: root,
          allTasks: [root, child],
          detail: taskDetail(root, [child]),
          runs: [
            {
              id: "run-1",
              task_id: "root",
              attempt_no: 1,
              agent_id: null,
              session_id: null,
              state: "failed",
              summary: null,
              metadata: null,
              error: null,
              workspace_mode: "shared",
              workspace_path: "/tmp",
              claimed_at: "2026-07-26T00:00:00Z",
              started_at: null,
              last_heartbeat_at: null,
              lease_expires_at: null,
              finished_at: null,
            },
          ],
        })}
      />
    );

    const closeButton = screen.getByRole("button", { name: "Close" });
    expect(closeButton).toBeDisabled();
    expect(closeButton.getAttribute("title")).toMatch(/worker completion protocol/i);
  });
});

describe("TaskDrawer terminal immutability", () => {
  it("disables every editable field for a cancelled task, same as done", () => {
    render(<TaskDrawer {...baseProps({ task: task({ status: "cancelled" }) })} />);
    expect(screen.getByLabelText("Title")).toBeDisabled();
    expect(screen.getByLabelText("Assignee")).toBeDisabled();
    expect(screen.getByLabelText("Priority")).toBeDisabled();
    expect(screen.getByLabelText("Workspace")).toBeDisabled();
  });

  it("never shows Save changes for a cancelled task even if its local title state becomes dirty", () => {
    // fireEvent bypasses the DOM's own "disabled inputs don't fire events"
    // restriction, so this exercises the `!isTerminal` guard on the Save
    // button directly rather than relying on `dirty` never becoming true.
    render(<TaskDrawer {...baseProps({ task: task({ status: "cancelled" }) })} />);
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Edited anyway" } });
    expect(screen.queryByRole("button", { name: "Save changes" })).not.toBeInTheDocument();
  });
});
