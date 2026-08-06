import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { TaskBoard } from "../../api/tasks";
import type { TaskFilters } from "../../stores/taskStore";
import { BoardToolbar } from "./BoardToolbar";

function board(overrides: Partial<TaskBoard> = {}): TaskBoard {
  return {
    id: "board-1",
    name: "Trial",
    description: "",
    working_dir: "/repo",
    default_workspace_mode: "git_worktree",
    max_running: 1,
    max_running_per_agent: null,
    max_tree_depth: 8,
    max_children_per_run: 32,
    max_open_tasks: 500,
    dispatch_enabled: true,
    git_delivery_remote: "origin",
    git_delivery_retention: "keep",
    git_delivery_author_name: "Owlery Task",
    git_delivery_author_email: "owlery-tasks@localhost",
    git_delivery_default_draft_pr: true,
    git_delivery_default_merge: "none",
    allow_local_deploy: false,
    archived: false,
    created_at: "2026-07-28T00:00:00Z",
    updated_at: "2026-07-28T00:00:00Z",
    ...overrides,
  };
}

const filters: TaskFilters = {
  text: "",
  assignee: "",
  priority: null,
  includeArchived: false,
  mine: false,
};

function renderToolbar(props: Partial<React.ComponentProps<typeof BoardToolbar>> = {}) {
  const onUpdateBoard = vi.fn().mockResolvedValue(undefined);
  const selected = props.selectedBoard ?? board();
  render(
    <BoardToolbar
      boards={[selected]}
      selectedBoard={selected}
      dispatcher={null}
      view="kanban"
      filters={filters}
      agents={[]}
      mutating={false}
      onSelectBoard={vi.fn()}
      onViewChange={vi.fn()}
      onFiltersChange={vi.fn()}
      onCreateBoard={vi.fn().mockResolvedValue(true)}
      onUpdateBoard={onUpdateBoard}
      onArchiveBoard={vi.fn().mockResolvedValue(undefined)}
      onToggleDispatcher={vi.fn().mockResolvedValue(undefined)}
      onNewTask={vi.fn()}
      {...props}
    />
  );
  return { onUpdateBoard };
}

function openSettings() {
  fireEvent.click(screen.getByLabelText("Board settings"));
}

afterEach(cleanup);

describe("BoardToolbar concurrency limits", () => {
  it("prefills the concurrency inputs from the board (null → blank)", () => {
    renderToolbar();
    openSettings();
    expect(screen.getByLabelText("Max running tasks")).toHaveValue(1);
    // null renders as an empty value on a number input
    expect(screen.getByLabelText("Max running per agent")).toHaveValue(null);
  });

  it("saves edited limits as integers and clears blanks to null", async () => {
    const { onUpdateBoard } = renderToolbar();
    openSettings();
    fireEvent.change(screen.getByLabelText("Max running tasks"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Max running per agent"), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onUpdateBoard).toHaveBeenCalledTimes(1));
    expect(onUpdateBoard).toHaveBeenCalledWith(
      expect.objectContaining({ max_running: null, max_running_per_agent: 3 })
    );
  });

  it("disables Save when a limit is not a positive whole number", () => {
    renderToolbar();
    openSettings();
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeEnabled();
    fireEvent.change(screen.getByLabelText("Max running tasks"), { target: { value: "0" } });
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Max running tasks"), { target: { value: "2" } });
    expect(save).toBeEnabled();
  });

  it("passes the limits through on create", async () => {
    const onCreateBoard = vi.fn().mockResolvedValue(true);
    renderToolbar({ onCreateBoard });
    fireEvent.click(screen.getByLabelText("Create board"));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New" } });
    fireEvent.change(screen.getByLabelText("Working directory"), { target: { value: "/x" } });
    fireEvent.change(screen.getByLabelText("Max running tasks"), { target: { value: "4" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(onCreateBoard).toHaveBeenCalledTimes(1));
    expect(onCreateBoard).toHaveBeenCalledWith(
      expect.objectContaining({ max_running: 4, max_running_per_agent: null })
    );
  });

  it("prefills and saves the local deployment opt-in", async () => {
    const { onUpdateBoard } = renderToolbar({ selectedBoard: board({ allow_local_deploy: true }) });
    openSettings();
    const optIn = screen.getByLabelText("Enable local deployment");
    expect(optIn).toBeChecked();
    fireEvent.click(optIn);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onUpdateBoard).toHaveBeenCalledTimes(1));
    expect(onUpdateBoard).toHaveBeenCalledWith(expect.objectContaining({ allow_local_deploy: false }));
  });
});
