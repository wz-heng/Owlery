# Messenger Reskin — Owlery's Own Visual Identity

> **Draft (2026-07-13, Albus).** Replace the inherited vm0 look
> (white + dark blue, `tokens.css` self-describes as "stolen
> wholesale from vm0's globals.css") with a visual language that is
> Owlery's own. Skin only — layout and DOM structure do not move.

## 1. Goal

Anyone opening Owlery after this lands should see a product with its
own face — a warm, owl-post "Messenger" identity — with zero visual
residue of the previous white/dark-blue template. The bar for done:

- No screen shows the old palette. Side-by-side before/after
  screenshots should not read as the same product.
- The polish details that make an app feel like a mature product —
  empty states, loading skeletons, focus rings, hover/transition
  consistency — are present and uniform, not just recolored.
- All four test suites green. Because this is a skin swap (class and
  token changes, not structural ones), the 69 Playwright e2e specs
  are expected to survive largely untouched; a broken e2e is a smell
  that the change went deeper than skin.

## 2. Motivation

The current frontend look was inherited, not chosen: the entire
palette was lifted from another project, and it reads as a generic
admin template. The owner wants the product's visual identity to be
one they defined. Separately, "mature product" perception is mostly
detail density — the current UI is missing table-stakes polish
(empty states, skeletons, consistent transitions). Both are fixed in
the same pass because they are the same pass: every token and detail
touched is one more inherited fingerprint replaced with a deliberate
choice.

Strategy note: this is identity replacement going forward, not
history scrubbing. Git history stays as-is; the narrative this work
supports is "I took ownership and redefined it", which a coherent
reskin demonstrates by itself.

## 3. The identity: "Messenger" (信使)

Owl post. Warm light theme — parchment, ink, amber.

- **Surfaces**: parchment off-white / warm cream backgrounds, warm
  (not cool) gray scale for borders and muted text.
- **Text**: ink — very dark warm gray, not pure black.
- **Primary accent**: amber/gold (the owl's eyes; also reads as
  brass/wax). Replaces the dark blue everywhere the blue appears
  today.
- **Secondary punctuation**: a wax-seal red is available for
  destructive/alert semantics. Use sparingly; amber carries the
  brand, red carries danger.
- **Type**: a characterful pairing — e.g. a serif or semi-serif for
  headings/brand moments, a clean sans for UI and body. Executor
  picks the actual faces (self-hosted or system-stack; no
  render-blocking webfont regressions, and the CJK fallback chain
  must be deliberate since the UI renders Chinese chat content
  constantly). Currently `tokens.css` defines only 3 font-family
  declarations, so this is centralized too.
- **Brand moments**: login page, sidebar header/logo treatment, and
  empty states get real design attention (owl/letter motifs, warm
  copy) — these are the three screens where identity registers.
- **Texture**: radius/shadow/border weight tuned to the warm
  identity (softer, paper-like) rather than inherited defaults.

## 4. Design points (where the work actually lives)

1. **`web/src/styles/tokens.css` is the single lever.** All colors
   live there as HSL triplets behind semantic tokens; components
   reference semantics. The reskin is primarily a redefinition of
   those triplets plus type/radius/shadow tokens. This file's vm0
   attribution comments go away with the values.
2. **Absorb the strays.** The only hardcoded colors outside
   `tokens.css` are in `FileViewerDialog.css` and a few gray-scale
   reads in `index.css` — fold them into the token system so the
   next reskin (or a future dark theme) is also a one-file job.
3. **Code blocks must match.** `FileViewerDialog.tsx` imports
   `highlight.js/styles/github.css`; MessageBubble's chat code
   blocks render through the same pipeline. Pick/build a
   highlight theme that sits on parchment. Same check for KaTeX
   output legibility on the new background.
4. **The chat surface is the product.** MessageBubble typography,
   code-block treatment, tool-call / delegation / research cards'
   visual hierarchy are where users spend 90% of their time — the
   new identity must be strongest there, not just in the chrome.
5. **Detail-density sweep**, same pass: empty states (sessions list,
   chat, agents, connectors), loading skeletons, focus-visible
   rings, hover states, transition timing — one consistent standard
   applied everywhere, defined as tokens where possible.
6. **Verification is visual as well as mechanical.** Beyond the four
   suites, the executor should drive the real app (login → chat →
   dialogs → mobile width) and screenshot the key screens; the
   acceptance judgment in §1 is made on those screenshots.

## 5. Non-goals ("不做" 清单)

- **No layout/DOM restructuring.** Sidebar, navigation, page
  composition all stay. If using the new skin surfaces a structural
  itch, that's a separate plan ("换骨" was explicitly deferred by
  the owner).
- **No dark theme.** Messenger is a light identity. The token
  consolidation (§4.2) leaves a dark theme as a future one-file
  addition, but it ships with a real need, not now.
- **No git-history rewriting or attribution scrubbing.** Out of
  scope and out of strategy.
- **No component-library or dependency changes.** Radix + the
  existing `ui/` primitives stay; this is values, not architecture.
- **No logo redesign beyond treatment.** `OwleryLogo.tsx` gets
  recolored/re-set into the new identity; commissioning new brand
  artwork is not this task.
