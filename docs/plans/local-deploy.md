# Task Board — Local deploy and restart

> **Status:** design task book, implementation pending.
>
> This document specifies the final mile of the Task Board delivery pipeline:
> turning a delivered run into the **running local instance** — staging the new
> version while the old one serves, an atomic switch with a seconds-long
> restart, a health-checked automatic rollback, and durable at-most-once
> records that survive the one thing no other delivery op has to survive: the
> death of the very process executing the op.
>
> It builds directly on `task-git-delivery.md` and changes none of its rules;
> `deploy_stage` / `deploy_switch` are two new op kinds under the same §3
> at-most-once discipline, the same CAS transactions, and the same audit
> stream.

## 1. Decision

The delivery panel of a completed `git_worktree` run gains a **Deploy**
action. Deploying means: make the delivered commit the code of the local
production Owlery instance and restart it, under three named guarantees:

- **running work is never harmed** — the switch refuses (or waits) while any
  turn, task run, bg task, research job, or delivery op is active;
- **old and new versions never interleave** — the running process serves its
  own complete slot (code + venv + built dist) until the instant of an atomic
  symlink flip; no half-built `dist`, no new-schema-old-code window;
- **the switch is near-invisible** — downtime is a few seconds, the SPA
  reconnects over the existing WS retry loop and hard-reloads its assets
  exactly once, sessions and board state are durable and reappear intact.

Deployment is built on a **dual-slot (blue/green) layout**: two full
checkouts (`a/`, `b/`) under a deploy root plus a `current` symlink. The
running server lives in one slot; a deploy fully prepares the idle slot while
the server is untouched, then a tiny detached **switcher** process flips the
symlink, restarts the server, health-checks the new one, and rolls the
symlink (and a pre-switch DB snapshot) back if it fails to come up.

The paradox this design must resolve — the process that records op results is
the process being killed — is resolved by a **local journal file**: the
switcher appends each step durably; whichever server boots next (new on
success, old on rollback) reconciles the journal into the op's terminal DB
state. The journal is a local file, so boot reconciliation stays DB-and-local
only, exactly within `task-git-delivery.md` §16's S3 rule.

### 1.1 In scope

The two new op kinds, the slots layout and its one-time `owlery deploy init`
migration, the switcher and journal, quiesce gating, DB snapshot + rollback,
boot reconciliation, the `deployments` table, REST/UI surface, and test gates.

### 1.2 Out of scope (user decision, 2026-07-29)

- **Arbitrary project deploys.** v1 deploys exactly one kind of target: the
  local Owlery instance itself (the real use case: Owlery shipping Owlery).
  A generic "run this project's deploy command" is the arbitrary-plugin
  deferral of `task-board.md` §18 and stays deferred — it is an executor for
  untrusted commands, a different security problem entirely.
- **Remote hosts.** The deploy target is this machine. Deploying to another
  box is push + that box's own pull, unchanged (`repo-topology`).
- **Model-triggered switches.** No MCP verb can execute `deploy_switch` in
  v1; a production restart is human-gated (§10). Workers may only *request*.

## 2. Relationship to the delivery pipeline

| Fact | Owner |
|---|---|
| What became of the attempt's branch? | `task_deliveries` / `task_delivery_ops` (extended with 2 op kinds) |
| What version is the local instance running, and how did it get there? | **`deployments`** (new) + the deploy journal |
| Was running work protected at switch time? | quiesce gate inside `deploy_switch` (§7) |

Deploy consumes the delivery's recorded evidence exactly as push does: it
fetches `attempt_head` **directly from the board repo by local path** — no
GitHub round-trip, no network, no credential. A prior `push` op is therefore
*not* a prerequisite; pushing to the shared remote remains the durable
integration path and is unaffected. Prerequisites for deploy are only:

- the delivery is in a goal-startable state (`ready`/`delivered`/`blocked`/
  `conflicted` per the existing `_GOAL_START_STATES`);
