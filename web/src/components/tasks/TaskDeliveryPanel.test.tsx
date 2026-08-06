import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  taskApi,
  type TaskDelivery,
  type TaskDeliveryOp,
  type TaskRun,
} from "../../api/tasks";
import { resetTaskStore, useTaskStore } from "../../stores/taskStore";
import { TaskDeliveryPanel } from "./TaskDeliveryPanel";

const run: TaskRun = {
  id: "run-1",
  task_id: "task-1",
  attempt_no: 1,
  agent_id: "agent-1",
  session_id: "session-1",
  state: "completed",
  summary: "Implemented",
  metadata: {},
  error: null,
  workspace_mode: "git_worktree",
  workspace_path: "/tmp/worktree",
  claimed_at: "2026-07-27T00:00:00Z",
  started_at: "2026-07-27T00:00:01Z",
  last_heartbeat_at: "2026-07-27T00:01:00Z",
  lease_expires_at: null,
  finished_at: "2026-07-27T00:02:00Z",
  cost: 0.1,
};

function delivery(overrides: Partial<TaskDelivery> = {}): TaskDelivery {
  return {
    id: "delivery-1",
    task_id: run.task_id,
    run_id: run.id,
    status: "ready",
    repository: "/repo",
    base_ref: "main",
    base_head: "aaaaaaaaaaaa",
    attempt_branch: "owlery/task-1-run-1",
    attempt_head: "bbbbbbbbbbbb",
    dirty: false,
    commits_ahead: 2,
    diffstat: { files: 2, insertions: 12, deletions: 3 },
    remote_name: "origin",
    remote_url: "git@example.com:acme/repo.git",
    pushed_ref: "",
    pr_number: null,
    pr_url: "",
    pr_state: "",
    merge_strategy: "fast_forward_only",
    retention: "keep",
    reason_kind: null,
    reason_detail: null,
    deployed_sha: null,
    deployed_slot: null,
    created_at: "2026-07-27T00:02:01Z",
    updated_at: "2026-07-27T00:02:01Z",
    ...overrides,
  };
}

function op(overrides: Partial<TaskDeliveryOp> = {}): TaskDeliveryOp {
  return {
    id: "op-1",
    delivery_id: "delivery-1",
    kind: "push",
    source_key: "task:task-1:run:run-1:push:1",
    external: true,
    state: "succeeded",
    request: {},
    result: { ref: "refs/heads/owlery/task-1-run-1" },
    error: null,
    actor_kind: "user",
    actor_agent_id: null,
    started_at: "2026-07-27T00:03:00Z",
    finished_at: "2026-07-27T00:03:01Z",
    created_at: "2026-07-27T00:03:00Z",
    ...overrides,
  };
}

function seed(row: TaskDelivery, ops: TaskDeliveryOp[] = []): void {
  useTaskStore.setState({
    token: "",
    deliveries: { [run.id]: row },
    deliveryOps: { [row.id]: ops },
  });
}

beforeEach(() => {
  resetTaskStore();
  vi.restoreAllMocks();
});

afterEach(cleanup);

