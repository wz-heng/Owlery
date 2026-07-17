/**
 * The card skeleton — one shape for the six surfaces the user sees most
 * after bubbles (`docs/plans/messenger-form.md` §4.2): the two delegation
 * cards, ResearchCard, BgTaskChip, ToolApproval and QuestionPrompt.
 * Before round 3 these were six unrelated designs that happened to share
 * a border radius.
 *
 * Both scales are the same three parts — a seal, a ruled header, a body —
 * and differ only in how much room they have to say it:
 *
 *   SheetCard  the STATEMENT scale. The seal straddles the top edge, the
 *              letterhead is a full-bleed rule. For an approval, a
 *              question, a reply from another agent.
 *   SealChip   the STATUS scale. Inline strip; the seal sits at the head
 *              of the rule rather than straddling an edge a 16px-tall
 *              thing doesn't really have. For a bg task, a research job,
 *              an outbound request.
 *
 * Every surface tone and every wax colour below is an existing round-1
 * token. The grammar changes form, never state-colour semantics: an
 * `attention` card is still plum, a `destructive` one still wax red.
 */
import type { HTMLAttributes, ReactNode } from "react";

import { Seal, type SealTone } from "./seal";
import { cn } from "../../lib/utils";

/** Surface tones. Each pairs a border and a fill that already exist. */
export type CardTone =
  | "brand"
  | "neutral"
  | "attention"
  | "success"
  | "destructive";

const TONE_SURFACE: Record<CardTone, string> = {
  brand: "border-primary/40 bg-primary-50",
  neutral: "border-ink-300 bg-ink-100",
  attention: "border-attention/35 bg-attention-surface",
  success: "border-success/40 bg-success-surface",
  destructive: "border-destructive/40 bg-destructive-surface",
};

/** The seal's wax follows the surface's tone — one decision, not two. */
const TONE_WAX: Record<CardTone, SealTone> = {
  brand: "brand",
  neutral: "ink",
  attention: "attention",
  success: "success",
  destructive: "destructive",
};

/** The letterhead's right-hand end: a timestamp, an id, a tool name, a
 * status. It truncates rather than pushing the rule open — a long MCP
 * tool name would otherwise blow the header apart on a phone. Callers
 * pass plain strings so we can hand the full value to `title`. */
function RuleMeta({ meta }: { meta: ReactNode }) {
  return (
    <span
      className="sheet-rule-meta"
      title={typeof meta === "string" ? meta : undefined}
    >
      {meta}
    </span>
  );
}

export interface SheetCardProps
  extends Omit<HTMLAttributes<HTMLDivElement>, "title"> {
  tone?: CardTone;
  /** The author's side — where the seal hangs. */
  side?: "left" | "right";
  /** Stamped into the wax: a monogram or a kind icon, never an emoji. */
  glyph?: ReactNode;
  /** The letterhead's left half — who is speaking. */
  title: ReactNode;
  /** The letterhead's right half — a timestamp, an id, a status. */
  meta?: ReactNode;
  children: ReactNode;
  className?: string;
  /** System-injected rather than spoken: lighter wax, no lift. */
  subdued?: boolean;
  /** Escape hatch for a surface that brings its own fill (the sheet still
   * supplies the rule, the seal and the padding). */
  surfaceClassName?: string;
  elevated?: boolean;
}

/** The statement scale: ruled, sealed, raised off the parchment. */
export function SheetCard({
  tone = "brand",
  side = "right",
  glyph,
  title,
  meta,
  children,
  className,
  subdued = false,
  surfaceClassName,
  elevated = true,
  ...rest
}: SheetCardProps) {
  return (
    <div
      className={cn(
        "sheet border text-sm",
        surfaceClassName ?? TONE_SURFACE[tone],
        elevated && "shadow-[var(--elevation-raised)]",
        className
      )}
      data-tone={tone}
      {...rest}
    >
      <Seal side={side} tone={TONE_WAX[tone]} subdued={subdued}>
        {glyph}
      </Seal>
      <div className="sheet-rule">
        <span className="truncate">{title}</span>
        {meta != null && <RuleMeta meta={meta} />}
      </div>
      {children}
    </div>
  );
}

export interface SealChipProps
  extends Omit<HTMLAttributes<HTMLDivElement>, "title"> {
  tone?: CardTone;
  /**
   * Replaces the seal at the head of the rule. Callers pass a live icon
   * here while something is actually running: motion is the information,
   * and the ornament budget is one mark per surface, so a spinner
   * outranks a monogram. Settle, and the wax comes back.
   */
  head?: ReactNode;
  /** Impressed into the wax when `head` is not given. */
  glyph?: ReactNode;
  /** The rule's content — the chip's letterhead. May contain the caller's
   * own expand toggle. */
  title: ReactNode;
  /** The rule's right-hand end. Truncates. */
  meta?: ReactNode;
  /** Trailing controls. Deliberately a separate slot from `title`: these
   * sit OUTSIDE any toggle button the caller puts in the rule, because
   * nesting <button> inside <button> is invalid HTML and breaks a11y. */
  actions?: ReactNode;
  /** Below the rule. Absent → the rule carries no border, because a rule
   * under nothing is just a line. */
  children?: ReactNode;
  className?: string;
  /** Sit inline with surrounding content rather than filling the column. */
  inline?: boolean;
}

/** The status scale. Same three parts as SheetCard, one size down. */
export function SealChip({
  tone = "neutral",
  head,
  glyph,
  title,
  meta,
  actions,
  children,
  className,
  inline = false,
  ...rest
}: SealChipProps) {
  return (
    <div
      className={cn(
        "chip border",
        inline ? "inline-block" : "block",
        TONE_SURFACE[tone],
        className
      )}
      data-tone={tone}
      {...rest}
    >
      <div className={cn("chip-rule", children != null && "chip-rule-ruled")}>
        {head ?? (
          <Seal
            side="left"
            tone={TONE_WAX[tone]}
            scale="chip"
            straddle={false}
          >
            {glyph}
          </Seal>
        )}
        <div className="min-w-0 flex-1">{title}</div>
        {meta != null && <RuleMeta meta={meta} />}
        {actions}
      </div>
      {children != null && <div className="chip-body">{children}</div>}
    </div>
  );
}
