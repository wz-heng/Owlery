import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  taskApi,
  type DeliveryChain,
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
    superseded_by_delivery_id: null,
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

function chain(overrides: Partial<DeliveryChain> = {}): DeliveryChain {
  return { target: null, superseded: [], ...overrides };
}

function seedChain(row: TaskDelivery, deliveryChain: DeliveryChain, ops: TaskDeliveryOp[] = []): void {
  useTaskStore.setState({
    token: "",
    deliveries: { [run.id]: row },
    deliveryOps: { [row.id]: ops },
    deliveryChains: { [run.id]: deliveryChain },
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
    render(<TaskDeliveryPanel run={run} />);
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
    render(<TaskDeliveryPanel run={run} />);

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

  it("gives every disabled action button a non-empty tooltip explaining why (task-board-gaps.md §3.5)", () => {
    seed(delivery());
    render(<TaskDeliveryPanel run={run} />);

    for (const label of ["Accept", "Commit", "Open PR", "Merge", "Teardown"]) {
      const button = screen.getByRole("button", { name: label });
      expect(button).toBeDisabled();
      expect(button.getAttribute("title")).toBeTruthy();
    }
    // Push is enabled in this fixture — no explanatory tooltip needed.
    expect(screen.getByRole("button", { name: "Push" })).toBeEnabled();
  });

  it("explains a pre-existing PR by name on the disabled Open PR button", () => {
    seed(delivery({ status: "delivered", pushed_ref: "refs/heads/x", pr_number: 42, pr_url: "https://example.com/pr/42" }));
    render(<TaskDeliveryPanel run={run} />);

    const openPr = screen.getByRole("button", { name: "Open PR" });
    expect(openPr).toBeDisabled();
    expect(openPr.getAttribute("title")).toMatch(/already open/i);
    // The existing PR itself is surfaced as a link, not a bare error.
    expect(screen.getByRole("link", { name: /PR #42/ })).toHaveAttribute("href", "https://example.com/pr/42");
  });

  it("explains that a delivered PR must merge on the platform", () => {
    seed(delivery({ status: "delivered", pr_number: 42 }));
    render(<TaskDeliveryPanel run={run} />);

    const mergeButton = screen.getByRole("button", { name: "Merge" });
    expect(mergeButton).toBeDisabled();
    expect(mergeButton.getAttribute("title")).toMatch(/merge on the platform/i);
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
    render(<TaskDeliveryPanel run={run} />);

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
    render(<TaskDeliveryPanel run={run} />);
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

  it("never renders a Local deploy section or deploy action buttons", () => {
    seed(delivery({ deployed_sha: "bbbbbbbbbbbb", deployed_slot: "b" }));
    render(<TaskDeliveryPanel run={run} />);

    expect(screen.queryByLabelText("Local deploy")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stage" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Switch" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Rollback" })).not.toBeInTheDocument();
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

    render(<TaskDeliveryPanel run={run} />);
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

describe("TaskDeliveryPanel supersede collapse", () => {
  it("collapses to a one-liner with no action buttons when superseded, and jumps to the target", () => {
    vi.spyOn(taskApi, "deliveryChain").mockResolvedValue(chain());
    const onOpenTask = vi.fn();
    seedChain(
      delivery({ superseded_by_delivery_id: "delivery-2" }),
      chain({ target: { delivery_id: "delivery-2", task_id: "task-2", task_title: "Deliver B", run_id: "run-2" } })
    );

    render(<TaskDeliveryPanel run={run} onOpenTask={onOpenTask} />);

    expect(screen.getByText(/Collapsed/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Teardown" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Deliver B" }));
    expect(onOpenTask).toHaveBeenCalledWith("task-2");
  });

  it("offers a full action row for the tip, plus a batch-teardown entry for what it has collapsed", async () => {
    vi.spyOn(taskApi, "deliveryChain").mockResolvedValue(chain());
    seedChain(
      delivery({ status: "delivered" }),
      chain({
        superseded: [
          { delivery_id: "delivery-9", task_id: "task-9", task_title: "Deliver A", run_id: "run-9" },
        ],
      })
    );
    useTaskStore.setState({ token: "token" });
    const teardown = vi.spyOn(taskApi, "teardownDelivery").mockResolvedValue(
      delivery({ id: "delivery-9", task_id: "task-9", run_id: "run-9", status: "delivered" })
    );

    render(<TaskDeliveryPanel run={run} />);

    expect(screen.getByRole("button", { name: "Accept" })).toBeInTheDocument();
    expect(screen.getByText(/collapsed 1 earlier delivery/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Teardown all collapsed" }));

    await waitFor(() =>
      expect(teardown).toHaveBeenCalledWith("token", "task-9", "run-9", {
        retention: "keep",
        confirmations: undefined,
      })
    );
  });

  it("gives the batch-teardown entry a tooltip when disabled while another action is in flight", () => {
    vi.spyOn(taskApi, "deliveryChain").mockResolvedValue(chain());
    seedChain(
      delivery({ status: "delivered" }),
      chain({
        superseded: [
          { delivery_id: "delivery-9", task_id: "task-9", task_title: "Deliver A", run_id: "run-9" },
        ],
      })
    );
    useTaskStore.setState({ token: "token", mutating: true });

    render(<TaskDeliveryPanel run={run} />);

    const teardownAll = screen.getByRole("button", { name: "Teardown all collapsed" });
    expect(teardownAll).toBeDisabled();
    expect(teardownAll.getAttribute("title")).toBeTruthy();
  });

  it("renders no batch-teardown entry when nothing has been collapsed", () => {
    vi.spyOn(taskApi, "deliveryChain").mockResolvedValue(chain());
    seedChain(delivery({ status: "delivered" }), chain());

    render(<TaskDeliveryPanel run={run} />);

    expect(screen.queryByRole("button", { name: "Teardown all collapsed" })).not.toBeInTheDocument();
  });
});
