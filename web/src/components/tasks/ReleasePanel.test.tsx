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

function releasesResponse(overrides: {
  releases?: ReleaseDeployment[];
  total?: number;
  live?: ReleaseDeployment | null;
  staged?: ReleaseDeployment | null;
  remote_tip?: string | null;
} = {}) {
  const releases = overrides.releases ?? [];
  return {
    releases,
    total: overrides.total ?? releases.length,
    limit: 10,
    offset: 0,
    live: null,
    staged: null,
    remote_tip: null,
    ...overrides,
  };
}

beforeEach(() => {
  resetTaskStore();
  vi.restoreAllMocks();
  // Releases default collapsed (task-board-overhaul.md §3.2); most tests
  // below exercise the expanded action/history surface.
  useTaskStore.setState({ token: "token", releasesExpanded: true });
});

afterEach(cleanup);

describe("ReleasePanel", () => {
  it("shows the configured release branch and loads history on mount", async () => {
    const list = vi.spyOn(taskApi, "releases").mockResolvedValue(
      releasesResponse({ releases: [release()], live: release(), remote_tip: release().sha })
    );

    render(<ReleasePanel board={board()} />);

    await waitFor(() =>
      expect(list).toHaveBeenCalledWith("token", "board-1", { limit: 10, offset: 0 })
    );
    expect(screen.getByText("main")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText(/Live r20260809\.01/)).toBeInTheDocument()
    );
    // remote tip == live sha here, so no "new commits available" affordance.
    await waitFor(() => expect(screen.getByText(/Remote tip/)).toBeInTheDocument());
    expect(screen.queryByText("New commits available to stage.")).not.toBeInTheDocument();
  });

  it("still shows Live and enables Rollback when the live release has aged off the first history page", async () => {
    // Regression (Snape review): live/staged must come from the dedicated
    // server-resolved fields, never derived from `releases.find(...)` over
    // just the first page — here the live row (r-live, 11 releases back)
    // isn't among the 10 newer terminal rows the page actually returns.
    const olderLive = release({ id: "r-live", version: "r-live", state: "live" });
    const newerPage = Array.from({ length: 10 }, (_, i) =>
      release({ id: `r-newer-${i}`, version: `r-newer-${i}`, state: "failed" })
    );
    vi.spyOn(taskApi, "releases").mockResolvedValue(
      releasesResponse({ releases: newerPage, live: olderLive, total: 11 })
    );

    render(<ReleasePanel board={board()} />);

    await waitFor(() => expect(screen.getByText(/Live r-live/)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Rollback" })).toBeInTheDocument();
    // The fetched history page itself is unaffected by the live/staged fix —
    // it still shows the 10 newer (non-live) rows the page actually returned.
    expect(screen.getByText("r-newer-0")).toBeInTheDocument();
  });

  it("flags new commits available when the remote tip is ahead of live and nothing is staged", async () => {
    vi.spyOn(taskApi, "releases").mockResolvedValue(
      releasesResponse({
        releases: [release()], live: release(), remote_tip: "b".repeat(40),
      })
    );

    render(<ReleasePanel board={board()} />);

    await waitFor(() =>
      expect(screen.getByText("New commits available to stage.")).toBeInTheDocument()
    );
  });

  it("surfaces the busy census when a release action is refused as not idle", async () => {
    vi.spyOn(taskApi, "releases").mockResolvedValue(
      releasesResponse({ releases: [release({ state: "staged" })], staged: release({ state: "staged" }) })
    );
    const switchRelease = vi.spyOn(taskApi, "releaseSwitch").mockResolvedValue({
      release: release({ state: "staged" }),
      op: {
        id: "op-1", release_id: "rel-1", kind: "switch", state: "failed",
        request: {}, result: null,
        error: "not_idle: instance is busy — session_turn:s1",
        journal_ref: null, actor_kind: "user", actor_agent_id: null,
        started_at: null, finished_at: null, created_at: "2026-08-09T00:00:00Z",
      },
    });

    render(<ReleasePanel board={board()} />);
    await waitFor(() => expect(screen.getByRole("button", { name: /Switch/ })).toBeEnabled());

    fireEvent.click(screen.getByRole("button", { name: /Switch/ }));
    await waitFor(() => expect(switchRelease).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.getByText(/not_idle: instance is busy — session_turn:s1/)).toBeInTheDocument()
    );
  });

  it("disables Switch until a release is staged, enables it once staged", async () => {
    vi.spyOn(taskApi, "releases").mockResolvedValue(
      releasesResponse({ releases: [release({ state: "staged" })], staged: release({ state: "staged" }) })
    );

    render(<ReleasePanel board={board()} />);

    await waitFor(() => expect(screen.getByText(/Staged r20260809\.01/)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /Switch/ })).toBeEnabled();
  });

  it("Switch stays disabled with no staged release", async () => {
    vi.spyOn(taskApi, "releases").mockResolvedValue(releasesResponse());

    render(<ReleasePanel board={board()} />);

    await waitFor(() => expect(screen.getByText("No live release yet.")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /Switch/ })).toBeDisabled();
  });

  it("Stage calls the release-stage endpoint and reloads history", async () => {
    vi.spyOn(taskApi, "releases").mockResolvedValue(releasesResponse());
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
    vi.spyOn(taskApi, "releases").mockResolvedValue(
      releasesResponse({ releases: [release()], live: release() })
    );
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
    vi.spyOn(taskApi, "releases").mockResolvedValue(releasesResponse());
    render(<ReleasePanel board={board()} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Stage" })).toBeEnabled());

    useTaskStore.setState({ mutating: true });
    await waitFor(() => expect(screen.getByRole("button", { name: "Stage" })).toBeDisabled());
  });

  it("defaults to collapsed: only the current row shows, no action buttons or full history", async () => {
    useTaskStore.setState({ releasesExpanded: false });
    vi.spyOn(taskApi, "releases").mockResolvedValue(
      releasesResponse({ releases: [release()], live: release(), total: 1 })
    );

    render(<ReleasePanel board={board()} />);

    await waitFor(() => expect(screen.getByText("r20260809.01")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Stage" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Switch/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/History/)).not.toBeInTheDocument();
  });

  it("expands on toggle to reveal actions and history, and collapses back", async () => {
    useTaskStore.setState({ releasesExpanded: false });
    vi.spyOn(taskApi, "releases").mockResolvedValue(
      releasesResponse({ releases: [release()], live: release(), total: 1 })
    );

    render(<ReleasePanel board={board()} />);
    await waitFor(() => expect(screen.getByText("r20260809.01")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /Releases/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Stage" })).toBeInTheDocument());
    expect(screen.getByText(/History/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Releases/ }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Stage" })).not.toBeInTheDocument());
  });

  it("shows a load-more control when more history exists, and appends the next page", async () => {
    const list = vi.spyOn(taskApi, "releases");
    list.mockResolvedValueOnce(
      releasesResponse({ releases: [release({ id: "rel-1", version: "r1" })], total: 3 })
    );
    render(<ReleasePanel board={board()} />);
    await waitFor(() => expect(screen.getByText("r1")).toBeInTheDocument());
    expect(screen.getByText(/History/)).toHaveTextContent("3");
    expect(screen.getByRole("button", { name: /Load more \(2 older\)/ })).toBeInTheDocument();

    list.mockResolvedValueOnce(
      releasesResponse({
        releases: [release({ id: "rel-2", version: "r2" }), release({ id: "rel-3", version: "r3" })],
        total: 3,
      })
    );
    fireEvent.click(screen.getByRole("button", { name: /Load more/ }));

    await waitFor(() => expect(list).toHaveBeenCalledWith("token", "board-1", { limit: 10, offset: 1 }));
    await waitFor(() => expect(screen.getByText("r2")).toBeInTheDocument());
    expect(screen.getByText("r3")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Load more/ })).not.toBeInTheDocument();
  });
});
