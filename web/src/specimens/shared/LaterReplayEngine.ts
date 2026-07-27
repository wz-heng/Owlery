export interface ReplayFrame {
  label: string;
  actor: string;
  note: string;
  state: object;
  event: Record<string, unknown>;
}

export class LaterReplayEngine {
  private cursor = 0;
  private frames: ReplayFrame[];

  constructor(frames: ReplayFrame[]) { this.frames = frames; }
  get position(): number { return this.cursor; }
  get length(): number { return this.frames.length; }
  get done(): boolean { return this.cursor >= this.frames.length; }
  reset(frames?: ReplayFrame[]): void {
    if (frames) this.frames = frames;
    this.cursor = 0;
  }
  step(): ReplayFrame | null {
    const frame = this.frames[this.cursor];
    if (!frame) return null;
    this.cursor += 1;
    return frame;
  }
}
