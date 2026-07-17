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
  /** Impress the owl itself into the wax instead of taking a glyph. Two
   * callers, both legitimate: the app speaking in its own voice (dialogs),
   * and the fallback when a name yields no monogram — see `monogram()`.
   * The owl stands in for *an impression we couldn't take*, never for a
   * specific agent's identity. */
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

/**
 * First letter or digit of a name, as a monogram — or `null` when the name
 * has none to give.
 *
 * `null` is the whole point of the signature. Agent names are free text and
 * are routinely pure emoji ("🦉"), pure punctuation, or empty; this used to
 * answer `"?"` for all of them, which stamped a *question mark* into the wax
 * on every one of that agent's turns — the seal, the most identity-carrying
 * mark in the app, reading as "who?". Callers must handle `null` by
 * impressing the owl instead (`<Seal mark />`): the app's own mark is always
 * a legitimate impression, and an unreadable name is not an error worth
 * shouting about on every bubble.
 */
export function monogram(name: string | undefined | null): string | null {
  if (!name) return null;
  const m = name.match(/\p{L}|\p{N}/u);
  return m ? m[0].toUpperCase() : null;
}
