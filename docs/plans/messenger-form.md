# Messenger Form — Round 3 of the Reskin

> **Draft (2026-07-16, Albus).** Round 1 replaced the inherited vm0
> palette with the Messenger identity; round 2 added craft density
> (elevation, hierarchy, composed empty states). The owner's verdict
> on the result: "换皮不彻底,只是换了个颜色" — the *color* identity
> landed, but the components themselves still wear generic faces.
> This round gives them a form language of their own. It sits between
> round 1's "skin" and a full re-layout: component-level shape and
> composition change, zero layout / information-architecture change.

## 1. Goal

A screenshot of any single component — a message bubble, a delegation
card, a dialog, a button row — should be recognizable as Owlery
*with the color stripped out*. Today it would be recognizable as
"a Tailwind project": one uniform radius, one border treatment,
symmetric bubbles, stock control shapes. The fix is one **shape
grammar**: a single geometric motif, chosen once, applied
consistently across every component family.

The bar (same judge as round 2): the owner looks at a chat exchange,
a delegation card, and a dialog, and none of them reads as a generic
part. Identity should survive a grayscale screenshot.

## 2. Why a "grammar" and not per-component styling

Round 2 already proved that componentwise polish (better shadows,
better spacing) does not produce identity — it produces a *nicer
generic template*. Identity comes from repetition of one
recognizable formal idea. Products people can identify from a
cropped screenshot (Linear, Arc, Notion) each have exactly one such
idea, applied everywhere. Owlery needs its one idea; this plan's
job is to pick it deliberately and spend it consistently.

## 3. Stage 1 — the sample room (STOP for owner approval)

Do NOT restyle the app in one pass. First build a **sample room**:

- Pick the two highest-traffic component surfaces: `MessageBubble`
  (one user + one assistant turn, with attribution row) and one
  delegation card (`AgentDelegationEventCard`).
- Produce **2–3 candidate shape grammars**, each rendered on those
  two components, screenshotted at real size on the real parchment
  background. Candidates must be *grammars*, not moods: each one is
  a short ruleset (corner treatment, edge treatment, seal/ornament
  policy, radius scale, where asymmetry lives) that could plausibly
  govern every component in the app.
- Starting directions worth exploring (executor may add their own,
  but each candidate must stay a one-idea grammar):
  - **Wax seal + letterhead**: circular seal motif as the anchor
    (attribution avatars, card headers, status chips); cards and
    bubbles get a letterhead rule; radius stays soft.
  - **Folded letter / cut corner**: one asymmetric cut or folded
    corner as the signature; bubbles and cards carry it in a
    consistent position; controls echo it at smaller scale.
  - **Deckle / stamp edge**: postal idiom — perforation or
    deckle-edge accents used sparingly as the recognition anchor.
- **Logo mark rides along.** The pending logo redesign (owner judged
  the current full-body owl "一般") is the *same decision* as the
  shape grammar — both answer "what is Owlery's geometric motif".
  Each grammar candidate ships with a matching simplified mark
  candidate (one geometric idea, legible at 16px, tested at
  16/32/96px on parchment). The owner picks a grammar and its mark
  together, once. `OwleryLogo.tsx` is already the single source, so
  the swap is cheap.
- Deliverable of stage 1: one comparison sheet (screenshots), a few
  sentences of tradeoff per candidate. **Then stop and wait for the
  owner's pick.** Nothing beyond the two sample components gets
  touched in stage 1.

## 4. Stage 2 — roll the chosen grammar across the app

After the owner picks, apply the grammar everywhere, by family:

1. **Bubbles** (`MessageBubble`): user vs assistant turns get
   *formally* distinct treatments (today they differ mostly by
   color/alignment). The attribution row (avatar + agent name) is
   designed as a letterhead, not a label. System-injected turns
   (`[bg-task-result]` badge) get a subdued variant of the same
   grammar.
2. **Card family** (`AgentDelegationEventCard`,
   `AgentDelegationRequestCard`, `ResearchCard`, `BgTaskChip`,
   `ToolApproval`, `QuestionPrompt`): one shared card skeleton in
   the grammar — these six are the app's most-seen surfaces after
   bubbles and currently six unrelated designs. Status accents keep
   the round-1 state colors (plum waiting, etc.) — the grammar
   changes form, never the state-color semantics.
