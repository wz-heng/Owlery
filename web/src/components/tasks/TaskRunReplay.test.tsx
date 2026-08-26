import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ReplayTimeline } from "../../api/tasks";
import { resetTaskStore, useTaskStore } from "../../stores/taskStore";
import { TaskRunReplay } from "./TaskRunReplay";

beforeEach(() => {
  resetTaskStore();
  vi.restoreAllMocks();
});

afterEach(cleanup);

function seed(replay: ReplayTimeline): void {
  useTaskStore.setState({ token: "", replays: { "run-1": replay } });
}

describe("TaskRunReplay", () => {
  it("shows a loading state while the timeline is not yet fetched", () => {
    useTaskStore.setState({ token: "", loadingReplay: { "run-1": true } });
    render(<TaskRunReplay taskId="task-1" runId="run-1" />);
    expect(screen.getByText(/Loading timeline/)).toBeInTheDocument();
  });

  it("renders the timeline's black-hole gap block with duration and bracketing events", () => {
    seed({
      session_id: "session-1",
      task_run: { task_id: "task-1", run_id: "run-1" },
      gap_threshold_seconds: 300,
      unobserved_prefix: null,
      timeline: [
        { kind: "message", ts: "2026-08-01T00:00:00Z", seq: 0, summary: "user: go", detail: {} },
        {
          kind: "gap",
          ts: null,
          seq: null,
          summary: "9000s of silence",
          detail: {
            duration_seconds: 9000,
            before: { kind: "message", ts: "2026-08-01T00:00:00Z", seq: 0, summary: "user: go", detail: {} },
            after: { kind: "turn_terminal", ts: "2026-08-01T02:30:00Z", seq: 1, summary: "turn ended: process_error", detail: {} },
          },
        },
        {
          kind: "turn_terminal",
          ts: "2026-08-01T02:30:00Z",
          seq: 1,
          summary: "turn ended: process_error exit_code=None signal=9",
          detail: { reason: "process_error", exit_code: null, signal: 9, escalation: null, reason_detail: {}, stderr_tail: "boom" },
        },
      ],
    });

    render(<TaskRunReplay taskId="task-1" runId="run-1" />);

    expect(screen.getByText(/2h 30m of silence/)).toBeInTheDocument();
    // Appears twice: once as the gap's "after" preview, once as the actual
    // terminal row below it.
    expect(screen.getAllByText(/turn ended: process_error/).length).toBe(2);
    // Abnormal terminal rows start expanded — the stderr tail is visible without a click.
    expect(screen.getByText("boom")).toBeInTheDocument();
  });

  it("lets the reader open a delegation's child session", () => {
    seed({
      session_id: "session-1",
      task_run: null,
      gap_threshold_seconds: 300,
      unobserved_prefix: null,
      timeline: [
        {
          kind: "delegation",
          ts: "2026-08-01T00:00:00Z",
          seq: 0,
          summary: "delegated to Snape: review this",
          detail: { delegation_id: "child-session", request: "review this" },
        },
      ],
    });
    const onOpenSession = vi.fn();
    render(<TaskRunReplay taskId="task-1" runId="run-1" onOpenSession={onOpenSession} />);

    fireEvent.click(screen.getByRole("button", { name: /Open/ }));
    expect(onOpenSession).toHaveBeenCalledWith("child-session");
  });

  it("flags an unobserved prefix distinctly from the timed timeline", () => {
    seed({
      session_id: "session-1",
      task_run: null,
      gap_threshold_seconds: 300,
      unobserved_prefix: {
        summary: "1 message(s) recorded before timestamps were tracked",
        events: [{ kind: "message", ts: null, seq: 0, summary: "user: legacy", detail: {} }],
      },
      timeline: [
        { kind: "message", ts: "2026-08-01T00:00:00Z", seq: 1, summary: "user: new", detail: {} },
      ],
    });
    render(<TaskRunReplay taskId="task-1" runId="run-1" />);
    expect(screen.getByText(/recorded before timestamps were tracked/)).toBeInTheDocument();
  });
});
