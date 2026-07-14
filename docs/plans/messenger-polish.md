# Messenger Polish — Round 2 of the Reskin

> **Draft (2026-07-14, Albus).** The Messenger identity
> (`messenger-reskin.md`) landed and passed review, but the owner's
> verdict on the preview was "不够精致" — the color identity arrived
> without the craft density. This round closes that gap. Same
> branch (`feat/messenger-reskin`), same constraints, same tripwire
> (e2e passes unmodified).

## 1. Goal

The preview at :8001 should stop reading as "beige template with
nice colors" and start reading as a crafted product. The bar: the
owner looks at login, sidebar, empty state, and a dialog, and none
of them feels flat, sparse, or wireframe-y. Chat content surfaces
(code blocks, tables, tool cards) are already the strongest screen
— they set the bar the chrome must rise to.

## 2. The five findings (owner-confirmed diagnosis)

1. **Everything is flat.** Hairline borders + near-zero shadows
   everywhere turn parchment into beige void. Build a small
   elevation scale as tokens — flat surface / raised card / floating
   overlay / inset well — with warm-tinted (not gray) shadows, and
   apply it consistently: dialogs float, cards sit crisply, inputs
   and empty-wells sink. Kill the dashed-border wells (wireframe
   idiom) in favor of the inset treatment.
2. **The sidebar is a template rail.** Uniform gray uppercase labels
   with naked "+" buttons, no icons, no rhythm, dead space below.
   Give it real hierarchy: micro-typography on section labels
   (size/weight/tracking), purposeful hover/active states, designed
   "+" affordances, a composed user card, and managed vertical
   rhythm so the empty lower half looks intentional.
3. **Empty states are voids, not compositions.** The owl mark is
   washed out to near-invisibility and the copy floats in cream
   nothingness. Raise the mark's presence, contain the composition
   (plate, vignette, or motif — executor's judgment), give the copy
   a serif headline + supporting line hierarchy, and make the
   treatment consistent across chat / sessions / connectors / usage.
4. **Controls are rough.** Segmented pills, buttons, inputs, close
   buttons: one refined standard (heights, radii, focus-visible
   rings, hover/pressed states). The dialog scrim is muddy gray-brown
   and kills the parchment behind it — switch to warm ink at low
   opacity so the world dims instead of dying.
5. **Micro-typography.** Tracking on uppercase labels, weight
   contrast in the login card (tagline hierarchy), label/value
   contrast in tables and stat rows. Small individually; together
   they are the missing 一口气. CJK fallback must stay deliberate.

## 3. Constraints (unchanged from round 1)

- Identity tokens stay: parchment / ink / brass, wax red for
  destructive. This round refines execution, not direction.
- No layout/DOM restructuring; additive markup where a composition
  needs it (per the clarified §5 of `messenger-reskin.md`). The e2e
  suite passing unmodified remains the tripwire.
- No new dependencies. Chat content surfaces largely untouched.

## 4. Acceptance

- All four gates green (backend via `-k "not real"` per the
  documented pre-existing codex real-CLI contention).
- Fresh before/after screenshots of the four named surfaces.
- `cd web && bun run build` at the end so the :8001 preview serves
  the polished bundle — the final judge is the owner's eye on it.