- the delivered tree is committed: `dirty=false` (run the `commit` op first)
  and `attempt_head` present. `nothing_to_deliver` (zero commits ahead)
  refuses deploy at the verb layer exactly as it refuses push;
- no merge is required. Deploying an unmerged attempt branch is legitimate
  (try it live, merge after); the panel labels the deployed sha as
  merged/unmerged into `base_ref` so nobody mistakes a trial for an
  integration.

## 3. Deployment model: slots and `current`

```text
<deploy_root>/
  current -> a            # symlink; the ONLY path anything runs through
  a/                      # full checkout: .git, .venv, web/dist, ...
  b/                      # full checkout: idle slot, staged here
  journal.jsonl           # append-only switcher journal (§8)
  snapshots/              # pre-switch SQLite snapshots (§7.4)
```

- Each slot is a complete, self-sufficient installation: its own git clone,
  its own `.venv` created **at its final path** (venvs are not relocatable —
  building in place is what makes the flip atomic and skew-free), its own
  built `web/dist`.
- The running process resolves everything through its own slot directory
  (`__file__`-relative, as `server/main.py` already does for `dist`), so a
  symlink flip never changes what the *old* process serves — the flip is
  invisible to it. New paths only take effect at restart. This single
  property is what eliminates every old/new interleaving hazard.
- `current` is flipped with the atomic `symlink+rename` idiom, never
  edit-in-place.

### 3.1 One-time initialization: `owlery deploy init`

The existing production instance is a single checkout. `owlery deploy init
--root <deploy_root> --from <existing checkout>` performs the one-time
migration, offline and idempotent:

1. clone `--from` into `<root>/a`, check out the same commit, create
   `<root>/a/.venv`, build `web/dist`;
2. clone again into `<root>/b` (staged lazily on first deploy otherwise);
3. create `current -> a`;
4. print the new canonical start command:
   `<root>/current/.venv/bin/owlery serve` — the user's launchd plist /
   shell alias changes once, here, and never again.

The original checkout is left untouched (the user retires it manually).
Deploy is **fail-closed** until this has happened: with no configured
`deploy_root` (§9), or a server whose own executable path does not resolve
through `<root>/current`, every deploy verb returns
`blocked(deploy_not_initialized)` / `blocked(not_running_via_current)` with
the exact command to fix — Owlery never guesses a layout and never deploys an
instance it could not restart correctly. `settings.debug` (uvicorn reload)
also refuses: a reloading dev server must not be production-switched.

## 4. Op model

Two new `task_delivery_ops` kinds, same table, same `UNIQUE(source_key)`,
same one-running partial index, same start/finish CAS:

| kind | external | source_key | nature |
|---|---|---|---|
| `deploy_stage` | 0 | `task:<t>:run:<r>:delivery:deploy_stage` | local, idempotent, re-runnable |
| `deploy_switch` | 1 | `task:<t>:run:<r>:delivery:deploy_switch` | at-most-once, never auto-retried |

`deploy_stage` is local and idempotent like `commit`: re-running it re-stages
the idle slot from scratch. `deploy_switch` is the sharpest external op in
the system — sharper than PR creation, because its side effect includes the
recorder's own death. Its `interrupted` handling is therefore journal-backed
(§8) rather than human-only, but the §3 rule is intact: a repeat is always an
explicit new `:retry:<n>` op, never a tick.

Delivery status transitions reuse §4.1.1 unchanged: ops start via the same
`ready→delivering` CAS family (from any `_GOAL_START_STATES` member),
succeed back to `ready`/`delivered`, fail to `blocked`. No new delivery
status is introduced; deployment's own lifecycle lives in `deployments` (§6).

A **global deploy lock** (a partial unique index on `deployments.state IN
('staging','switching')`) serializes deploys across all boards and
deliveries: one instance, one pipeline at a time. A second attempt gets
`blocked(deploy_locked)` naming the holder.

## 5. `deploy_stage` — everything slow, while the server is up

