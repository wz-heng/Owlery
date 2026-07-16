/**
 * Messenger Form — round 3 stage 1 sample room.
 *
 * Renders the three candidate shape grammars (plus today's form as a
 * control) on the two highest-traffic surfaces named in the plan:
 * MessageBubble (one user + one assistant turn, attribution included)
 * and AgentDelegationEventCard. Reachable only from the Vite dev
 * server at `/form-samples.html`; `vite build` never sees it, so no
 * product route and no bundle bytes.
 *
 * The bubbles here are deliberate *replicas*, not the real components
 * wired to the store: the real ones need a live session, a delegation
 * fetch, and the WS store to render, and the plan freezes product
 * components for stage 1. Markup and classes below are copied verbatim
 * from `MessageBubble.tsx` / `AgentDelegationEventCard.tsx`; the only
 * edits are the shape classes each grammar swaps in. That keeps the
 * stage-2 diff honest — whichever column wins, its deltas against the
 * `current` column are exactly the changes that land in the real
 * files.
 */

import type { CSSProperties, ReactNode } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  IconArrowBackUp,
  IconChevronDown,
  IconExternalLink,
  IconSubtask,
} from "@tabler/icons-react";

import { MARKS, scallopPath } from "./marks";
import "./grammars.css";

type GrammarId = "current" | "seal" | "fold" | "stamp";

const USER_TEXT =
  "帮我把 e2e 里那三个真机 spec 拆出来单独跑,顺便看下 handoff-pull 为什么只在代理机上红。";

const ASSISTANT_MD = [
  "拆出来了 —— 三个 `@llm` spec 现在走 `test:e2e:llm`,其余 66 个跑 `test:e2e:fast`。",
  "",
  "`handoff-pull` 的红跟代码无关:它的 `--server` 指向一个临时 loopback 监听器,",
  "而这台机器的 `http_proxy` 没有豁免 `127.0.0.1`,请求被代理吞掉了。",
  "",
  "- 本机修法:`export no_proxy=127.0.0.1,localhost`",
  "- 产品侧不动 —— 这个 flag 本来就要支持远端 target",
].join("\n");

const DELEGATION_BODY = `分支 fix/mcp-callback-trust-env 看过了,结论:通过。

trust_env=False 钉在 loopback 回调上是对的 — 那些 target 永远是 assembly.py 里硬编码的 127.0.0.1:{port},不存在需要走代理的情形。
两处 [nit]:_shared.py:41 的注释还写着旧的 "octopus" 拼法;测试里 monkeypatch 的 env 建议用 fixture 收口。`;

/* ── The seal primitive (grammar A) ──────────────────────────────── */

function SealDisc({
  side,
  tone,
  children,
}: {
  side: "left" | "right";
  tone: string;
  children: ReactNode;
}) {
  return (
    <span className="g-seal-mark grid place-items-center" data-side={side}>
      <svg
        viewBox="0 0 32 32"
        className="absolute inset-0 h-full w-full"
        style={{ color: tone }}
        aria-hidden
      >
        <path d={scallopPath(16, 16, 13.4, 16.4, 13)} fill="currentColor" />
        <path
          d={scallopPath(16, 16, 10.2, 12.2, 13)}
          fill="none"
          stroke="hsl(var(--ink-0) / 0.4)"
          strokeWidth="1.1"
        />
      </svg>
      <span
        className="relative text-[10px] font-semibold leading-none"
        style={{ color: "hsl(var(--primary-foreground))" }}
      >
        {children}
      </span>
    </span>
  );
}

/* ── User turn ───────────────────────────────────────────────────── */

