import { describe, expect, it } from "vitest";

import type {
  DeliveryStatus,
  Task,
  TaskDeliverySummary,
} from "../../api/tasks";
import {
  DELIVERY_TONE_PILL,
  deliveryChip,
  deliveryStatusTone,
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