Staging prepares the idle slot completely; the running server is not touched
and running tasks are not even aware of it.

1. resolve the idle slot (the one `current` does not point to);
2. `git fetch <board repo path> <attempt_head>` into the slot clone, then
   `git checkout --detach <attempt_head>` — the exact reviewed sha, never a
   branch name that could move;
3. sync the slot venv: `<slot>/.venv/bin/pip install -e <slot>` (create the
   venv if absent). The venv lives at its final path from the start;
4. build the frontend in the slot: `cd <slot>/web && bun install && bun run
   build`;
5. sanity probe: `<slot>/.venv/bin/python -c "import server.main"` — an
   import-crash is caught at stage time, not at switch time;
6. record a `deployments` row `state='staged'` with slot, sha, delivery/op
   ids, and fold `staged_sha`/`staged_slot` into the op result.

Any step failing marks the op `failed` (`reason_kind='stage_failed'`, full
command output in the op error, secret-free) and the delivery `blocked`; the
running instance is untouched by construction. Staging is bounded per
subprocess by `deploy_stage_timeout_seconds` (§9) — `bun install`/`pip` are
allowed minutes, unlike the 60s git ops.

Re-staging (a new attempt sha over an old staged slot) simply overwrites: the
prior `deployments` row becomes `superseded`.

## 6. `deployments` — what is live, durably

```sql
CREATE TABLE deployments (
    id TEXT PRIMARY KEY,
    delivery_id TEXT REFERENCES task_deliveries(id) ON DELETE SET NULL,
    task_id TEXT,                        -- denormalized for display; survives delivery GC
    op_id TEXT,                          -- the deploy_switch op, once one exists
    slot TEXT NOT NULL,                  -- 'a' | 'b'
    sha TEXT NOT NULL,
    source_repo TEXT NOT NULL,
    state TEXT NOT NULL,                 -- staged|switching|live|rolled_back|superseded|failed
    journal TEXT,                        -- JSON: final journal excerpt for this deploy
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX deployments_one_live ON deployments(state) WHERE state = 'live';
CREATE UNIQUE INDEX deployments_one_active ON deployments(state)
  WHERE state IN ('staging','switching');
```

Exactly one `live` row at any time answers "what is this instance running?";
`/health` exposes its sha and slot so the answer is visible without the DB.
History (who deployed what, when, rolled back why) is the row sequence plus
the op audit trail — no separate log.

The previous `live` row becomes `superseded` on a successful switch and is
the target of the standalone **Rollback** action (§11): rolling back is
planning a new `deploy_switch` op that flips to the still-intact previous
slot — same machinery, no special path.

## 7. `deploy_switch` — the at-most-once flip

### 7.1 Quiesce gate

The switch begins with an idleness check inside the start-op CAS window, and
refuses (`blocked(not_idle)`, listing exactly what is busy) if any of:

- a session turn is active anywhere (`session_manager` active-turn census,
  including parked-resume pending);
- a task run is `running`/`claimed` on any board, or the dispatcher has a
  claim in flight;
- a bg task, research job, or delegation child turn is running;
- another delivery op is `running` (the one-running index already enforces
  the per-delivery case; the census covers other deliveries);
- pending `session_injections` exist (they would be consumed mid-restart).

Two softer modes, both explicit request parameters:

- `drain=true`: pause the task dispatcher and bridge intake, stop starting
  queued turns, then wait up to `deploy_quiesce_timeout_seconds` for the
  census to reach zero; on timeout, un-pause and return `blocked(not_idle)`.
- `switch_when_idle=true`: plan the op and hold it in memory until the census
  first reaches zero, then execute. The hold does not survive a restart: if
  the server dies first the op is still `planned`, and boot leaves `planned`
  ops alone (per §3 nothing auto-starts); the UI re-offers the trigger.

There is **no** force override for the quiesce gate in v1. Killing running
turns to deploy faster is precisely the harm this feature promises not to do;
an operator in a genuine hurry can interrupt turns through the existing UI
first, visibly and deliberately.