describe("TaskDeliveryPanel", () => {
  it("offers Accept before a delivery row exists", async () => {
    const accept = vi.spyOn(taskApi, "acceptDelivery").mockResolvedValue(
      delivery({ status: "ready" })
    );
    render(<TaskDeliveryPanel run={run} allowLocalDeploy={false} />);
    useTaskStore.setState({ token: "token" });

    expect(screen.getByText("Not accepted")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() =>
      expect(accept).toHaveBeenCalledWith("token", run.task_id, run.id, {
        base_ref: undefined,
        confirmations: undefined,
      })
    );
  });

  it("enables only legal actions and renders the durable op log", () => {
    seed(delivery(), [op()]);
    render(<TaskDeliveryPanel run={run} allowLocalDeploy={false} />);

    expect(screen.getByLabelText("Git delivery")).toHaveTextContent("ready");
    expect(screen.getByRole("button", { name: "Accept" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Commit" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Push" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Open PR" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Merge" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Teardown" })).toBeDisabled();
    expect(screen.getByText("push")).toBeInTheDocument();
    expect(screen.getByText("succeeded")).toBeInTheDocument();
    expect(screen.getByText(/ref: refs\/heads\/owlery\/task-1-run-1/)).toBeInTheDocument();
  });

  it("shows blocked evidence and allows terminal teardown", () => {
    seed(
      delivery({
        status: "blocked",
        reason_kind: "interrupted",
        reason_detail: "server restarted; external effect is unknown",
      }),
      [op({ state: "interrupted", error: "server restarted" })]
    );
    render(<TaskDeliveryPanel run={run} allowLocalDeploy={false} />);

    expect(screen.getByText(/interrupted:/)).toBeInTheDocument();
    expect(screen.getByText(/external effect is unknown/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Teardown" })).toBeEnabled();
    expect(screen.getByText("interrupted", { selector: "span" })).toBeInTheDocument();
  });

  it("passes the selected retention policy to teardown", async () => {
    seed(delivery({ status: "delivered" }));
    const teardown = vi.spyOn(taskApi, "teardownDelivery").mockResolvedValue(
      delivery({ status: "delivered", retention: "remove_worktree_keep_branch" })
    );
    render(<TaskDeliveryPanel run={run} allowLocalDeploy={false} />);
    useTaskStore.setState({ token: "token" });

    fireEvent.change(screen.getByLabelText("Retention"), {
      target: { value: "remove_worktree_keep_branch" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Teardown" }));
    await waitFor(() =>
      expect(teardown).toHaveBeenCalledWith("token", run.task_id, run.id, {
        retention: "remove_worktree_keep_branch",
        confirmations: undefined,
      })
    );
  });

  it("shows the opt-in deploy controls and stages a settled delivery", async () => {
    seed(delivery());
    useTaskStore.setState({ token: "token" });
    const deployments = vi.spyOn(taskApi, "deployments").mockResolvedValue({
      deployments: [],
      live: null,
    });
    const stage = vi.spyOn(taskApi, "deployStage").mockResolvedValue(delivery());

    render(<TaskDeliveryPanel run={run} allowLocalDeploy />);

    expect(screen.getByLabelText("Local deploy")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Switch" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Stage" }));
    await waitFor(() => expect(stage).toHaveBeenCalledWith("token", run.task_id, run.id));
    await waitFor(() => expect(deployments).toHaveBeenCalled());
  });

  it("renders rollback only for this delivery's live deployment", async () => {
    seed(delivery({ deployed_sha: "bbbbbbbbbbbb", deployed_slot: "b" }));
    useTaskStore.setState({ token: "token" });
    vi.spyOn(taskApi, "deployments").mockResolvedValue({
      deployments: [{
        id: "live-1", delivery_id: "delivery-1", task_id: run.task_id, op_id: "op-2",
        slot: "b", sha: "bbbbbbbbbbbb", source_repo: "/repo", state: "live", journal: null,
        created_at: "2026-07-27T00:00:00Z", updated_at: "2026-07-27T00:00:00Z",
      }],
      live: null,
    });
    render(<TaskDeliveryPanel run={run} allowLocalDeploy />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Rollback" })).toBeEnabled());
  });

  it("requires the typed destructive phrase before resubmitting", async () => {
    seed(delivery({ status: "blocked" }));
    useTaskStore.setState({
      token: "token",
      deliveryConfirmation: {
        taskId: run.task_id,
        runId: run.id,
        action: "push",
        confirmation: "allow_force_push",
        verb: "push",
        message: "Remote history moved; confirm a force-with-lease push.",
      },
    });
    const push = vi.spyOn(taskApi, "pushDelivery").mockResolvedValue(
      delivery({ status: "delivered", pushed_ref: "refs/heads/owlery/task-1-run-1" })
    );

    render(<TaskDeliveryPanel run={run} allowLocalDeploy={false} />);
    const confirm = screen.getByRole("button", { name: "Confirm" });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText("confirmation phrase"), {
      target: { value: "wrong" },
    });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText("confirmation phrase"), {
      target: { value: "push" },
    });
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);

    await waitFor(() => expect(push).toHaveBeenCalledWith("token", run.task_id, run.id, {
      confirmations: { allow_force_push: true },
    }));
    expect(useTaskStore.getState().deliveryConfirmation).toBeNull();
  });
});
