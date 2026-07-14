# Usage-limit park & auto-resume

## 1. Problem

The user's Claude plan has a 5-hour usage window. When it runs out
mid-turn, the turn fails with a usage-limit error and the session just
stops. Every task that was in flight — a chat turn, a scheduled run, a
delegation chain — sits dead until a human notices, waits for the reset,
and manually resends. On marathon days this happens repeatedly and the
lost hours are pure waste: the work was going to continue anyway, the
only missing piece was "wait until HH:MM, then press go".

## 2. Where this sits in the failed-turn taxonomy

`harness-transient-retry.md` §2 established three dispositions for a
failed turn: auth-credential rejection → stop and flag; transient
backend error → bounded seconds-scale retry; everything else — including
the user's own usage limit — surfaces as-is. The limit was *deliberately*
excluded from retry because hammering an exhausted quota is waste. That
reasoning still holds; the correct disposition just isn't "hammer", it's
"park until the window resets, then resume once". This plan adds that
fourth disposition:

1. Auth error → stop, flag credential (`harness-credential-reauth.md`).
2. Transient backend error → exponential backoff retry, seconds scale.
3. **Usage-limit error → park; auto-resume at the reset time.** (this doc)
4. Everything else → surface as-is.

The classifiers stay mutually exclusive by construction. Today the bare
"rate limit" / "429" / "quota" tokens appear in *neither* pattern set
precisely so the user's-limit message falls through to "surface as-is";
this work claims those messages for the new classifier instead.

## 3. Goal

Any turn that fails because of the user's own usage limit resumes
automatically after the limit resets, unattended, and picks up where it
left off. Uniform coverage: chat turns, scheduled runs, delegation
children, telegram-bridge turns — everything flows through
`session_manager._run_backend`, so one turn-level mechanism covers all
of them. Both backends (claude-code and codex; codex has its own 5-hour
and weekly windows) via the same `RuntimeProfile` pattern machinery —
no `if backend ==` outside `server/harness/`.

## 4. Design points

**Detection behind the harness contract.** Mirror the auth/transient
work: `RuntimeProfile.usage_limit_patterns` for classification, plus a
per-backend hook that extracts the reset time from the error text
(Claude's CLI limit messages have historically carried one — e.g. an
epoch after a `|`, or a human "resets 3pm" phrase; codex says "try again
at …"). **Ground truth first**: no local transcript currently holds a
real sample of either CLI's limit message (searched before writing
this), so the first implementation step is to capture live samples from
both backends — the next real limit hit is itself the capture
opportunity — and derive patterns and the reset-time parser from those,
not from folklore. Patterns must stay disjoint from the auth and
transient sets.

**Park is persisted, not slept.** When a turn is classified as
limit-failed, the run loop ends the turn with a distinct `limit_paused`
outcome instead of a plain error. A pending-resume record goes to the
DB: session id, resume mode + payload (original prompt, or captured
resume id — see below), reset-at timestamp, attempt counter. APScheduler
(already the schedules engine) gets a date job at reset-at plus jitter.
On server boot, pending records rebuild their jobs — a park must survive
a restart, since the whole point is multi-hour waits.

**Resume reuses the transient-retry two-mode recovery** — the hard
problem is already solved there and must not be re-derived. No output
streamed before the failure → re-run the ORIGINAL prompt, discarding any
mid-attempt session id. Output already streamed and a resume id
captured → resume with "continue" so tools don't re-run and text doesn't
duplicate. The park record stores which mode applies and its payload at
park time.

**Still limited at wake-up → re-park, bounded.** The parsed reset may be
wrong, or the fresh window may already be eaten by whatever resumed
first. A wake-up turn that immediately limit-fails re-parks to the newly
parsed reset. Cap consecutive parks without progress (3); on exhaustion
surface a clear terminal error.

**Reset time unparseable → probe fallback.** If no reset time can be
extracted, probe on a fixed interval (30 min), bounded (12 attempts,
comfortably past one 5-hour window). A probe that limit-fails costs one
spawn and near-zero tokens.

**Stagger the herd.** Several sessions parked on the same reset must not
all fire at once — space wake-ups ~60s apart, first come first served.

**Only limit-failed turns restart.** A turn the user interrupted (Esc)
or any other error class must never be auto-restarted. The trigger is
the classifier verdict on a *failed* turn, nothing else.

**Message queue holds.** A parked session's queued messages stay queued
and drain normally after a successful resume; nothing may fire into the
exhausted window.

**User-visible and cancellable.** The session shows a "limit reached —
auto-resumes at HH:MM" state (persisted marker + WS broadcast, same
pattern as retry markers); the user can cancel a pending resume from the
UI, which deletes the record and the job. The telegram bridge relays the
park and the eventual resume, so an unattended phone user isn't staring
at silence.

**Delegations.** A parked child session is *pending*, not failed: the
parent's delegation must keep waiting through the park rather than
surfacing an error or timing out. Audit the delegation wait path for
anything that would misread a multi-hour quiet child.

**Schedules.** A parked scheduled run resumes at reset like any turn.
Occurrences that come due while the session is parked follow the
existing busy-session semantics; the parked resume is the continuation
of record.

**Testability without quota.** The e2e fake CLI (`e2e-slim.md`) emits a
canned limit-error message to exercise classify → park → persist →
wake-up through the real spawn path; scheduler time is injectable, so
the wake-up fires in test time, not wall-clock hours. Backend unit
tests cover the classifier disjointness (limit vs transient vs auth on
real captured samples), reset-time parsing, re-park bounding, and
boot-time job rebuild.

## 5. Rejected alternatives

- **Fold the limit into transient retry with long backoff** (blindly
  re-knock every 30 min): smallest diff, but wastes up to a half-hour
  after every reset, spends spawns for nothing when the reset time is
  right there in the message, and can't tell the user *when* work
  resumes. Probe-mode exists in this design only as the fallback for an
  unparseable reset, not as the mechanism.
- **External watchdog** (cron script scanning for failed sessions and
  resending): bypasses the harness boundary, duplicates session
  semantics outside the app, invisible to the UI/bridge. Contradicts
  the architecture rule that all model-runtime interaction lives behind
  `server/harness/`.
- **Credential failover** (switch to another account when limited):
  semantically dangerous (silently burning a different subscription),
  entangled with the credential system, and not what was asked.

## 6. What this does NOT do

- No management of Claude Code sessions running outside Owlery — the
  server can only resume turns it owns.
- No proactive throttling or pre-limit scheduling ("slow down as the
  window empties") — usage-tracking already visualizes the window;
  demand-side scheduling is a separate topic and a separate decision.
- No confirm-before-resume interaction. The requirement is unattended
  auto-restart; cancellability covers the control need.
- No change to transient-retry or auth-error semantics; the new
  classifier slots in beside them.