### 7.2 Handoff

Once quiesced (and re-verified immediately before handoff):

1. checkpoint SQLite (`PRAGMA wal_checkpoint(TRUNCATE)`) and copy `owlery.db`
   to `<root>/snapshots/<op_id>.db` (§7.4);
2. write journal step `handoff` (op id, from-slot, to-slot, shas, pid, port);
3. mark the op `running` result-partial with `journal_ref`, commit the CAS;
4. spawn the **switcher** fully detached — double-fork + `setsid`, own
   process group, stdio to `<root>/switcher.log`. Detachment is not optional
   hygiene: this machine's incident history (`multi-agent-contention`) is
   exactly what a switcher reaped by its parent's process-group cleanup
   would recreate. The switcher binary is `<old slot>/.venv/bin/owlery
   deploy-switch --journal ... --op ...` — the *currently trusted* version's
   code, stdlib-only, no imports from the new slot;
5. broadcast WS `server_restarting` (§11), then initiate the normal graceful
   shutdown path (the same lifespan teardown a SIGTERM takes today).

### 7.3 The switcher

Small, sequential, journaling every step before performing it:

1. wait for the old pid to exit and the port to free (timeout
   `deploy_switch_timeout_seconds`; on timeout journal `old_wont_die` and
   exit — flip never happened, boot of the old server reconciles the op as
   `failed(old_wont_die)`; the switcher never SIGKILLs a server that is
   still finishing durable writes);
2. flip `current` to the staged slot (atomic rename); journal `flip_done`;
3. start `<root>/current/.venv/bin/owlery serve` detached, same argv/env
   contract as the old process (captured in the journal at handoff);
4. poll `GET /health` (unauthenticated by design) until it answers with the
   new sha, up to `deploy_health_timeout_seconds`;
5. **success**: journal `switched_ok`, exit 0;
6. **failure** (no health in time, or the new process exited): journal
   `rollback_begin`; stop the new process if alive (SIGTERM, then KILL after
   grace — the new instance holds no user work: probation, §7.5, kept it
   from starting any); flip `current` back; restore the DB snapshot over
   `owlery.db` (§7.4); start the old slot's server; journal `rolled_back`
   with the captured reason; exit 1.

### 7.4 The DB snapshot

The snapshot exists for exactly one window: the new version may migrate the
schema (`PRAGMA user_version` steps) or write boot-recovery rows before
health is confirmed; rolling back to old code on a new schema is the
old/new clash in database form. Restoring the pre-switch snapshot on rollback
makes the rollback total. It is safe **only** because of quiesce + probation:
nothing with durable external meaning runs between snapshot and health
verdict, so the discarded writes are recomputable recovery bookkeeping — the
old server re-derives them at its own boot. Snapshots are pruned to
`deploy_keep_snapshots` (§9); a rollback never deletes the snapshot it used.

### 7.5 Boot probation

A server booting while the journal's last entry for a known op is
non-terminal (`flip_done` but not `switched_ok`/`rolled_back`) is a **deploy
candidate under probation**: it runs its normal DB-only boot recovery, but
holds the task dispatcher, injection drain, bridges, and scheduler paused
until the journal goes terminal or `deploy_health_timeout_seconds` elapses
(the pause primitive already exists —
`session_injection_dispatch_paused`). Probation is what licenses §7.4:
the health window performs no user-visible work that a snapshot restore
would silently undo or double-deliver.

## 8. Journal and boot reconciliation

`<root>/journal.jsonl` is append-only, fsynced per line, each line
`{ts, op_id, step, detail}`. Boot delivery-reconciliation
(`task-git-delivery.md` §16) gains one step, still DB-and-local-file only:

For any `deploy_switch` op left `running` in the DB, read the journal for
that op id:

