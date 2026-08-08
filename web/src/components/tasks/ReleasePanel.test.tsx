import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { taskApi, TaskApiError, type ReleaseDeployment, type TaskBoard } from "../../api/tasks";
import { resetTaskStore, useTaskStore } from "../../stores/taskStore";
import { ReleasePanel } from "./ReleasePanel";

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
    allow_local_deploy: true,
    deploy_release_ref: "main",
    archived: false,
    created_at: "2026-07-28T00:00:00Z",
    updated_at: "2026-07-28T00:00:00Z",
    ...overrides,
  };
}

function release(overrides: Partial<ReleaseDeployment> = {}): ReleaseDeployment {
  return {
    id: "rel-1",
    board_id: "board-1",
    version: "r20260809.01",
    source_ref: "main",
    sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    source_repo: "/repo",
    deployment_id: "dep-1",
    state: "live",
    actor_kind: "user",
    actor_agent_id: null,
    error: null,
    created_at: "2026-08-09T00:00:00Z",
    updated_at: "2026-08-09T00:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  resetTaskStore();
  vi.restoreAllMocks();
  useTaskStore.setState({ token: "token" });
});

afterEach(cleanup);

describe("ReleasePanel", () => {
  it("shows the configured release branch and loads history on mount", async () => {
    const list = vi.spyOn(taskApi, "releases").mockResolvedValue({
      releases: [release()],
      live: release(),
      staged: null,
    });

    render(<ReleasePanel board={board()} />);

    await waitFor(() => expect(list).toHaveBeenCalledWith("token", "board-1"));
    expect(screen.getByText("main")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText(/Live r20260809\.01/)).toBeInTheDocument()
    );
  });

  it("disables Switch until a release is staged, enables it once staged", async () => {
    vi.spyOn(taskApi, "releases").mockResolvedValue({
      releases: [release({ state: "staged" })],
      live: null,
      staged: release({ state: "staged" }),
    });

    render(<ReleasePanel board={board()} />);

    await waitFor(() => expect(screen.getByText(/Staged r20260809\.01/)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /Switch/ })).toBeEnabled();
  });

  it("Switch stays disabled with no staged release", async () => {
    vi.spyOn(taskApi, "releases").mockResolvedValue({ releases: [], live: null, staged: null });

    render(<ReleasePanel board={board()} />);

    await waitFor(() => expect(screen.getByText("No live release yet.")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /Switch/ })).toBeDisabled();
  });

  it("Stage calls the release-stage endpoint and reloads history", async () => {
    vi.spyOn(taskApi, "releases").mockResolvedValue({ releases: [], live: null, staged: null });
    const stage = vi.spyOn(taskApi, "releaseStage").mockResolvedValue({
      release: release({ state: "staged" }),
      op: {
        id: "op-1", release_id: "rel-1", kind: "stage", state: "succeeded",
        request: {}, result: null, error: null, journal_ref: null,
        actor_kind: "user", actor_agent_id: null,
        started_at: null, finished_at: null, created_at: "2026-08-09T00:00:00Z",
      },
    });

    render(<ReleasePanel board={board()} />);
    await waitFor(() => expect(screen.getByText("No live release yet.")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Stage" }));
    await waitFor(() => expect(stage).toHaveBeenCalledWith("token", "board-1"));
  });

  it("Rollback requires typed confirmation before resubmitting", async () => {
    vi.spyOn(taskApi, "releases").mockResolvedValue({
      releases: [release()],
      live: release(),
      staged: null,
    });
    const rollback = vi.spyOn(taskApi, "releaseRollback").mockRejectedValueOnce(
      new TaskApiError(
        "rollback replaces the running local version; confirmation is required",
        409,
        null,
        { code: "requires_confirmation", confirmation: "confirm_rollback", action: "rollback" }
      )
    );

    render(<ReleasePanel board={board()} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Rollback" })).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Rollback" }));
    await waitFor(() => expect(rollback).toHaveBeenCalledWith("token", "board-1", false));

    const confirm = await screen.findByRole("button", { name: "Confirm" });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText("confirmation phrase"), { target: { value: "rollback" } });
    expect(confirm).toBeEnabled();

    fireEvent.click(confirm);
    await waitFor(() => expect(rollback).toHaveBeenCalledWith("token", "board-1", true));
  });

  it("Stage stays enabled without a staged release, but disabled while mutating", async () => {
    vi.spyOn(taskApi, "releases").mockResolvedValue({ releases: [], live: null, staged: null });
    render(<ReleasePanel board={board()} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Stage" })).toBeEnabled());

    useTaskStore.setState({ mutating: true });
    await waitFor(() => expect(screen.getByRole("button", { name: "Stage" })).toBeDisabled());
  });
});
