import {
  applyWsEvent,
  type WsEventApplication,
} from "../../hooks/useWebSocket";
import type { SpecimenEvent } from "./scripts";

export interface ReplayStep {
  index: number;
  event: SpecimenEvent;
  outcome: WsEventApplication;
}

type ApplyEvent = (event: Record<string, unknown>) => WsEventApplication;

/** A deliberately clock-free replay cursor.
 *
 * React owns scheduling so pause/speed controls remain ordinary UI state;
 * this class owns the invariant that every step advances exactly once through
 * the same event applicator used by the live WebSocket.
 */
export class ReplayEngine {
  private cursor = 0;
  private events: SpecimenEvent[];
  private readonly apply: ApplyEvent;

  constructor(
    events: SpecimenEvent[],
    apply: ApplyEvent = applyWsEvent
  ) {
    this.events = events;
    this.apply = apply;
  }

  get position(): number {
    return this.cursor;
  }

  get length(): number {
    return this.events.length;
  }

  get done(): boolean {
    return this.cursor >= this.events.length;
  }

  reset(events = this.events): void {
    this.events = events;
    this.cursor = 0;
  }

  peek(): SpecimenEvent | null {
    return this.events[this.cursor] ?? null;
  }

  step(eventOverride?: SpecimenEvent): ReplayStep | null {
    const scripted = this.events[this.cursor];
    if (!scripted) return null;
    const event = eventOverride ?? scripted;
    const index = this.cursor;
    this.cursor += 1;
    return { index, event, outcome: this.apply(event) };
  }
}
