import { describe, expect, it, vi } from "vitest";

import type { WsEventApplication } from "../../hooks/useWebSocket";
import { ReplayEngine } from "./ReplayEngine";
import type { SpecimenEvent } from "./scripts";

const event = (type: string, seq: number): SpecimenEvent => ({
  type,
  seq,
  session_id: "test-session",
});

const applied = (input: Record<string, unknown>): WsEventApplication => ({
  status: "applied",
  eventType: input.type as string,
  sessionId: input.session_id as string,
  seq: input.seq as number,
  baseline: null,
  reason: "event_applied",
});

describe("ReplayEngine", () => {
  it("advances once per step and stops at the script boundary", () => {
    const apply = vi.fn(applied);
    const engine = new ReplayEngine(
      [event("user_message", 1), event("assistant_text", 2)],
      apply
    );

    expect(engine.position).toBe(0);
    expect(engine.step()?.event.type).toBe("user_message");
    expect(engine.position).toBe(1);
    expect(engine.step()?.event.type).toBe("assistant_text");
    expect(engine.done).toBe(true);
    expect(engine.step()).toBeNull();
    expect(apply).toHaveBeenCalledTimes(2);
  });

  it("accepts a user-provided override while preserving script order", () => {
    const apply = vi.fn(applied);
    const engine = new ReplayEngine([event("question_answer", 4)], apply);
    const override = {
      ...event("question_answer", 4),
      content: "公开预览",
    };

    expect(engine.step(override)?.event.content).toBe("公开预览");
    expect(engine.done).toBe(true);
    expect(apply).toHaveBeenCalledWith(override);
  });

  it("can be reset with a different scenario", () => {
    const engine = new ReplayEngine([event("status", 1)], applied);
    engine.step();
    engine.reset([event("error", 9), event("limit_resumed", 10)]);

    expect(engine.position).toBe(0);
    expect(engine.length).toBe(2);
    expect(engine.peek()?.type).toBe("error");
  });
});
