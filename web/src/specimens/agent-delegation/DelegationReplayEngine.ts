import type { DelegationSpecimenEvent } from "./scripts";

export interface DelegationReplayStep {
  index: number;
  event: DelegationSpecimenEvent;
}

export class DelegationReplayEngine {
  private cursor = 0;
  private events: DelegationSpecimenEvent[];

  constructor(events: DelegationSpecimenEvent[]) {
    this.events = events;
  }

  get position(): number { return this.cursor; }
  get length(): number { return this.events.length; }
  get done(): boolean { return this.cursor >= this.events.length; }

  reset(events = this.events): void {
    this.events = events;
    this.cursor = 0;
  }

  step(): DelegationReplayStep | null {
    const event = this.events[this.cursor];
    if (!event) return null;
    const step = { index: this.cursor, event };
    this.cursor += 1;
    return step;
  }
}