function UserBubble({ grammar }: { grammar: GrammarId }) {
  const shell = "max-w-[85%] space-y-1";
  const label = (
    <div className="msg-label flex items-center justify-end gap-2 text-xs font-semibold text-muted-foreground">
      <span>You</span>
    </div>
  );

  if (grammar === "current") {
    return (
      <div className="msg msg-user group flex justify-end">
        <div className={shell}>
          {label}
          <div className="msg-content inline-block rounded-lg rounded-br-sm border border-primary/35 bg-primary-50 px-4 py-3 text-sm text-foreground whitespace-pre-wrap break-words shadow-[var(--elevation-raised)]">
            {USER_TEXT}
          </div>
        </div>
      </div>
    );
  }

  if (grammar === "seal") {
    // The attribution row moves *inside* the sheet and becomes the
    // letterhead — that is the grammar, not a decoration on top of it.
    return (
      <div className="msg msg-user group flex justify-end">
        <div className={shell}>
          <div
            className="g-seal msg-content inline-block border border-primary/35 bg-primary-50 px-4 pb-3 text-sm text-foreground shadow-[var(--elevation-raised)]"
            style={{ "--g-gutter": "1rem" } as CSSProperties}
          >
            <SealDisc side="right" tone="hsl(var(--primary-700))">
              W
            </SealDisc>
            <div className="g-seal-rule flex items-center justify-between gap-8 text-[10px] font-semibold uppercase tracking-[0.16em] text-muted-foreground">
              <span>You</span>
              <span className="font-normal normal-case tracking-normal">21:04</span>
            </div>
            <div className="whitespace-pre-wrap break-words">{USER_TEXT}</div>
          </div>
        </div>
      </div>
    );
  }

  if (grammar === "fold") {
    return (
      <div className="msg msg-user group flex justify-end">
        <div className={shell}>
          {label}
          <div
            className="g-fold msg-content inline-block border border-primary/35 bg-primary-50 px-4 py-3 text-sm text-foreground shadow-[var(--elevation-raised)]"
            data-fold="br"
            style={{ "--fold-back": "hsl(var(--primary-200))" } as CSSProperties}
          >
            <div
              className="g-fold-body whitespace-pre-wrap break-words"
              data-fold="br"
            >
              {USER_TEXT}
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="msg msg-user group flex justify-end">
      <div className={shell}>
        {label}
        <div
          className="g-stamp msg-content inline-block border border-primary/35 bg-primary-100 shadow-[var(--elevation-raised)]"
          data-selvedge="right"
          style={{ "--perf": "hsl(var(--primary-100))" } as CSSProperties}
        >
          <div className="g-stamp-field bg-primary-50 px-3.5 py-2.5 text-sm text-foreground whitespace-pre-wrap break-words">
            {USER_TEXT}
          </div>
          <span
            className="g-stamp-denom text-[9px] font-semibold uppercase tracking-[0.18em]"
            data-selvedge="right"
          >
            You
          </span>
        </div>
      </div>
    </div>
  );
}

/* ── Assistant turn ──────────────────────────────────────────────── */

function AssistantBody() {
  return (
    <Markdown remarkPlugins={[remarkGfm]}>{ASSISTANT_MD}</Markdown>
  );
}

function AssistantBubble({ grammar }: { grammar: GrammarId }) {
  const label = (
    <div className="msg-label flex items-center gap-1.5 text-xs font-semibold text-muted-foreground">
      <span aria-hidden className="text-sm leading-none">
        🦉
      </span>
      <span>Dobby</span>
    </div>
  );

  if (grammar === "current") {
    return (
      <div className="msg msg-assistant space-y-1">
        {label}
        <div className="msg-content markdown rounded-lg rounded-tl-sm border border-border bg-card px-4 py-3 text-sm leading-relaxed shadow-[var(--elevation-raised)]">
          <AssistantBody />
        </div>
      </div>
    );
  }

  if (grammar === "seal") {
    return (
      <div className="msg msg-assistant">
        <div
          className="g-seal msg-content markdown border border-border bg-card px-4 pb-3 text-sm leading-relaxed shadow-[var(--elevation-raised)]"
          style={{ "--g-gutter": "1rem" } as CSSProperties}
        >
          <SealDisc side="left" tone="hsl(var(--ink-800))">
            🦉
          </SealDisc>
          <div className="g-seal-rule flex items-center justify-between gap-8 text-[10px] font-semibold uppercase tracking-[0.16em] text-muted-foreground">
            <span>Dobby</span>
            <span className="font-normal normal-case tracking-normal">21:06</span>
          </div>
          <AssistantBody />
        </div>
      </div>
    );
  }

  if (grammar === "fold") {
    return (
      <div className="msg msg-assistant space-y-1">
        {label}
        <div
          className="g-fold msg-content markdown border border-border bg-card px-4 py-3 text-sm leading-relaxed shadow-[var(--elevation-raised)]"
          data-fold="bl"
          style={{ "--fold-back": "hsl(var(--ink-200))" } as CSSProperties}
        >
          <div className="g-fold-body" data-fold="bl">
            <AssistantBody />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="msg msg-assistant space-y-1">
      {label}
      <div
        className="g-stamp msg-content border border-border bg-ink-100 shadow-[var(--elevation-raised)]"
        data-selvedge="left"
        style={{ "--perf": "hsl(var(--ink-100))" } as CSSProperties}
      >
        <div className="g-stamp-field markdown bg-card px-3.5 py-2.5 text-sm leading-relaxed">
          <AssistantBody />
        </div>
        <span
          className="g-stamp-denom text-[9px] font-semibold uppercase tracking-[0.18em]"
          data-selvedge="left"
        >
          Dobby
        </span>
      </div>
    </div>
  );
}

/* ── Delegation event card ───────────────────────────────────────── */

function DelegationInner({ header = true }: { header?: boolean }) {
  return (
    <>
      {header && (
      <div className="w-full flex items-start gap-2 text-left">
        <span className="text-muted-foreground shrink-0 mt-0.5">
          <IconChevronDown size={14} />
        </span>
        <IconSubtask size={14} className="text-primary shrink-0 mt-0.5" />
        <span className="flex-1 leading-snug">
          <span className="font-medium">Snape</span>
          <span className="text-muted-foreground"> replied</span>
        </span>
      </div>
      )}
      <pre className="agent-delegation-body mt-2 ml-6 whitespace-pre-wrap break-words text-xs text-foreground font-sans leading-relaxed">
        {DELEGATION_BODY}
      </pre>
      <div className="mt-2 ml-6 flex items-center gap-3 text-[11px]">
        <span className="btn-open-delegation inline-flex items-center gap-1 text-primary">
          <IconExternalLink size={11} />
          Open Snape&apos;s session
        </span>
        <span className="text-muted-foreground/70">delegation_id=3ba1f2d9ba62</span>
      </div>
    </>
  );
}

function DelegationCard({ grammar }: { grammar: GrammarId }) {
  const label = (
    <div className="msg-label text-xs font-semibold text-muted-foreground text-right flex items-center justify-end gap-1.5">
      <IconArrowBackUp size={12} className="text-muted-foreground" />
      <span>From delegation · 3ba1f2d9</span>
    </div>
  );

  if (grammar === "current") {
    return (
      <div className="msg msg-agent-delegation-event flex justify-end">
        <div className="max-w-[85%] space-y-1">
          {label}
          <div className="agent-delegation-card rounded-lg border px-3 py-2.5 text-sm border-primary/40 bg-primary-50">
            <DelegationInner />
          </div>
        </div>
      </div>
    );
  }

  if (grammar === "seal") {
    return (
      <div className="msg msg-agent-delegation-event flex justify-end">
        <div className="max-w-[85%] space-y-1">
          <div
            className="g-seal agent-delegation-card border border-primary/40 bg-primary-50 px-3 pb-2.5 text-sm shadow-[var(--elevation-raised)]"
            style={{ "--g-gutter": "0.75rem" } as CSSProperties}
          >
            <SealDisc side="right" tone="hsl(var(--primary-700))">
              <IconSubtask size={12} />
            </SealDisc>
            <div className="g-seal-rule flex items-center justify-between gap-8 text-[10px] font-semibold uppercase tracking-[0.16em] text-muted-foreground">
              <span>Snape · replied</span>
              <span className="font-normal normal-case tracking-normal">
                3ba1f2d9
              </span>
            </div>
            <DelegationInner header={false} />
          </div>
        </div>
      </div>
    );
  }

  if (grammar === "fold") {
    return (
      <div className="msg msg-agent-delegation-event flex justify-end">
        <div className="max-w-[85%] space-y-1">
          {label}
          <div
            className="g-fold agent-delegation-card border border-primary/40 bg-primary-50 px-3 py-2.5 text-sm shadow-[var(--elevation-raised)]"
            data-fold="br"
            style={{ "--fold-back": "hsl(var(--primary-200))" } as CSSProperties}
          >
            <div className="g-fold-body" data-fold="br">
              <DelegationInner />
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="msg msg-agent-delegation-event flex justify-end">
      <div className="max-w-[85%] space-y-1">
        {label}
        <div
          className="g-stamp agent-delegation-card border border-primary/40 bg-primary-100 shadow-[var(--elevation-raised)]"
          data-selvedge="right"
          style={{ "--perf": "hsl(var(--primary-100))" } as CSSProperties}
        >
          <div className="g-stamp-field bg-primary-50 px-3 py-2.5 text-sm">
            <DelegationInner />
          </div>
          <span
            className="g-stamp-denom text-[9px] font-semibold uppercase tracking-[0.18em]"
            data-selvedge="right"
          >
            Snape
          </span>
        </div>
      </div>
    </div>
  );
}

/* ── The sheet ───────────────────────────────────────────────────── */

interface Candidate {
  id: GrammarId;
  title: string;
  subtitle: string;
  mark: keyof typeof MARKS | null;
  rules: string[];
}

const CANDIDATES: Candidate[] = [
  {
    id: "current",
    title: "Control — today",
    subtitle: "round 2, shipped",
    mark: null,
    rules: [
      "One radius everywhere; one 1px border everywhere.",
      "Author encoded by colour + alignment only.",
      "A 2px corner nudge (rounded-br-sm / -tl-sm) as the sole shape signal.",
      "No motif — nothing repeats across families.",
    ],
  },
  {
    id: "seal",
    title: "A · Seal & Letterhead",
    subtitle: "the motif is THE DISC",
    mark: "seal",
    rules: [
      "Corners: uniform --radius. A corner never carries meaning.",
      "Every sheet is ruled: a full-bleed hairline under its header.",
      "The attribution row IS the letterhead — it lives inside the sheet.",
      "A scalloped wax disc straddles the top edge, inset 14px from the author's side.",
      "The disc is the app's one avatar/status primitive (agent emoji, kind icon, initial).",
      "Asymmetry: only which side the seal sits on. Ornament budget: one disc per surface.",
      "Scale: 26px on sheets, 32px on dialogs, 8px dot on controls/tabs.",
    ],
  },
  {
    id: "fold",
    title: "B · Dog-ear / Fold",
    subtitle: "the motif is THE FOLD",
    mark: "fold",
    rules: [
      "Three corners at --radius; the fourth is square and carries a turned flap.",
      "Where: the sheet's bottom corner on the author's side — where a letter is signed and folded.",
      "That single rule gives user vs assistant a formal difference with zero ornament.",
      "The flap is paper-back tone + a crease hairline + its own drop shadow.",
      "Nothing else is added. Attribution stays a plain caption on the folded side.",
      "Scale: --fold 18px on sheets, 8px on chips/controls, 24px on dialogs.",
      "Sidebar: the active row folds on its chat-facing corner.",
    ],
  },
  {
    id: "stamp",
    title: "C · Postal Frame",
    subtitle: "the motif is THE PERFORATION",
    mark: "stamp",
    rules: [
      "Every sheet is a stamp: a tinted band around a content field.",
      "Band and field are separated by a punched rule — teeth in the band's own tone.",
      "Asymmetry: the selvedge — the band doubles to 19px on the author's side.",
      "The selvedge is the only place a mark may go (the denomination).",
      "Radius: outer --radius, field --radius minus 4px. Pitch: --perf-pitch 8px.",
      "Controls: perf collapses to a single toothed underline on the active tab.",
    ],
  },
];

function MarkRow({ id }: { id: keyof typeof MARKS }) {
  const Mark = MARKS[id];
  return (
    <div className="flex items-end gap-5">
      {[16, 32, 96].map((s) => (
        <div key={s} className="flex flex-col items-center gap-1.5">
          <Mark size={s} className="text-primary-700" />
          <span className="text-[9px] font-mono text-muted-foreground">{s}</span>
        </div>
      ))}
    </div>
  );
}

function Column({ candidate }: { candidate: Candidate }) {
  return (
    <section className="flex w-[440px] shrink-0 flex-col gap-4">
      <header className="space-y-1">
        <h2 className="font-brand text-lg font-semibold text-foreground">
          {candidate.title}
        </h2>
        <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">
          {candidate.subtitle}
        </p>
      </header>

      <div className="space-y-4 rounded-lg border border-ink-300/70 bg-transparent p-5">
        <UserBubble grammar={candidate.id} />
        <AssistantBubble grammar={candidate.id} />
        <DelegationCard grammar={candidate.id} />
      </div>

      <div className="rounded-lg border border-ink-300/70 p-5">
        <p className="mb-4 text-[10px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
          Mark
        </p>
        {candidate.mark ? (
          <MarkRow id={candidate.mark} />
        ) : (
          <p className="text-xs italic text-muted-foreground">
            current OwleryLogo — unchanged
          </p>
        )}
      </div>

      <ul className="space-y-1.5 text-xs leading-relaxed text-ink-800">
        {candidate.rules.map((r) => (
          <li key={r} className="flex gap-2">
            <span className="text-primary-700">·</span>
            <span>{r}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function SampleRoom() {
  return (
    <div className="min-h-screen px-8 py-10">
      <header className="mb-8 max-w-4xl space-y-2">
        <h1 className="font-brand text-2xl font-semibold">
          Messenger Form — candidate shape grammars
        </h1>
        <p className="text-sm text-muted-foreground">
          Round 3 stage 1 sample room. Real parchment ground, real type,
          real elevation tokens, real component markup — only the shape
          rules differ per column. The bar: strip the colour and the
          column still says Owlery.
        </p>
      </header>
      <div className="flex gap-10">
        {CANDIDATES.map((c) => (
          <Column key={c.id} candidate={c} />
        ))}
      </div>
    </div>
  );
}
