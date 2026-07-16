/**
 * Messenger Form — round 3 stage 1 logo-mark candidates.
 *
 * SAMPLE ROOM ONLY — the product still renders `OwleryLogo.tsx`
 * untouched. One mark per shape grammar, because the mark and the
 * grammar answer the same question ("what is Owlery's geometric
 * motif"), so they get picked together (plan §3).
 *
 * All three share one owl face — two eye rings + a V beak — carried as
 * negative space. What differs is the *frame*, and the frame is the
 * grammar: a scalloped seal (A), a dog-eared sheet (B), a perforated
 * stamp (C). Each is drawn on a 32-unit grid, single `currentColor`
 * (A and C are one flat fill; B spends one extra tone on the flap —
 * see the sheet's tradeoff note), so they behave like the existing
 * mark: recolorable, hole-punched, correct on any surface.
 */

import type { ReactNode } from "react";

/** Alternating-radius rim: `lobes` bulges, quadratic control points
 * pushed past the outer radius so each lobe reads as a wax bead. */
export function scallopPath(
  cx: number,
  cy: number,
  rIn: number,
  rOut: number,
  lobes: number
) {
  const step = (Math.PI * 2) / lobes;
  const pt = (a: number, r: number) => [cx + Math.cos(a) * r, cy + Math.sin(a) * r] as const;
  let d = "";
  for (let i = 0; i < lobes; i++) {
    const a0 = i * step;
    const [x0, y0] = pt(a0, rIn);
    const [cxp, cyp] = pt(a0 + step / 2, rOut);
    const [x1, y1] = pt(a0 + step, rIn);
    if (i === 0) d += `M${x0.toFixed(2)} ${y0.toFixed(2)}`;
    d += `Q${cxp.toFixed(2)} ${cyp.toFixed(2)} ${x1.toFixed(2)} ${y1.toFixed(2)}`;
  }
  return `${d}Z`;
}

/** The shared owl face, as sub-paths meant to be punched out of a
 * frame with `fill-rule: evenodd`. `s` scales it inside the frame. */
function owlFacePaths(cx: number, cy: number, s: number) {
  const eyeR = 3.15 * s;
  const eyeDx = 4.25 * s;
  const eyeY = cy - 1.2 * s;
  const circle = (x: number, y: number, r: number) =>
    `M${(x - r).toFixed(2)} ${y.toFixed(2)}a${r.toFixed(2)} ${r.toFixed(2)} 0 1 0 ${(r * 2).toFixed(2)} 0a${r.toFixed(2)} ${r.toFixed(2)} 0 1 0 ${(-r * 2).toFixed(2)} 0Z`;
  const beakHalf = 1.85 * s;
  const beakTop = cy + 1.7 * s;
  const beakTip = cy + 6.0 * s;
  return [
    circle(cx - eyeDx, eyeY, eyeR),
    circle(cx + eyeDx, eyeY, eyeR),
    `M${(cx - beakHalf).toFixed(2)} ${beakTop.toFixed(2)}L${(cx + beakHalf).toFixed(2)} ${beakTop.toFixed(2)}L${cx.toFixed(2)} ${beakTip.toFixed(2)}Z`,
  ].join("");
}

export interface MarkProps {
  size?: number;
  className?: string;
}

function frame(size: number, className: string | undefined, children: ReactNode) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 32 32"
      width={size}
      height={size}
      fill="currentColor"
      className={className}
      aria-hidden
    >
      {children}
    </svg>
  );
}

/** Mark A — the wax seal. Scalloped disc, face impressed into it. */
export function MarkSeal({ size = 32, className }: MarkProps) {
  return frame(
    size,
    className,
    <path
      fillRule="evenodd"
      clipRule="evenodd"
      d={scallopPath(16, 16, 13.6, 16.6, 13) + owlFacePaths(16, 15.4, 1.06)}
    />
  );
}

/** Mark B — the folded sheet. Square, one corner dog-eared; the flap
 * is the one place the mark spends a second tone. */
export function MarkFold({ size = 32, className }: MarkProps) {
  const f = 11;
  const r = 4.5;
  const x0 = 1.5;
  const y0 = 1.5;
  const x1 = 30.5;
  const y1 = 30.5;
  const body =
    `M${x0 + r} ${y0}` +
    `L${x1 - f} ${y0}` +
    `L${x1} ${y0 + f}` +
    `L${x1} ${y1 - r}` +
    `A${r} ${r} 0 0 1 ${x1 - r} ${y1}` +
    `L${x0 + r} ${y1}` +
    `A${r} ${r} 0 0 1 ${x0} ${y1 - r}` +
    `L${x0} ${y0 + r}` +
    `A${r} ${r} 0 0 1 ${x0 + r} ${y0}Z`;
  // The corner point reflected across the fold line — the flap lying
  // face-down on the sheet.
  const flap = `M${x1 - f} ${y0}L${x1} ${y0 + f}L${x1 - f} ${y0 + f}Z`;
  return frame(
    size,
    className,
    <>
      <path fillRule="evenodd" clipRule="evenodd" d={body + owlFacePaths(15.5, 17.4, 1.02)} />
      <path d={flap} opacity="0.42" />
    </>
  );
}

/** Mark C — the stamp. Perforated square; teeth and face punched by
 * one mask so the whole thing stays a single flat fill. */
export function MarkStamp({ size = 32, className }: MarkProps) {
  const teeth: ReactNode[] = [];
  const pitch = 5.8;
  const rT = 1.9;
  const lo = 2.2;
  const hi = 29.8;
  for (let i = 0; i < 5; i++) {
    const p = lo + pitch * (i + 0.5);
    teeth.push(<circle key={`t${i}`} cx={p} cy={lo} r={rT} fill="black" />);
    teeth.push(<circle key={`b${i}`} cx={p} cy={hi} r={rT} fill="black" />);
    teeth.push(<circle key={`l${i}`} cx={lo} cy={p} r={rT} fill="black" />);
    teeth.push(<circle key={`r${i}`} cx={hi} cy={p} r={rT} fill="black" />);
  }
  return frame(
    size,
    className,
    <>
      <defs>
        <mask id="owlery-mark-stamp">
          <rect x={lo} y={lo} width={hi - lo} height={hi - lo} rx="2" fill="white" />
          {teeth}
          <path d={owlFacePaths(16, 15.6, 1.0)} fill="black" fillRule="evenodd" />
        </mask>
      </defs>
      <rect x="0" y="0" width="32" height="32" mask="url(#owlery-mark-stamp)" />
    </>
  );
}

export const MARKS = {
  seal: MarkSeal,
  fold: MarkFold,
  stamp: MarkStamp,
} as const;
