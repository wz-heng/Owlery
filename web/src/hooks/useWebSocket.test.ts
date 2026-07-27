import { beforeEach, describe, expect, it } from "vitest";

import { useSessionStore } from "../stores/sessionStore";
import {
  applyWsEvent,
  parkedTurnFromSnapshot,
  shouldApplyWsEvent,
} from "./useWebSocket";

/** Snapshot-baseline dedup primitive.
 *
 * The full handler is hard to unit-test cleanly because it touches the
 * zustand store + WebSocket lifecycle, but the guard inside it — "is
 * this event's seq already covered by the snapshot we just loaded?" —
 * is the whole bug-fix and is a tiny pure function. Testing it
 * directly is the strongest signal that the dedup works for the race
 * window the seq mechanism is designed to cover.
 */
describe("shouldApplyWsEvent", () => {
  it("applies events without seq (ephemeral status/queue updates)", () => {
    expect(shouldApplyWsEvent(undefined, 5)).toBe(true);
    expect(shouldApplyWsEvent(null, 5)).toBe(true);
  });

  it("applies events when no baseline is set yet (fresh session)", () => {
    expect(shouldApplyWsEvent(0, undefined)).toBe(true);
    expect(shouldApplyWsEvent(7, undefined)).toBe(true);
  });

  it("applies events with seq strictly greater than baseline", () => {
    expect(shouldApplyWsEvent(6, 5)).toBe(true);
    expect(shouldApplyWsEvent(100, 99)).toBe(true);
  });

  it("drops events with seq <= baseline (already in snapshot)", () => {
    expect(shouldApplyWsEvent(5, 5)).toBe(false);
    expect(shouldApplyWsEvent(0, 5)).toBe(false);
    expect(shouldApplyWsEvent(99, 100)).toBe(false);
  });

  it("treats baseline=0 distinctly from baseline=undefined", () => {
    // baseline=0 means "seq 0 is in the snapshot, but seq 1+ are not"
    expect(shouldApplyWsEvent(0, 0)).toBe(false);
    expect(shouldApplyWsEvent(1, 0)).toBe(true);
  });
});

/** Snapshot → park-banner restore.
 *
 * The park banner is otherwise only set by a live `limit_paused` WS event, so
 * a reload/reconnect would drop it. Both restore paths (reconnect + session
 * select) map the snapshot's `pending_park` through this pure helper, so
 * testing it directly is the strongest signal the paused state survives a
 * refresh (limit-auto-resume.md §4).
 */
describe("parkedTurnFromSnapshot", () => {
  it("returns null when the session isn't parked", () => {
    expect(parkedTurnFromSnapshot(null)).toBeNull();
    expect(parkedTurnFromSnapshot(undefined)).toBeNull();
  });

  it("maps a pending park to the store's ParkedTurn shape", () => {
    expect(
      parkedTurnFromSnapshot({
        resume_at: "2026-07-16T07:00:00+00:00",
        limit_kind: "five_hour",
      })
    ).toEqual({
      resumeAt: "2026-07-16T07:00:00+00:00",
      limitKind: "five_hour",
    });
  });

  it("still restores the banner for an epoch-less probe park", () => {
    // No reset epoch (probe fallback) — the banner must still show, just
    // without an "at HH:MM".
    expect(parkedTurnFromSnapshot({ resume_at: null })).toEqual({
      resumeAt: null,
      limitKind: null,
    });
  });
});

describe("applyWsEvent", () => {
  beforeEach(() => {
    useSessionStore.setState({
      messages: {},
      lastAppliedSeq: {},
      pendingQuestions: {},
      pendingQueue: {},
      parkedTurns: {},
      bgTasks: {},
      research: {},
    });
  });

  it("normalizes a persisted assistant event into the real message store", () => {
    const outcome = applyWsEvent({
      type: "assistant_text",
      session_id: "specimen",
      seq: 4,
      content: "streamed answer",
    });

    expect(outcome).toMatchObject({
      status: "applied",
      eventType: "assistant_text",
      seq: 4,
      baseline: null,
    });
    expect(useSessionStore.getState().messages.specimen).toEqual([
      { role: "assistant", type: "text", content: "streamed answer" },
    ]);
    expect(useSessionStore.getState().lastAppliedSeq.specimen).toBe(4);
  });

  it("reports and drops a duplicate without mutating the transcript", () => {
    applyWsEvent({
      type: "assistant_text",
      session_id: "specimen",
      seq: 8,
      content: "first delivery",
    });
    const duplicate = applyWsEvent({
      type: "assistant_text",
      session_id: "specimen",
      seq: 8,
      content: "duplicate delivery",
    });

    expect(duplicate).toMatchObject({
      status: "dropped",
      reason: "duplicate_seq",
      baseline: 8,
    });
    expect(useSessionStore.getState().messages.specimen).toHaveLength(1);
  });

  it("makes unknown event types observable without changing state", () => {
    const outcome = applyWsEvent({
      type: "future_event",
      session_id: "specimen",
    });

    expect(outcome.status).toBe("ignored");
    expect(outcome.reason).toBe("unknown_event");
    expect(useSessionStore.getState().messages.specimen).toBeUndefined();
  });
});
