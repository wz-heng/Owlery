/**
 * The geometry of the Owlery seal — the single motif of the round-3
 * "Seal & Letterhead" shape grammar (`docs/plans/messenger-form.md`).
 *
 * One rim, generated once, used by everything: the <Seal> component, the
 * <OwleryLogo> mark, the favicon, and (as a mask data-URI baked into
 * `--seal-rim`) every CSS-only dot on controls and sidebar rows. Change
 * the numbers here and `SEAL_RIM_URI` below regenerates for JS callers —
 * but `--seal-rim` in tokens.css is a *pasted* copy, so update it too.
 * It is pasted rather than imported because a CSS custom property cannot
 * call a function, and one duplicated string beats a runtime style
 * injection on every page load.
 */

/** 13 lobes: prime, so no lobe ever lines up with a horizontal or
 * vertical edge and the disc never reads as a cog. */
export const SEAL_LOBES = 13;
export const SEAL_R_IN = 13.4;
export const SEAL_R_OUT = 16.4;

/**
 * A scalloped rim on a 32-unit grid: anchors at `rIn`, quadratic control
 * points at `rOut` on the half-angles, so each lobe bulges like a bead of
 * pressed wax. The curve's true extent is ~14.7 units (a quadratic reaches
 * only halfway to its control point), so the path stays inside the box even
 * though the control points sit outside it.
 */
export function scallopPath(
  cx: number,
  cy: number,
  rIn: number,
  rOut: number,
  lobes: number,
  precision = 2
): string {
  const step = (Math.PI * 2) / lobes;
  const pt = (a: number, r: number) =>
    [cx + Math.cos(a) * r, cy + Math.sin(a) * r] as const;
  const n = (v: number) => v.toFixed(precision);
  let d = "";
  for (let i = 0; i < lobes; i++) {
    const a0 = i * step;
    const [x0, y0] = pt(a0, rIn);
    const [cxp, cyp] = pt(a0 + step / 2, rOut);
    const [x1, y1] = pt(a0 + step, rIn);
    if (i === 0) d += `M${n(x0)} ${n(y0)}`;
    d += `Q${n(cxp)} ${n(cyp)} ${n(x1)} ${n(y1)}`;
  }
  return `${d}Z`;
}

/** The canonical rim, centred on the 32-grid. */
export const SEAL_RIM_PATH = scallopPath(
  16,
  16,
  SEAL_R_IN,
  SEAL_R_OUT,
  SEAL_LOBES
);

/** The inner impression ring — the step a real seal's matrix leaves
 * inside the rim. Drawn as a stroke, not a fill. */
export const SEAL_RING_PATH = scallopPath(16, 16, 10.2, 12.2, SEAL_LOBES);

/**
 * The owl face, as sub-paths meant to be punched out of the rim with
 * `fill-rule: evenodd` — two eyes and a beak, nothing else. It is the
 * *impression*, so it is always a hole, never a painted shape: that is
 * what keeps the mark correct on parchment, on brass, and on ink alike.
 */
export function owlFacePaths(cx: number, cy: number, s: number): string {
  const eyeR = 3.15 * s;
  const eyeDx = 4.25 * s;
  const eyeY = cy - 1.2 * s;
  const n = (v: number) => v.toFixed(2);
  const circle = (x: number, y: number, r: number) =>
    `M${n(x - r)} ${n(y)}a${n(r)} ${n(r)} 0 1 0 ${n(r * 2)} 0a${n(r)} ${n(
      r
    )} 0 1 0 ${n(-r * 2)} 0Z`;
  const beakHalf = 1.85 * s;
  const beakTop = cy + 1.7 * s;
  const beakTip = cy + 6.0 * s;
  return [
    circle(cx - eyeDx, eyeY, eyeR),
    circle(cx + eyeDx, eyeY, eyeR),
    `M${n(cx - beakHalf)} ${n(beakTop)}L${n(cx + beakHalf)} ${n(
      beakTop
    )}L${n(cx)} ${n(beakTip)}Z`,
  ].join("");
}

/** The full mark: rim with the owl impressed into it, one evenodd path. */
export const SEAL_MARK_PATH = SEAL_RIM_PATH + owlFacePaths(16, 15.4, 1.06);

/**
 * Wax colours — the second half of an agent's identity (the monogram is the
 * first). Defined as HSL triplets in `tokens.css` (`--wax-*`, every one
 * WCAG-AA against a white monogram); this array is the order the id hash
 * indexes into. Red is deliberately absent: it stays reserved for the
 * `destructive` state and is never an identity colour.
 */
export const WAX_TONES = [
  "plum",
  "indigo",
  "teal",
  "bronze",
  "forest",
  "slate",
] as const;

export type WaxTone = (typeof WAX_TONES)[number];

/**
 * The wax an agent is sealed in — deterministic from its id, so the same
 * agent is always the same colour and the palette stays globally balanced
 * without a per-agent picker. A small FNV-1a hash over the id keeps the
 * distribution stable across reloads and machines (no `Math.random`, no
 * dependence on insertion order).
 */
export function waxToneForId(id: string): WaxTone {
  let h = 0x811c9dc5;
  for (let i = 0; i < id.length; i++) {
    h ^= id.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return WAX_TONES[(h >>> 0) % WAX_TONES.length];
}

/** An agent's wax as a ready-to-use CSS colour (for `color` / `fill`). */
export function waxColorForId(id: string): string {
  return `hsl(var(--wax-${waxToneForId(id)}))`;
}
