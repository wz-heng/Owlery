import { describe, expect, it } from "vitest";
import { budgetGauge, fmtUsd } from "./budget";

describe("budgetGauge", () => {
  it("classifies below the soft percentage as ok", () => {
    const g = budgetGauge(2, 10, 0.8);
    expect(g.level).toBe("ok");
    expect(g.fraction).toBeCloseTo(0.2);
    expect(g.ratio).toBeCloseTo(0.2);
  });

  it("classifies at-or-above the soft percentage (but below limit) as soft", () => {
    expect(budgetGauge(8, 10, 0.8).level).toBe("soft");
    expect(budgetGauge(9.99, 10, 0.8).level).toBe("soft");
  });

  it("classifies at-or-above the limit as hard, clamping the bar to full", () => {
    const g = budgetGauge(15, 10, 0.8);
    expect(g.level).toBe("hard");
    expect(g.fraction).toBe(1); // clamped for the bar
    expect(g.ratio).toBeCloseTo(1.5); // raw ratio preserved
  });

  it("treats exactly at the limit as hard (matches the gate's >=)", () => {
    expect(budgetGauge(10, 10, 0.8).level).toBe("hard");
  });

  it("degrades a non-positive limit to a full hard bar rather than dividing by zero", () => {
    const g = budgetGauge(1, 0, 0.8);
    expect(g.level).toBe("hard");
    expect(g.fraction).toBe(1);
    expect(Number.isFinite(g.ratio)).toBe(false);
  });
});

describe("fmtUsd", () => {
  it("formats to four decimals with a dollar sign", () => {
    expect(fmtUsd(1.5)).toBe("$1.5000");
    expect(fmtUsd(0.0001)).toBe("$0.0001");
  });
});