3. **Dialog family** (`ui/dialog.tsx` + its consumers): the floating
   sheet becomes a designed object in the grammar (header treatment,
   corner signature), not a rounded rectangle with a title.
4. **Controls** (`ui/button.tsx`, `input.tsx`, `tabs.tsx`,
   `dropdown-menu.tsx`): radius scale and edge treatment follow the
   grammar. This is where restraint matters most — controls echo the
   motif; they must not each carry the full ornament.
5. **Sidebar items** (`SessionList`, `AgentList` rows): active/hover
   states and the session-item silhouette adopt the grammar's accent
   position so the rail reads as the same product as the chat.
6. Shape values (radii, cut sizes, seal dimensions, ornament
   opacities) land in `tokens.css` as tokens next to the color
   system — the grammar must be as token-driven as the palette.

## 5. Constraints

- **Layout and information architecture are untouched.** Panel
  positions, list structures, what-is-where: all frozen. This round
  changes what components *are shaped like*, not where they live.
- **DOM inside a component may change** (that is the point —
  wrappers, pseudo-element anchors, ornament spans are all fair
  game), but ARIA roles, visible text, and anything e2e locators
  key on stay stable by default.
- **e2e discipline, softened one notch from rounds 1–2**: the suite
  is no longer required to pass *untouched*. A handful of specs
  asserting internal structure may legitimately need updating — but
  every spec edit must be individually justified as "the assertion
  encoded the old form" (never "the test was in the way"), and the
  full suite must be green at the end. Treat an unexpected red as a
  regression first, an outdated assertion second.
- Round-1/2 assets are law: the parchment/ink/brass palette, the
  state-color system (incl. plum), the elevation scale, the type
  system. This round adds form; it does not relitigate color or
  elevation. New shape tokens compose with them.
- No new dependencies. Ornament is CSS/SVG, not a library.
- All four gates green before each commit (per CLAUDE.md); fresh
  before/after screenshots of the five §4 families; `bun run build`
  at the end so the preview instance serves the result.

## 6. What this defers / will not do

- **Re-layout (option A)**: sidebar form-factor, chat composition,
  any structural rearrangement — explicitly out. If the grammar
  lands and the owner still wants a distinct *silhouette*, that is
  a separate decision with a separate cost conversation.
- **Dark theme**: unchanged from round 1 — a future one-file
  addition, easier after this round tokenizes shape.
- **Favicon/app-icon production set** beyond swapping
  `OwleryLogo.tsx`: only if the chosen mark demands it; asset
  pipelines are not this round's job.
- **Chat content surfaces** (code blocks, tables, markdown body):
  already the strongest screens; the grammar frames them (bubble,
  card) but does not restyle their internals.

## 7. Round-4 revision — the seal is an agent's identity

Superseded by `docs/plans/agent-identity.md`. Round 3 shipped a
narrower reading of the seal: it stamped chat turns, while agent
avatars stayed emoji in the sidebar lists ("nothing is being sealed
there"), and the ornament budget was phrased as *one seal per
surface*. Round 4 corrects both, and this doc follows the code so the
two don't disagree:

- **An agent's identity IS its seal.** Wherever an agent appears — a
  chat turn, a sidebar row, a dialog header, the chat header, the
  usage table — it appears as its wax seal: its monogram (name's
  first letter) impressed in a wax colour assigned deterministically
  from its id (`--wax-*` palette, six non-red tones; red stays
  reserved for `destructive`). There is no emoji avatar anywhere; the
  `agents.avatar` column and its picker are retired.
- **Ornament budget, restated:** *one seal per identity*, not per
  surface. A list row carries the one identity it names; session rows
  and controls still get the `seal-dot` echo, never a second full
  seal. `<Seal>` (and its identity wrapper `<AgentSeal>`) is the app's
  one avatar primitive, at a new `--seal-avatar` (~20px) scale for
  rows and headers.
