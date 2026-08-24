import { describe, expect, it } from "vitest";

import type {
  DeliveryStatus,
  Task,
  TaskDelivery,
  TaskDeliverySummary,
} from "../../api/tasks";
import {
  DELIVERY_TONE_PILL,
  deliveryButtonState,
  deliveryChip,
  deliveryStatusTone,
  isArchivableDoneTask,
  verdictBadge,
} from "./taskPresentation";

function summary(overrides: Partial<TaskDeliverySummary> = {}): TaskDeliverySummary {
  return {
    status: "ready",
    dirty: false,
    commits_ahead: 0,
    pushed_ref: null,
    pr_number: null,
    pr_state: null,
    merge_strategy: null,
    reason_kind: null,
    ...overrides,
  };
}

type ChipInput = Pick<
  Task,
  "delivery" | "latest_run_state" | "latest_run_workspace_mode"
>;

function input(overrides: Partial<ChipInput> = {}): ChipInput {
  return {
    delivery: null,
    latest_run_state: null,
    latest_run_workspace_mode: null,
    ...overrides,
  };
}

describe("deliveryStatusTone", () => {
  const cases: Array<[DeliveryStatus, string]> = [
    ["pending", "neutral"],
    ["preparing", "attention"],
    ["ready", "neutral"],
    ["delivering", "attention"],
    ["delivered", "success"],
    ["conflicted", "destructive"],
    ["blocked", "destructive"],
    ["failed", "destructive"],
  ];
  it.each(cases)("%s → %s tone with a known pill class", (status, tone) => {
    const derived = deliveryStatusTone(status);
    expect(derived).toBe(tone);
    expect(DELIVERY_TONE_PILL[derived]).toBeTruthy();
  });
});

describe("deliveryChip — no delivery", () => {
  it("returns null when there is no run at all", () => {
    expect(deliveryChip(input())).toBeNull();
  });

  it("returns null for a shared/copy run with no delivery", () => {
    expect(
      deliveryChip(
        input({ latest_run_state: "completed", latest_run_workspace_mode: "copy" })
      )
    ).toBeNull();
  });

  it("returns null while a git_worktree run is still running", () => {
    expect(
      deliveryChip(
        input({
          latest_run_state: "running",
          latest_run_workspace_mode: "git_worktree",
        })
      )
    ).toBeNull();
  });

  it("shows a neutral 'Not accepted' for a completed git_worktree run", () => {
    expect(
      deliveryChip(
        input({
          latest_run_state: "completed",
          latest_run_workspace_mode: "git_worktree",
        })
      )
    ).toEqual({ label: "Not accepted", tone: "neutral" });
  });
});

describe("deliveryChip — with delivery", () => {
  it("pending and preparing read as neutral 'Accepting…'", () => {
    for (const status of ["pending", "preparing"] as DeliveryStatus[]) {
      expect(deliveryChip(input({ delivery: summary({ status }) }))).toEqual({
        label: "Accepting…",
        tone: "neutral",
      });
    }
  });

  it("ready + dirty → attention 'Uncommitted' (dirty wins over commits ahead)", () => {
    expect(
      deliveryChip(
        input({ delivery: summary({ status: "ready", dirty: true, commits_ahead: 3 }) })
      )
    ).toEqual({ label: "Uncommitted", tone: "attention" });
  });

  it("ready + clean + commits ahead → attention 'Ready to push'", () => {
    expect(
      deliveryChip(
        input({ delivery: summary({ status: "ready", dirty: false, commits_ahead: 2 }) })
      )
    ).toEqual({ label: "Ready to push", tone: "attention" });
  });

  it("ready + clean + nothing ahead → neutral 'Accepted'", () => {
    expect(
      deliveryChip(
        input({ delivery: summary({ status: "ready", dirty: false, commits_ahead: 0 }) })
      )
    ).toEqual({ label: "Accepted", tone: "neutral" });
  });

  it("delivering → attention 'Delivering…'", () => {
    expect(deliveryChip(input({ delivery: summary({ status: "delivering" }) }))).toEqual({
      label: "Delivering…",
      tone: "attention",
    });
  });

  it("delivered + merge_strategy → success 'Merged' (merge wins over a PR)", () => {
    expect(
      deliveryChip(
        input({
          delivery: summary({
            status: "delivered",
            merge_strategy: "fast_forward_only",
            pr_number: 5,
            pushed_ref: "refs/heads/x",
          }),
        })
      )
    ).toEqual({ label: "Merged", tone: "success" });
  });

  it("delivered + PR → success 'PR #n · state'", () => {
    expect(
      deliveryChip(
        input({
          delivery: summary({ status: "delivered", pr_number: 42, pr_state: "open" }),
        })
      )
    ).toEqual({ label: "PR #42 · open", tone: "success" });
  });

  it("delivered + PR without a state omits the separator", () => {
    expect(
      deliveryChip(
        input({ delivery: summary({ status: "delivered", pr_number: 42, pr_state: null }) })
      )
    ).toEqual({ label: "PR #42", tone: "success" });
  });

  it("delivered via push only → success 'Pushed'", () => {
    expect(
      deliveryChip(
        input({ delivery: summary({ status: "delivered", pushed_ref: "refs/heads/x" }) })
      )
    ).toEqual({ label: "Pushed", tone: "success" });
  });

  it.each(["conflicted", "blocked", "failed"] as DeliveryStatus[])(
    "%s → destructive chip carrying the reason kind",
    (status) => {
      expect(
        deliveryChip(input({ delivery: summary({ status, reason_kind: "conflict" }) }))
      ).toEqual({ label: "conflict", tone: "destructive" });
    }
  );

  it("terminal failure without a reason falls back to the status word", () => {
    expect(
      deliveryChip(input({ delivery: summary({ status: "failed", reason_kind: null }) }))
    ).toEqual({ label: "failed", tone: "destructive" });
  });
});

