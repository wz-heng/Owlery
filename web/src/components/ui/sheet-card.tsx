/**
 * The card skeleton — one shape for the six surfaces the user sees most
 * after bubbles (`docs/plans/messenger-form.md` §4.2): the two delegation
 * cards, ResearchCard, BgTaskChip, ToolApproval and QuestionPrompt.
 * Before round 3 these were six unrelated designs that happened to share
 * a border radius.
 *
 * Two scales, one grammar:
 *
 *   sheet  a ruled surface with a seal straddling its top edge. For
 *          anything that is a *statement*: an approval, a question, a
 *          reply from another agent.
 *   chip   an inline strip with the seal sitting in the header row. For
 *          anything that is a *status*: a bg task, a research job, an
 *          outbound request. At 16px the wax is a blob, not an
 *          impression, so the chip carries no glyph and no rule.
 *
 * Status accents stay on the round-1 state colours — `attention` is still
 * plum. The grammar changes form, never state-colour semantics.
 */
import type { ReactNode } from "react";

import { Seal, type SealTone } from "./seal";
import { cn } from "../../lib/utils";

/** Surface tones. Each pairs a border and a fill that already exist. */
export type CardTone = "brand" | "neutral" | "attention" | "success" | "destructive";

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

export interface SheetCardProps {
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
    >
      <Seal side={side} tone={TONE_WAX[tone]} subdued={subdued}>
        {glyph}
      </Seal>
      <div className="sheet-rule">
        <span className="truncate">{title}</span>
        {meta != null && <span className="sheet-rule-meta">{meta}</span>}
      </div>
      {children}
    </div>
  );
}

export interface SealChipProps {
  tone?: CardTone;
  /** Live state gets a spinner or an icon in place of the wax; the seal
   * is for identity, not for motion. */
  icon?: ReactNode;
  children: ReactNode;
  className?: string;
}

/**
 * The status scale. A chip is a strip, so the seal sits inline at its
 * head rather than straddling an edge it doesn't have. Callers that have
 * a live status icon pass it as `icon` and get no seal — one mark per
 * surface is the ornament budget, and a spinner outranks a monogram when
 * something is actually running.
 */
export function SealChip({
  tone = "neutral",
  icon,
  children,
  className,
}: SealChipProps) {
  return (
    <div
      className={cn(
        "seal-chip flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-xs",
        TONE_SURFACE[tone],
        className
      )}
      data-tone={tone}
    >
      {icon ?? <Seal side="left" tone={TONE_WAX[tone]} scale="chip" straddle={false} />}
      {children}
    </div>
  );
}
