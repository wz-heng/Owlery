# Session relay: context watermark + autonomous handoff

## 1. Why

Two production incidents, one disease — marathon sessions:

1. **Hot re-read** (2026-07-09): a 20-turn Dobby session at ~200k context
   burned 43.7M cache-read tokens (≈$54). Each turn re-reads the full
   context once per tool step; a fat session pays ~10× its context per
   turn.
2. **Cold rewrite / wake-up tax** (2026-07-15): the prompt cache TTL is
   1h. Poking a fat idle session forces a full cache re-*write* of the
   context at ~2× input price. A 4-day-old session (~1M context, riding
   the Fable window) paid **$20.70 for a single turn** before doing any
   work, paid that tax four times in one day, and burned $212 lifetime.
   Two such pokes together ate ~50% of a 5h quota window in minutes.

Both costs are proportional to context length, and both recur for as
long as the session stays alive. "Open a fresh session per task" as a
*discipline* has failed repeatedly — sticky sessions, delegation
replies, and bg-task results all pull work back into old sessions, and
nothing in the product shows how fat a session has become. This plan
turns the discipline into a mechanism.

Decided with the user (2026-07-15): the relay is **autonomous** — no
confirmation gate. The agent wraps up, hands off, and the user is told
after the fact.

## 2. Goal

When a session's context crosses a threshold, Owlery ends its life
gracefully and automatically: the agent writes a handoff note, a
successor session opens with that note, live bindings move over, the
old session is archived with a visible pointer, and the user is
notified. No session should ever again accumulate days of context and
tax every future touch.

Deliverables, in one project (per Do-It-Right, no phase split):

1. context watermark measured and visible per session,
2. wrap-up reminder injected into the agent past the threshold,
3. autonomous relay at the turn boundary,
4. binding/routing migration to the successor,
5. after-the-fact surfacing to the user.

## 3. Watermark measurement

- Current context size ≈ the **latest API call's**
  `input + cache_read + cache_creation` tokens. This is per-call data,
  not the per-turn aggregate that `turn_usage` stores (a fat turn sums
  ~10 re-reads; the aggregate overstates context ~10×). The claude
  stream already carries per-call usage on each assistant event; codex
  reports token counts in its stream too. Surface it from the harness
  layer (the single boundary — `harness-layer.md`), via the
  `RuntimeProfile`, so both backends feed the same field. Executor
  verifies the exact codex field.
- Persist the latest watermark on the session and push it over WS with
  turn events, so the UI and the relay controller both see it without
  new polling.

## 4. Threshold: absolute tokens, not window percent

The wake-up tax and the re-read cost are proportional to *absolute*
context size; the model window is irrelevant to cost. A "70% of
window" rule on a 1M-window model would relay at 700k — after the
damage. Default threshold **~150k tokens**, one per-agent setting to
override. Evaluation happens **at turn end only** — never kill a turn
mid-flight. An idle fat session thus pays at most one more wake-up tax:
the turn that pokes it is also the turn that retires it.

## 5. The relay sequence

At turn end with watermark ≥ threshold:

1. **Wrap-up reminder.** If the turn is still producing (multi-step),
   inject a system reminder mid-turn — the same injection channel used
   for bg-task results — telling the agent: finish the current step,
   commit work in progress, and write a handoff note. If the turn ended
   before any reminder fired, run one dedicated harness turn whose only
   instruction is the handoff note. That one extra fat-context turn is
   a one-time cost that replaces a recurring tax.
2. **Handoff note.** Session-scoped baton, distinct from agent memory
   (agent-scoped, cross-session — `memory.md`). Must cold-start a
   successor: task goal, current state, decisions taken and rejected,
   files/branches touched, immediate next steps, pointers to plan docs.
   Persist it as a message in the old session AND as the opening
   context of the new one.
3. **Successor session.** Same agent, same working dir, same backend
   and credential; name derived from the original with a relay
   generation marker. Opens with the handoff note as its first-turn
   context plus a machine-readable pointer to the origin session id.
   Do NOT auto-load any origin transcript beyond the note.
4. **Binding migration.** Everything that injects future turns by
   session id must point at the successor:
   - bridge mappings (sticky chat → session),
   - in-flight delegations (the delegation id IS the child session id;
     the *parent* pointer for reply injection is what moves),
   - in-flight bg tasks (their completion turns must land in the
     successor, not the archived corpse).
   Remap rather than wait — waiting on a long-running child blocks the
   relay indefinitely. Executor inventories every injection path
   (`delegations`, `bg_tasks`, bridges, schedules) and covers each with
   a test; a reply landing in an archived session is the failure mode
   that would silently strand work.
5. **Retire and tell.** Archive the old session with a visible
   "relayed to →" marker; the successor shows "relayed from ←". Emit a
   notifier event (so a bound telegram chat learns its session moved).
   The relay is autonomous by decision — the surfacing is the *entire*
   consent story, so it must be unmissable, not a log line.

## 6. Watermark visibility (UI)

A per-session context gauge (e.g. `142k`) in the session header and
list, with a warning state past ~70% of the relay threshold and a
distinct "relay pending" state once crossed. This is the human-facing
half: it explains *why* a relay happened and lets the user see fat
building up in real time. Follow the reskin design language
(`messenger-reskin.md`) — no new visual dialect.

## 7. Interactions with existing behavior

- **Harness auto-compaction** (claude-side) may shrink context on its
  own; the watermark reflects whatever the last call actually carried,
  so a compacted session naturally drops below threshold — no special
  case.
- **Fork/rewind** (`session-fork.md`, `session-rewind.md`): a forked
  session inherits real context; it gets a watermark like any other.
  Relay of a session with `fork_status` pending is deferred to the next
  turn end — don't interleave two lifecycle state machines in one tick.
- **Failed turns**: relay only fires after a *successful* turn end. A
  turn that died (limit/auth/transient — `limit-auto-resume` territory)
  must not trigger a handoff-note turn that will itself fail.

## 8. What we will NOT do

- **No automatic summarization/compression of live context.** Lossy,
  invisible when it goes wrong, and the harness already has its own
  compaction. Rejected 2026-07-15.
- **No confirmation gate, and no "ask first" mode/toggle.** Decided:
  autonomous. A confirm toggle reintroduces the discipline problem this
  plan exists to remove.
- **No window-percentage thresholds** (§4).
- **No redesign of delegation/bg-task result injection.** Injecting
  into the calling session is correct; relay keeps that session thin.
- **No auto-loading of origin-session transcripts into successors.**
  The note is the baton; the origin link is for human archaeology.

## 9. What this defers

- **Cost-aware thresholds** (pricing-table-driven, per-model): needs a
  maintained price table; absolute token thresholds capture the
  economics well enough until real usage says otherwise.
