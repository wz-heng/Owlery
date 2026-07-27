import type { BgSpecimenEvent } from "./scripts";

export interface BgReplayStep { index: number; event: BgSpecimenEvent }

export class BgReplayEngine {
  private cursor = 0;
  private events: BgSpecimenEvent[];

  constructor(events: BgSpecimenEvent[]) { this.events = events; }
  get position(): number { return this.cursor; }
  get length(): number { return this.events.length; }
  get done(): boolean { return this.cursor >= this.events.length; }
  reset(events = this.events): void { this.events = events; this.cursor = 0; }
  step(): BgReplayStep | null {
    const event = this.events[this.cursor];
    if (!event) return null;
    return { index: this.cursor++, event };
  }
}
