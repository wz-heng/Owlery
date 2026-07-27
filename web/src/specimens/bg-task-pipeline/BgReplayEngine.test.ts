import { describe, expect, it } from "vitest";

import { BgReplayEngine } from "./BgReplayEngine";
import type { BgSpecimenEvent } from "./scripts";

const event = (type: BgSpecimenEvent["type"]): BgSpecimenEvent => ({ type, actor: "Manager" });

describe("BgReplayEngine", () => {
  it("advances once per event and stops at the script boundary", () => {
    const engine = new BgReplayEngine([event("bg_started"), event("bg_completed")]);
    expect(engine.step()?.index).toBe(0);
    expect(engine.step()?.event.type).toBe("bg_completed");
    expect(engine.done).toBe(true);
    expect(engine.step()).toBeNull();
  });

  it("resets cleanly into a different terminal path", () => {
    const engine = new BgReplayEngine([event("followup_reply")]);
    engine.step();
    engine.reset([event("cancel_requested"), event("result_injected")]);
    expect(engine.position).toBe(0);
    expect(engine.length).toBe(2);
    expect(engine.step()?.event.type).toBe("cancel_requested");
  });
});