describe("verdictBadge", () => {
  it("flags a done task with verdict=fail", () => {
    expect(verdictBadge({ status: "done", verdict: "fail" })).toEqual({
      label: "Review failed",
      tone: "destructive",
    });
  });

  it("is null for a done task with verdict=pass", () => {
    expect(verdictBadge({ status: "done", verdict: "pass" })).toBeNull();
  });

  it("is null for a done task with no verdict recorded (legacy tasks)", () => {
    expect(verdictBadge({ status: "done", verdict: null })).toBeNull();
  });

  it("is null for a non-done task even with verdict=fail set", () => {
    expect(verdictBadge({ status: "running", verdict: "fail" })).toBeNull();
  });
});

describe("isArchivableDoneTask", () => {
  it("is archivable when done with no delivery", () => {
    expect(isArchivableDoneTask({ status: "done", archived: false, delivery: null })).toBe(true);
  });

  it("is archivable when cancelled with no delivery (task-board-gaps.md §3.4)", () => {
    expect(isArchivableDoneTask({ status: "cancelled", archived: false, delivery: null })).toBe(true);
  });

  it("is NOT archivable for any other status", () => {
    expect(isArchivableDoneTask({ status: "blocked", archived: false, delivery: null })).toBe(false);
    expect(isArchivableDoneTask({ status: "triage", archived: false, delivery: null })).toBe(false);
  });

  it("is not archivable once already archived", () => {
    expect(isArchivableDoneTask({ status: "cancelled", archived: true, delivery: null })).toBe(false);
  });

  it("a cancelled task with an undelivered delivery is not archivable", () => {
    expect(
      isArchivableDoneTask({ status: "cancelled", archived: false, delivery: summary({ status: "failed" }) })
    ).toBe(false);
  });

  it("a cancelled task with a delivered delivery is archivable", () => {
    expect(
      isArchivableDoneTask({ status: "cancelled", archived: false, delivery: summary({ status: "delivered" }) })
    ).toBe(true);
  });
});

describe("deliveryButtonState", () => {
  function d(overrides: Partial<TaskDelivery> = {}) {
    return {
      status: "ready" as DeliveryStatus,
      dirty: false,
      commits_ahead: 0,
      pr_number: null,
      pushed_ref: "",
      ...overrides,
    };
  }

  it("before any delivery exists, only Accept is enabled — every other action explains it needs Accept first", () => {
    expect(deliveryButtonState("accept", null)).toEqual({ enabled: true, reason: null });
    for (const kind of ["commit", "push", "pull_request", "merge", "teardown"] as const) {
      const state = deliveryButtonState(kind, null);
      expect(state.enabled).toBe(false);
      expect(state.reason).toBeTruthy();
    }
  });

  it("accept is disabled with a reason once past pending", () => {
    const state = deliveryButtonState("accept", d({ status: "ready" }));
    expect(state).toEqual({ enabled: false, reason: "Already accepted" });
  });

  it("commit requires ready + dirty, and explains which precondition is missing", () => {
    expect(deliveryButtonState("commit", d({ status: "ready", dirty: true }))).toEqual({
      enabled: true,
      reason: null,
    });
    expect(deliveryButtonState("commit", d({ status: "ready", dirty: false })).reason).toMatch(/nothing to commit/i);
    expect(deliveryButtonState("commit", d({ status: "pending", dirty: true })).reason).toBeTruthy();
  });

  it("push requires ready + clean + commits ahead, each with its own reason", () => {
    expect(
      deliveryButtonState("push", d({ status: "ready", dirty: false, commits_ahead: 2 }))
    ).toEqual({ enabled: true, reason: null });
    expect(
      deliveryButtonState("push", d({ status: "ready", dirty: true, commits_ahead: 2 })).reason
    ).toMatch(/commit the pending changes/i);
    expect(
      deliveryButtonState("push", d({ status: "ready", dirty: false, commits_ahead: 0 })).reason
    ).toMatch(/nothing to push/i);
  });

  it("pull_request explains a missing push, and a pre-existing PR by name", () => {
    expect(deliveryButtonState("pull_request", d({ pushed_ref: "" })).reason).toMatch(/push the branch/i);
    expect(
      deliveryButtonState("pull_request", d({ pushed_ref: "refs/heads/x", pr_number: 7 })).reason
    ).toMatch(/already open/i);
    expect(
      deliveryButtonState("pull_request", d({ pushed_ref: "refs/heads/x", pr_number: null }))
    ).toEqual({ enabled: true, reason: null });
  });

  it("merge explains a missing PR, and that a delivered PR must merge on the platform", () => {
    expect(deliveryButtonState("merge", d({ pr_number: null })).reason).toMatch(/open a pull request/i);
    expect(
      deliveryButtonState("merge", d({ pr_number: 7, status: "delivered" })).reason
    ).toMatch(/merge on the platform/i);
    expect(deliveryButtonState("merge", d({ pr_number: 7, status: "ready" }))).toEqual({
      enabled: true,
      reason: null,
    });
  });

  it("teardown requires a terminal status", () => {
    expect(deliveryButtonState("teardown", d({ status: "ready" })).reason).toMatch(/terminal state/i);
    for (const status of ["delivered", "failed", "blocked", "conflicted"] as DeliveryStatus[]) {
      expect(deliveryButtonState("teardown", d({ status }))).toEqual({ enabled: true, reason: null });
    }
  });
});