| journal tail | reconciliation |
|---|---|
| `switched_ok` | op `succeeded`; `deployments` row → `live` (previous live → `superseded`); delivery folded (`deployed_sha`, slot) |
| `rolled_back(reason)` | op `failed(reason)`; deployment → `rolled_back`; delivery `blocked(health_failed)` with the journal detail |
| `old_wont_die` | op `failed(old_wont_die)`; deployment stays `staged` (flip never happened); delivery `blocked` |
| `flip_done`, non-terminal, journal fresh | probation (§7.5): leave the op `running`, wait for the switcher's verdict |
| `handoff` only, or stale non-terminal | op `interrupted`, delivery `blocked(interrupted)`, deployment → `failed`; a human inspects `current`, the journal, and `switcher.log` — never an auto-repair |

The `switched_ok` row is normally written *after* the new server is already
up, so the common path is: new server boots under probation, the switcher
sees health, journals `switched_ok`, and the probationary server finalizes
the op the moment it observes the terminal journal line (a bounded local
poll, not a network call). Every other server boot — no non-terminal deploy
ops — skips all of this at the cost of one `stat()`.

## 9. Configuration

Global (`server/config.py`) — deployment is an instance property, not a
board property:

- `deploy_root` (default unset → feature disabled, verbs return
  `blocked(deploy_not_initialized)`)
- `deploy_stage_timeout_seconds` (default 600; per staging subprocess)
- `deploy_switch_timeout_seconds` (default 30; old-exit + port-free wait)
- `deploy_health_timeout_seconds` (default 60)
- `deploy_quiesce_timeout_seconds` (default 120; `drain=true` wait cap)
- `deploy_keep_snapshots` (default 5)

Board-level: one flag, `allow_local_deploy` (default **false**). A board
whose runs may touch the production instance is an explicit decision; the
trial board flips it on. Everything else inherits the global instance config.

New `reason_kind` values (same column, same semantics):
`deploy_not_initialized`, `not_running_via_current`, `deploy_locked`,
`not_idle`, `stage_failed`, `health_failed`, `old_wont_die`.

## 10. Permissions and destructive-action protection

- `deploy_stage`: human via REST/UI, or an orchestrator Agent via MCP —
  staging is side-effect-free for the running instance.
- `deploy_switch`: **human via REST/UI only** in v1. No MCP verb executes a
  switch; a worker's `request_delivery(note)` may carry a deploy hint, an
  orchestrator may stage, but restarting production on model initiative is
  out until a real trust decision says otherwise (§13 of
  `task-git-delivery.md` applies: the repository counts flags, not prompt
  claims).
- No confirmation flag skips the quiesce gate (§7.1 — deliberately no
  force). The one destructive confirmation is `rollback` of a live
  deployment (`confirm_rollback=true`), because it discards the running
  version, and it reuses the same typed-confirmation surfacing as §13.
- Secret hygiene unchanged: journal, op results, and `deployments.journal`
  carry paths, shas, pids, and exit codes — never tokens (no credential is
  involved anywhere in this pipeline: local-path fetch, local build, local
  restart).

## 11. REST, WS, and browser experience

```text
POST /api/tasks/{t}/runs/{r}/delivery/deploy/stage
POST /api/tasks/{t}/runs/{r}/delivery/deploy/switch      # {drain?, switch_when_idle?}
GET  /api/deployments                                    # history + live row
POST /api/deployments/{id}/rollback                      # {confirm_rollback}
```

All verbs are CAS-guarded, 409 + current state on a lost race, broadcast
`task_event` rows like every other op. Additions:

- **WS `server_restarting`** broadcast at handoff. The SPA shows a "deploying
  — back in a moment" banner and lets the existing reconnect loop do its
  job.
- **Asset-skew hard reload**: `/health` (and the WS hello) gains the build
  sha. When the client reconnects and sees a sha different from the one it
  booted with, it does one `location.reload()`. This closes the classic
  post-deploy stale-chunk 404 and is most of what "无感" means in practice.
- **Delivery panel**: a Deploy section after the existing action row — staged
  sha/slot card, Stage / Switch buttons with prerequisite-disabled states,
  the busy-census list when `not_idle`, and the journal tail when blocked.
