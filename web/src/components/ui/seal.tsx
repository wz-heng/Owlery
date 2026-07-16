/**
 * The seal — the one motif of the round-3 shape grammar, and the app's
 * single avatar/status primitive (`docs/plans/messenger-form.md` §4).
 *
 * A seal is *impressed*, not stuck on. It carries a monogram or a kind
 * icon; it never carries an emoji. Emoji avatars still identify agents in
 * the sidebar lists, where nothing is being sealed — but a chat turn is a
 * letter, and letters get wax.
 *
 * Tones map onto the round-1 state colours and add nothing: `attention`
 * is still plum, `destructive` is still the wax red it was always
 * reserved for. The grammar changes form, never state-colour semantics.
 */
import type { ReactNode } from "react";

import { SEAL_MARK_PATH, SEAL_RIM_PATH, SEAL_RING_PATH } from "../../lib/seal";
import { cn } from "../../lib/utils";

export type SealTone =
  | "brand"
  | "ink"
  | "attention"
  | "success"
  | "destructive";

/** Wax colours. Each is an existing token — no new colour is minted. */
const TONE_WAX: Record<SealTone, string> = {
  brand: "hsl(var(--primary-700))",
  ink: "hsl(var(--ink-800))",
  attention: "hsl(var(--attention))",
  success: "hsl(var(--success))",
  destructive: "hsl(var(--destructive))",
};

export type SealScale = "sheet" | "dialog" | "chip";

const SCALE_VAR: Record<SealScale, string> = {
  sheet: "var(--seal-sheet)",
  dialog: "var(--seal-dialog)",
  chip: "var(--seal-chip)",
};

export interface SealProps {
  /** Which edge the seal hangs from — the *author's* side. The only
   * asymmetry the grammar permits. */
  side: "left" | "right";
  tone?: SealTone;
  scale?: SealScale;
  /** A monogram (1 char) or a kind icon. Never an emoji. */
  children?: ReactNode;
  className?: string;
  /** Subdued: the same seal, pressed in a lighter wax. Used for
   * system-injected turns, which are ours but not *spoken*. */
  subdued?: boolean;
  /** Straddle the sheet's top edge (the default, and the grammar's rule
   * for anything sheet-sized). Chips set this false: at 16px the seal
   * sits inline in the header row, because a chip has no top edge worth
   * breaking. */
  straddle?: boolean;
  /** Impress the owl itself into the wax instead of taking a glyph. This
   * is the Owlery mark, so it is reserved for the app speaking in its own
   * voice — dialogs — and never used to stand in for an agent. */
  mark?: boolean;
}

export function Seal({
  side,
  tone = "brand",
  scale = "sheet",
  children,
  className,
  subdued = false,
  straddle = true,
  mark = false,
}: SealProps) {
  return (
    <span
      className={cn(
        "seal",
        straddle && "seal-straddle",
        subdued && "seal-subdued",
        className
      )}
      data-side={side}
      style={{ "--seal-size": SCALE_VAR[scale] } as React.CSSProperties}
      aria-hidden
    >
      <svg
        viewBox="0 0 32 32"
        className="seal-rim"
        style={{ color: TONE_WAX[tone] }}
      >
        <path
          d={mark ? SEAL_MARK_PATH : SEAL_RIM_PATH}
          fill="currentColor"
          fillRule="evenodd"
          clipRule="evenodd"
        />
        {/* The matrix step. Invisible at chip scale, which is correct —
         * a small seal is a blob of wax, not a detailed impression. The
         * mark variant skips it: the owl is already the impression. */}
        {!mark && (
          <path
            d={SEAL_RING_PATH}
            fill="none"
            stroke="hsl(var(--ink-0) / 0.4)"
            strokeWidth="1.1"
          />
        )}
      </svg>
      {children != null && <span className="seal-glyph">{children}</span>}
    </span>
  );
}

/** First letter of a name, as a monogram. Falls back to the owl when the
 * name has no letter to give (an emoji-only agent name, say) — the app's
 * own mark is always a legitimate impression. */
export function monogram(name: string | undefined | null): string {
  if (!name) return "?";
  const m = name.match(/\p{L}|\p{N}/u);
  return m ? m[0].toUpperCase() : "?";
}
