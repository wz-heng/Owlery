import { describe, expect, it } from "vitest";

import { DelegationReplayEngine } from "./DelegationReplayEngine";
import type { DelegationSpecimenEvent } from "./scripts";

const event = (type: DelegationSpecimenEvent["type"]): DelegationSpecimenEvent => ({
  type,
  actor: "Owlery",
});

describe("DelegationReplayEngine", () => {
  it("advances exactly once per step and stops at the boundary", () => {
    const engine = new DelegationReplayEngine([
      event("delegation_started"),
      event("child_reply"),
    ]);

    expect(engine.position).toBe(0);
    expect(engine.step()?.index).toBe(0);
    expect(engine.step()?.event.type).toBe("child_reply");
    expect(engine.done).toBe(true);
    expect(engine.step()).toBeNull();
  });

  it("can switch scenarios without retaining the previous cursor", () => {
    const engine = new DelegationReplayEngine([event("child_running")]);
    engine.step();
    engine.reset([event("nested_started"), event("nested_reply")]);

    expect(engine.position).toBe(0);
    expect(engine.length).toBe(2);
    expect(engine.step()?.event.type).toBe("nested_started");
  });
});
