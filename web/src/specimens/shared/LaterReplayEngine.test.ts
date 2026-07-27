import { describe, expect, it } from "vitest";
import { LaterReplayEngine } from "./LaterReplayEngine";

describe("LaterReplayEngine", () => {
  const frames = [
    { label: "one", actor: "a", note: "", state: { phase: 1 }, event: { type: "one" } },
    { label: "two", actor: "b", note: "", state: { phase: 2 }, event: { type: "two" } },
  ];

  it("steps deterministically and stops at the end", () => {
    const engine = new LaterReplayEngine(frames);
    expect(engine.step()?.label).toBe("one");
    expect((engine.step()?.state as { phase: number }).phase).toBe(2);
    expect(engine.step()).toBeNull();
    expect(engine.done).toBe(true);
  });

  it("can reset with another script", () => {
    const engine = new LaterReplayEngine(frames);
    engine.step();
    engine.reset([frames[1]!]);
    expect(engine.position).toBe(0);
    expect(engine.step()?.label).toBe("two");
  });
});