- **Deployments page section** (inside existing settings/usage surface, no
  new top-level route): the `deployments` history, live sha/slot, Rollback
  with typed confirmation.

## 12. Failure matrix

Two shapes of refusal appear below. **Preconditions** — board not opted in
(§9), no `deploy_root` / not via `current` / debug (§3.1), and the §2 git
prerequisites (settled state, `dirty=false`, `attempt_head` present,
`nothing_to_deliver`) — are refused *before* any op is planned: a 409 that
names the fix, creating no op and mutating no delivery, exactly like the
existing git-delivery preconditions. **Op outcomes** — everything from the
global-lock loss onward — are durable: an op row plus a delivery status change.

| Failure | Outcome |
|---|---|
| Deploy verb on a board without `allow_local_deploy`, or with no `deploy_root` / not started via `current` / debug mode | precondition 409 naming the fix (`deploy_not_initialized \| not_running_via_current`); **no op, no delivery mutation** |
| §2 git prerequisite unmet (unsettled state / dirty / no `attempt_head` / `nothing_to_deliver`) | precondition 409 naming the fix; **no op, no delivery mutation** |
| Second deploy while one is staging/switching | op `failed(deploy_locked)` + delivery `blocked(deploy_locked)` naming the holder |
| Stage: fetch/build/venv/import-probe fails (or a foreign idle slot) | op `failed(stage_failed)` + full output; delivery `blocked(stage_failed)`; running instance untouched |
| Switch requested while work is running | `blocked(not_idle)` + busy census; `drain` waits bounded, then same |
| Server dies after `handoff` journal, switcher never ran | boot: op `interrupted`, journal tail shown; human inspects |
| Old process won't exit in time | switcher exits without flipping; op `failed(old_wont_die)`; nothing changed |
| New server never healthy | switcher flips back, restores DB snapshot, restarts old; op `failed(health_failed)`; deployment `rolled_back` |
| Switcher itself dies mid-flip | journal tail `flip_done` stale → op `interrupted`; `current` state + `switcher.log` are the evidence; never auto-repaired |
| Rollback requested later | new `deploy_switch` op to the superseded slot, `confirm_rollback` required |
| Crash between op-terminal commit and origin injection | covered by the existing `:delivery:terminal` boot reconstruction (B2) — deploy adds no new notification path |

## 13. Invariants

1. The running instance is only ever mutated by an atomic `current` flip
   performed while no Owlery server process is between "old exited" and "new
   booted" — never by writing into a live slot.
2. Exactly one `deployments` row is `live`; `/health` reports its sha; the
   flip and the DB row are reconciled by the journal on every boot.
3. `deploy_switch` is at-most-once: uniquely keyed, journal-backed, never
   auto-retried; every repeat is an explicit new op.
4. The quiesce gate cannot be forced; no turn, task run, bg task, or pending
   injection is ever killed or consumed by a deploy.
5. Rollback-on-failed-health is total: symlink, DB snapshot, and process all
   return to the pre-switch state, and probation guarantees the discarded
   writes were recomputable bookkeeping only.
6. The switcher runs the old (trusted) version's code, fully detached in its
   own process group and session; new-slot code first executes as the new
   server, never inside the op path.
7. Boot reconciliation for deploys reads the DB and local files only; no
   network I/O enters the boot barrier (S3 upheld).
8. No credential exists anywhere in the deploy pipeline; journals and op
   results are secret-free by construction.
9. Deploy never touches the board repo, run worktrees, or any path outside
   `deploy_root` (+ the DB snapshot dir inside it).

## 14. Test plan (gates)

All four `CLAUDE.md` suites stay green; this feature only adds.

**Backend unit (`tests/test_task_deploy_*.py`)** — all against temp dirs and
a fake instance (a stub `owlery serve` script that binds a port and answers
`/health`); no real production paths:

