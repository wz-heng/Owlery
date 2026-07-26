# Durable async execution and session injection

> Implemented 2026-07-24. This document is authoritative for delegation
> round persistence and for delivery of delegation, background-task, and
> research results into sessions. It supersedes the in-memory delivery details
> in `agent-collaboration.md` §3/§5 and `native-deep-research.md` §6/§7.

## 1. Decision

Owlery does **not** have a generic `work_items`/`runs` table. Background shell
tasks, deep research, schedules, and delegation rounds have different execution
semantics and keep their own state.

Two genuinely shared concepts are persisted separately:

1. `delegation_runs` records one row per round in a delegated child session.
   The public `delegation_id` is still the child session id; `run_id` identifies
   a particular initial/follow-up round.
2. `session_injections` is a narrow durable outbox for one operation only:
   turning a system-produced event into one user-message row in a target
   session. Delegation questions/terminals, bg results, and research reports use
   it. It is not a webhook, bridge, or general workflow queue.

## 2. Why the two tables are separate

Execution and delivery are different facts:

- A delegation can be `completed` while its reply is queued behind an active
  parent turn.
- A process can die after work completes but before the parent transcript sees
  the result.
- Delivery into the transcript can succeed while the model turn consuming that
  message is later interrupted.

Conflating those states either loses results or claims more certainty than the
system has.

## 3. Delegation rounds

`delegation_runs` is append-only per child session:

```text
run_id PK
delegation_id FK -> sessions.id
round_no UNIQUE per delegation_id
request
start_seq
state: running | completed | failed | cancelled | interrupted
error
created_at
finished_at
```

`start_seq` is the last message sequence present before the round begins. The
round's durable result is reconstructed from later assistant text rows. In the
real stream path, `SessionManager` persists each event before broadcasting it to
`DelegationManager`, so a terminal event cannot be observed before its result
text is queryable.

On restart, `running` becomes `interrupted`, not `failed`: the child process is
gone, but external side effects may already have happened. Owlery never retries
an interrupted round automatically. It sends the caller an interruption notice
with any partial assistant text that was persisted, then archives the child.

Follow-ups create a new `run_id` and increment `round_no`; prior outcomes are
never overwritten. `GET /api/sessions/{parent}/delegations/{child}/runs`
exposes the complete history.

## 4. Session injection protocol

`session_injections` stores:

```text
id PK
source_key UNIQUE
session_id FK -> sessions.id
prompt
status: pending | delivered | failed
created_at
delivered_at
error
```

Producer keys are stable and event-specific:

- `delegation:<run_id>:terminal`
- `delegation:<run_id>:question:<question_id>`
- `bg:<task_id>`
- `research:<job_id>`

The protocol is:

1. Insert or find the unique outbox row and commit it.
2. Queue `QueuedPrompt(prompt, injection_id)` through normal session ordering.
3. When the prompt becomes a user message, insert `messages.injection_id`. A
   SQLite trigger validates the pending target and changes the outbox row to
   `delivered` inside that same INSERT statement. This statement-level boundary
   matters because Owlery shares one `aiosqlite` connection across coroutines;
   two Python `execute()` calls would not prevent an unrelated commit between
   them.
4. A partial unique index on `messages.injection_id` prevents two transcript
   rows for one source event below the model layer.
5. On boot, reconcile any legacy `pending` row that already has a matching
   transcript message, then replay only the remaining `pending` rows. Under the
   trigger-based writer, a committed transcript row necessarily has a committed
   `delivered` acknowledgement.

Boot and shutdown add a lifecycle barrier around that protocol:

1. Immediately after loading sessions, boot pauses injection dispatch.
2. All managers bind their listeners, then bg/delegation/research reconcile
   execution state and create any missing outbox sources without starting
   model turns.
3. Pending events aimed at delegation parents that are themselves being
   interrupted are committed directly to those parents' transcripts. They are
   acknowledged as delivered but deliberately left unconsumed; restarting the
   parent model would revive non-idempotent work after declaring it interrupted.
4. Recovered delegation sessions archive only after the whole nested tree has
   been materialized.
5. One centralized resume drains remaining pending intents into live sessions.
6. Shutdown pauses dispatch before stopping producers. Terminal bg/research
   state and outbox rows may still commit, but no new consuming turn starts;
   the next boot drains them.

`delivered` means “the source event is durably present in the transcript.” It
does not mean the subsequent model turn finished. Owlery does not automatically
rerun an interrupted consuming turn, because that turn may itself have produced
non-idempotent effects.

## 5. Subsystem integration

- **Delegation:** questions and terminal replies/errors use the outbox. Terminal
  output is rebuilt from child messages, not from the live `captured_text`
  cache. Boot recovery also repairs a terminal run whose outbox source was not
  created before the prior process stopped.
- **Background tasks:** every terminal state, including boot-recovered
  `interrupted`, creates `bg:<task_id>` exactly once. `delivery_required` is a
  schema-cutover marker: new rows are `1` and terminal rows missing their source
  are repaired on boot. Migrated historical terminal rows are backfilled to `0`
  so old command output is not replayed; migrated `running`/`pending` rows are
  backfilled to `1` so their boot-time `interrupted` outcome reaches the caller.
- **Research:** a report file is written atomically before the job becomes
  `completed`; delivery uses `research:<job_id>`. Boot recovery converts legacy
  `completed + injection_status=pending` rows into outbox entries. The old
  `injection_status` column remains a compatibility mirror for one release.

## 6. Invariants

1. `interrupted` is distinct from `failed`: interruption means the business
   outcome may be unknown.
2. No restart may leave a prior-process execution falsely `running`.
3. A terminal result must be reconstructible from a durable source.
4. Every system-produced session turn persists its intent before entering an
   in-memory queue.
5. Delivery acknowledgement and transcript insertion commit atomically.
6. Duplicate source events are rejected by a database uniqueness constraint,
   not by asking the model to notice repeated prose markers.
7. No interrupted non-idempotent work or consuming model turn is retried
   automatically.
8. If execution terminalization and outbox creation cannot share one statement,
   boot recovery must reconstruct any missing source from durable execution
   state; a terminal status alone is not proof that delivery was scheduled.
9. Boot recovery and shutdown run with dispatch paused. No model turn may start
   between domain-state reconciliation and the centralized drain, or after
   teardown begins.