- init: slot layout creation, idempotent re-run, canonical command output;
  fail-closed verbs without `deploy_root` / via-`current` / debug guards;
- stage: local-path fetch of the exact sha, detached checkout, venv-at-final-
  path, import probe catches a broken slot, `stage_failed` capture,
  supersede-on-restage, global lock;
- switch: quiesce census (each busy source individually), drain
  pause/resume, `switch_when_idle` non-durability across restart, snapshot
  checkpoint+copy, handoff journal, detachment (own pgid/sid);
- switcher (driven standalone against the fake instance): old-exit wait,
  `old_wont_die` no-flip path, atomic flip, health success, health-failure
  rollback (flip-back + snapshot restore + old restart), journal fsync
  ordering (step written before action);
- boot reconciliation: every journal-tail row of the §8 table, probation
  holds dispatcher/drain until terminal or timeout, stale-journal →
  interrupted, `stat()`-only fast path when no deploy op is open;
- `deployments` uniqueness (one live, one active), rollback op planning,
  `confirm_rollback` gating;
- op-model conformance: source keys, one-running index, CAS races, §4.1.1
  table extension rows, event emission.

**Frontend unit (Vitest)**: deploy panel state machine, busy-census
rendering, `server_restarting` banner, build-sha mismatch → single reload.

**E2E (Playwright, fake CLI)**: stage against a temp instance through the
real UI; switch verbs assert the quiesce refusal and the staged→switch
button flow up to handoff. The actual restart is **not** performed under the
shared e2e server (it would kill the suite's own backend); the full
switch/rollback path is covered by the standalone switcher integration tests
above, which drive real processes on a real port. This split is stated here
so nobody "fixes" the e2e gap by restarting the test server.

**Real-CLI / quota**: none — no model call anywhere in this feature.

## 15. What this deliberately defers

- generic deploy commands for arbitrary projects (§1.2 — a different trust
  problem);
- remote-host deploys (push + pull remains the path);
- MCP-triggered `deploy_switch` (needs an explicit trust decision);
- auto-deploy on merge/delivery (same reason `task-git-delivery.md` §23
  defers auto-deliver);
- zero-downtime socket handover (SO_REUSEPORT / fd passing): seconds of
  downtime with WS reconnect is the honest cost; true zero-downtime is a
  large mechanism for a single-user instance and earns nothing today.

## 16. Acceptance criteria

Done only when:

1. `owlery deploy init` converts a real checkout to slots and the instance
   runs via `current`, with all fail-closed guards demonstrably firing
   beforehand;
2. staging a delivered run prepares the idle slot completely while the
   running instance handles work throughout, and a broken build never leaves
   staging;
3. a switch on an idle instance completes with seconds of downtime, the SPA
   reconnects and hard-reloads once, `/health` reports the new sha, and the
   op/deployment/journal all agree;
4. a switch on a busy instance refuses with the exact busy census; drain
   waits and then refuses honestly; nothing running is ever harmed;
5. a deliberately broken new version (failing health) rolls back
   automatically — symlink, DB snapshot, old server — and the op records
   `failed(health_failed)` with the journal;
6. every crash point (pre-handoff, pre-flip, post-flip, post-health) boot-
   reconciles to the §8 table with no auto-retry and no network I/O;
7. rollback of a live deployment works as an ordinary confirmed op;
8. all four suites pass and independent review approves.

## 17. Implementation order

1. **layout + init + config**: slots, `deploy init`, fail-closed guards,
   `deployments` table migration — no ops yet;
2. **stage op**: local pipeline + lock + supersede, full unit coverage;
3. **switcher + journal**: the standalone `deploy-switch` command driven by
   integration tests against the fake instance — before any server wiring;
4. **switch op + quiesce + probation + boot reconciliation**: the full §7/§8
   machinery in the coordinator and lifespan;
5. **REST + WS + UI**: verbs, `server_restarting`, build-sha reload, panel,
   deployments section, contracts regeneration;
6. **full matrix + docs + independent review**.
