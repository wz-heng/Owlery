# Task Board — Git delivery closure

> **Status:** design task book, implementation pending.
>
> This document specifies the closing half of the Task Board's `git_worktree`
> workspace mode: turning a completed worker attempt's branch into a durably
> tracked, human-visible, at-most-once **delivery** — commit ownership, branch
> push, pull-request creation, an optional safe merge, conflict/failure states,
> worktree teardown, and crash recovery.
>
> It builds directly on `task-board.md` (def9d08) and the durable outbox
> semantics of `durable-session-injections.md`. It changes **no** existing
> lifecycle rule; it adds a new post-terminal delivery phase gated behind an
> explicit decision, exactly the capability `task-board.md` §7.3 / §18 deferred
> as *"automatic Git merging, PR creation, or worktree deletion."*

## 1. Decision

The Task Board gains a first-class, durable **delivery** object attached to a
completed `git_worktree` run. A delivery answers one question: *what happened to
the code this attempt produced?* It records, as at-most-once side-effecting
operations that are **never** automatically retried:

- **accept**: capture the run's base/head identity, dirty state, and diffstat as
  the immutable delivery baseline;
- **commit**: adopt any uncommitted worktree changes into a single owned commit
  with deterministic authorship, or record that the worker already committed;
- **push**: publish the attempt branch to a configured Git remote;
- **pull_request**: open a PR/MR on the hosting platform via an existing
  connector credential;
- **merge** (optional, opt-in): a conservative, non-destructive integration into
  the base branch;
- **teardown**: remove the worktree registration and, per retention policy,
  delete or keep the local attempt branch and directory.

The delivery is a durable **intent-and-effect** record, parallel to how
`task_runs` is the execution truth and `session_injections` is the delivery
truth for notifications. It does not reopen `done`; a completed run stays
completed. Delivery is a distinct, human/orchestrator-gated action on top of it.

### 1.1 In scope

Everything in §1 for `git_worktree` runs, plus its REST/MCP/UI surface,
boot/shutdown ordering, migration, failure matrix, invariants, and test gates.

### 1.2 Out of scope (user decision, 2026-07-26)

- Budgets and automatic model/provider routing — explicitly **not** built now.
- The other `task-board.md` §18 deferrals (cross-board dependencies, recurring
  task definitions, arbitrary webhook/plugin steps, org/role permissions).
- `shared` and `copy` workspace modes get **no** delivery pipeline: `shared`
  edits a live directory in place and `copy` is an inspect-only side-effect
  sandbox. Only `git_worktree` produces a deliverable branch. See §23.

## 2. Relationship to existing subsystems

| Fact | Owner |
|---|---|
| Did the attempt succeed, and what did the worker report? | `task_runs` (unchanged) |
| What became of that attempt's branch/commits? | **`task_deliveries` + `task_delivery_ops`** (new) |
| Did a terminal notification reach the origin transcript once? | `session_injections` (unchanged) |
| Which credential authorizes a hosting-platform write? | `connector_manager` (reused) |

Delivery consumes, but never mutates, the run's recorded git evidence
(`workspaces.inspect_git_workspace` → `{branch, head, porcelain}` and the run
metadata `base_head`/`repository`/`branch` written by `prepare_workspace`). It
reuses `connector_manager.get_access_token()` for platform auth and
`workspaces.cleanup_private_workspace` for filesystem removal — the latter
extended so it stops leaking worktree registrations (§10).

This feature therefore also requires a small extension on the **prepare** side:
`prepare_workspace` must, at worktree-creation time, capture the base **branch
name** alongside the `base_head` it already records (§5, B1). Accept only reads
that snapshot; it never re-derives the base branch from the source repo's live
`HEAD`. def9d08 runs created before this change have no captured `base_ref` and
are handled by the legacy path in §5.

## 3. Core principle: outbound effects are at-most-once, never auto-retried

This is the non-negotiable rule the user named. Every operation that mutates
state outside Owlery's own database — writing to a remote, opening a PR,
performing a merge, deleting a branch — is:

1. **planned** as a durable `task_delivery_ops` row with a stable, unique
   `source_key` (mirroring `session_injections.source_key`);
2. **executed** exactly once against the outside world;
3. **recorded** terminally as `succeeded` or `failed` with the platform's
   response captured verbatim;
4. **never** re-executed automatically — not by a dispatcher tick, not by a
   heartbeat, not by boot recovery. A process that dies mid-op leaves the op
   `interrupted`, whose meaning is precisely *"the external effect may or may not
   have happened; a human must look."*

Retry is always an explicit, human/orchestrator-initiated **new op** with a new
`source_key`, after the operator has inspected the outside world. This is the
same stance `durable-session-injections.md` §6 invariant 7 takes for consuming
turns, generalized to outbound side effects. Local, idempotent, fully
reconstructable operations (reading git state, computing a diffstat, tearing
down a local worktree whose remote push already succeeded) are **not** ops under
this rule; only externally-observable mutations are.

## 4. Concepts and identity

### 4.1 Delivery

A `task_deliveries` row is created the first time a delivery action is requested
for a **completed** `git_worktree` run. Exactly one delivery exists per run
(`UNIQUE(run_id)`); it is the durable head-state of that attempt's code fate.

Delivery status:

```text
pending -> preparing -> ready -> delivering -> delivered
                 \                    |
                  \-> conflicted      \-> blocked
                   \-> failed
```

- `pending`: created, baseline not yet captured.
- `preparing`: capturing base/head/dirty/diffstat (local, idempotent).
- `ready`: baseline captured; awaiting a delivery decision (push/PR/merge).
- `delivering`: one outbound op is in flight.
- `delivered`: the requested delivery goal (push and/or PR and/or merge) reached
  its terminal success; teardown may still be pending per retention policy.
- `conflicted`: a merge could not proceed without a non-fast-forward/manual
  resolution. Never force-resolved. Requires human action.
- `blocked`: an op failed, a credential/remote is missing, a destructive guard
  refused, or the process was interrupted mid-op. Never auto-advances.
- `failed`: baseline capture itself was impossible (e.g. the worktree no longer
  exists and no push had occurred). Terminal-but-reopenable via a new op.

`delivered`, `conflicted`, `blocked`, `failed` are all human-actionable; none
starts a new outbound op on its own.

#### 4.1.1 Normative transition table

Every delivery status change goes through one of these guarded operations. A
transition not listed here returns 409 and leaves the durable row unchanged —
exactly the discipline `task-board.md` §3.2.1 applies to tasks. The human/agent
"new action after a terminal state" rows are explicit so recovery from
`blocked`/`conflicted`/`failed` is never an ad-hoc status write.

| From | Operation | To | Guard |
|---|---|---|---|
| (none) | `accept` on a completed `git_worktree` run | `preparing` | run is `completed`, mode `git_worktree`, no delivery yet |
| `preparing` | baseline captured | `ready` | base/attempt/dirty/diffstat recorded |
| `preparing` | baseline impossible (worktree gone, no prior push) | `failed` | reason `workspace_gone_no_effect` |
| `preparing` | base branch unresolvable for a legacy run | `blocked` | reason `base_ambiguous` (§5) |
| `ready` | `commit`/`push`/`pull_request`/`merge` op starts | `delivering` | start-op CAS; no other running op |
| `delivering` | outbound op succeeds, goal reached | `delivered` | finish-op CAS |
| `delivering` | outbound op succeeds, more requested ops remain | `ready` | finish-op CAS |
| `delivering` | merge cannot fast-forward / conflicts | `conflicted` | `git merge --abort` clean |
| `delivering` | op fails / missing remote / missing-or-ambiguous connector / destructive-refused | `blocked` | `reason_kind` set |
| `delivering` | process died mid-op | `blocked` | op → `interrupted`, `reason_kind='interrupted'` (boot, §16) |
| `blocked` | human/orchestrator plans a **new** op (fresh `source_key`) | `delivering` | precondition for that op met (e.g. push before PR) |
| `conflicted` | human/orchestrator plans a **new** op after resolving | `delivering` | precondition met |
| `failed` | human/orchestrator plans a **new** op (e.g. re-`accept` after restoring the worktree) | `preparing`/`delivering` | precondition met |
| `ready`/`delivered`/`blocked`/`conflicted`/`failed` | `teardown` op | same status | local teardown does not change delivery goal-status; retention guard (§10) |

Re-running `accept` on an existing delivery is idempotent: it re-reads the
baseline (refreshing only the non-`base` fields per §6/N1) and returns the
current row; it never rewinds a `delivering`/terminal delivery to `preparing`.

### 4.2 Delivery op

`task_delivery_ops` is append-only. One row is one at-most-once outbound
operation of one `kind ∈ {commit, push, pull_request, merge, branch_delete,
worktree_remove}` for one delivery. Op state mirrors run state:

```text
planned | running | succeeded | failed | interrupted
```

`interrupted` = the worker/host process disappeared while the op was `running`;
the external effect is unknown and is **never** retried automatically. A repeat
of the same logical intent is a **new** op row with a fresh `source_key`
suffix (`:retry:<n>`), created only by explicit human/orchestrator action.

Note `commit`, `branch_delete`, and `worktree_remove` operate on the **local**
repo. They are still logged as ops for a complete audit trail and to keep the
"push already happened, then teardown deleted the local branch" sequence
inspectable, but only `push`, `pull_request`, and `merge` (against a remote or
shared base) carry true external-side-effect weight under §3. A purely local op
that fails is safe to re-run; an external one is not.

## 5. Change acceptance (`accept` / baseline)

The first delivery action on a completed run captures an immutable baseline into
`task_deliveries`, computed from the run's worktree if it still exists, else from
run metadata:

- `base_ref` and `base_head`: the branch and commit the worktree was cut from.
  Both are **captured at `prepare_workspace` time**, never re-derived at accept.
  When the worktree is created, `prepare_workspace` records — alongside the
  `base_head` it already writes — the base branch via `git symbolic-ref --short
  HEAD` (a detached source `HEAD` records an **empty** `base_ref`). Accept copies
  these from run metadata as a read-only snapshot. This is essential: the source
  repo's live `HEAD` may have moved to a different branch by the time a human
  triggers delivery, so resolving the base branch at accept time would attribute
  the attempt to the wrong branch. See B1 in §2 for the prepare-side extension.
- `attempt_branch`: `owlery/task-<task>-run-<attempt>` (from run metadata);
- `attempt_head`: the worktree's current `HEAD` (`inspect_git_workspace.head`);
- `dirty`: whether `git status --porcelain` is non-empty at accept time;
- `diffstat`: files changed / insertions / deletions between `base_head` and the
  effective delivered tree, stored as structured JSON;
- `commits_ahead`: count of commits on `attempt_branch` not in `base_head`.

Only the `base_ref`/`base_head` pair is immutable once captured. `attempt_head`,
`commits_ahead`, and `diffstat` describe the delivered tree and are refreshed
after a `commit` op adds the owned commit (§6, N1); they are recomputed, not
frozen at first accept.

Acceptance is read-only and fully re-runnable; it is not an op under §3. If the
worktree directory is gone and `attempt_head == base_head` was never advanced
and no push op ever succeeded, there is nothing to deliver → delivery `failed`,
with `reason_kind='workspace_gone_no_effect'` (the `reason_kind` column is
shared across `blocked`/`conflicted`/`failed`; see §11.1, N2). If a push op **had**
succeeded, the remote branch is the surviving artifact and the delivery stays
actionable (re-open PR against the pushed ref).

### 5.1 Legacy runs without a captured `base_ref`

Runs created by def9d08 before the prepare-side change carry `base_head` but no
`base_ref`. Owlery does **not** guess the base branch. Accept moves the delivery
to `blocked(base_ambiguous)` and the UI/API asks the operator to name a
candidate base branch. The chosen branch is accepted **only if** its tip
contains `base_head` as an ancestor (`git merge-base --is-ancestor <base_head>
<candidate>`); otherwise the choice is rejected and the delivery stays
`blocked(base_ambiguous)`. A verified candidate is written into the delivery as
`base_ref` and delivery proceeds normally. New runs never reach this path.

### 5.2 Verification gate (advisory, not enforced here)

The delivery panel surfaces the run's `complete` metadata (the worker's declared
verification/evidence and any captured artifacts). Delivery does **not** run the
project test suite itself — that is the worker's responsibility recorded at
`complete` time. A future gate that blocks push on missing verification is
listed in §23; it is not built now because it needs a per-board policy decision.

## 6. Commit ownership

A completed worktree can be **clean** (worker already committed) or **dirty**
(worker left uncommitted edits — legitimate; `complete` records porcelain).

- **Clean** (`dirty=false`, `commits_ahead ≥ 1`): the worker's commits are the
  deliverable as-is. No `commit` op is created. Authorship is whatever the
  worker set; Owlery does not rewrite history.
- **Clean but empty** (`dirty=false`, `commits_ahead = 0`): the attempt produced
  no committed change. The delivery still reaches `ready`; push/PR/merge are
  refused at the **verb** layer with a `nothing_to_deliver` 409 (it is not a
  stored status or `reason_kind` — the delivery is legitimately `ready`, just
  with nothing to send), and only `teardown` is offered. This is not a failure of
  the run.
- **Dirty** (`dirty=true`): Owlery offers exactly one `commit` op that stages all
  tracked+untracked changes (`git add -A`) inside the attempt worktree and
  creates **one** owned commit. It never amends or rebases the worker's prior
  commits.

Owned-commit authorship is deterministic and attributable, never anonymous:

- author/committer name: `Owlery Task <task-short-id>` (configurable board-level
  default `git_delivery_author_name`);
- author/committer email: `git_delivery_author_email` board setting, default
  `owlery-tasks@localhost`;
- message: a template rendering task id/title, run attempt, assignee Agent
  identity, and the worker's `complete` summary, with a trailer
  `Owlery-Task: <task_id>` / `Owlery-Run: <run_id>`. No `Co-Authored-By` is
  fabricated for a human who did not write the code.

The `commit` op is local and idempotent-by-effect: it is a no-op if the worktree
is already clean by the time it runs (guarded by re-reading porcelain in the
same op), so a crash-then-manual-inspect that finds the commit already present
does not double-commit.

After a successful `commit` op the delivery's `attempt_head`, `commits_ahead`,
and `diffstat` are recomputed and folded in by the finish-op CAS (N1); the
immutable `base_ref`/`base_head` are untouched. Push and PR always operate on
this refreshed head, so a dirty attempt's owned commit — not its pre-commit
head — is what reaches the remote.

## 7. Push

`push` publishes `attempt_branch` to a remote.

- **Remote resolution**: the delivery reads `git remote` on the repo. The target
  remote is a board setting `git_delivery_remote` (default `origin`). If the
  repo has no such remote, push is unavailable and the delivery is
  `blocked(no_remote)` with a precise reason — never an invented remote.
- **Command**: `git push --set-upstream <remote> <attempt_branch>` with
  `--force-with-lease` **disabled** by default. A first push of an
  Owlery-created branch is non-destructive (the branch cannot already exist
  upstream unless a prior op pushed it). If the remote ref already exists and its
  tip is not an ancestor of the local tip, push is refused as a destructive
  guard (§13) rather than forced.
- **Transport credentials (S4)**: push authenticates entirely through the repo's
  **ambient** Git credential configuration — the SSH agent/keys or a configured
  Git credential helper the operator already uses for that remote. Owlery
  **never** injects the GitHub connector token into the remote URL, into the
  `git push` command line, or into the process environment; the connector token
  is used **only** for the hosting-platform REST API (PR creation, §8), never for
  git transport. This keeps the transport secret in git's own credential store,
  keeps it out of process listings and op results, and means a repo with no
  usable push credential simply produces a normal op `failed` (reason
  `push_auth_failed`) that a human resolves — Owlery does not silently fabricate
  a credentialed URL.
- **At-most-once**: the `push` op's `source_key` is
  `task:<task_id>:run:<run_id>:delivery:push`. If the process dies after
  `git push` contacted the remote but before the op row committed `succeeded`,
  boot recovery marks the op `interrupted` and the delivery `blocked`; a human
  confirms the remote state before any re-push (a re-push is a new
  `:push:retry:<n>` op). The remote branch itself is idempotent to re-push if
  unchanged, but Owlery does not assume that and does not auto-retry.
- Push captures the resolved remote URL (secret-stripped), the pushed ref, and
  the remote sha into the op result.

## 8. Pull request creation

`pull_request` opens a PR/MR against the base branch on the hosting platform.

- **Platform + auth (B3)**: GitHub is the v1 target and reuses the existing
  connector, resolved deterministically from the **run's snapshot `agent_id`**
  (the Agent that actually did the work, stored on `task_runs`), never from the
  identity of whoever triggers delivery. The delivery enumerates GitHub connector
  installations that this run-agent has enabled and that are currently live
  (`connector_manager.get_agent_connector_ids(run.agent_id)` filtered to the
  `github` kind and to installations whose token is usable):
  - **exactly one** live GitHub installation → it is selected automatically and
    its OAuth-App `repo`-scoped token authorizes `POST /repos/{owner}/{repo}/pulls`;
  - **zero** → `blocked(no_connector)` with a browser affordance to connect one;
  - **more than one** → `blocked(ambiguous_connector)`; Owlery does not pick.

  REST and the UI may pass an **explicit** `connector_installation_id` to select
  among multiple (or to override the single auto-pick); the chosen id is recorded
  in the `pull_request` op's `request` JSON so the audit shows which credential
  opened the PR. The MCP `deliver` verb may forward an installation id too, but
  the **caller's** Agent identity never substitutes for the run agent when
  enumerating candidates — a caller cannot borrow its own connector to act on a
  run performed by a different Agent. No new OAuth flow, no new secret storage,
  and Owlery never shells out to a `gh` CLI that may be absent.
- **Prerequisite**: a PR requires a preceding successful `push` op; the delivery
  refuses `pull_request` from a state with no pushed ref.
- **PR content**: title from task title (+ attempt tag), body from the worker
  `complete` summary + structured evidence + a link back to the Owlery task, base
  = `base_ref`, head = `attempt_branch`. Draft vs ready-for-review is a request
  parameter (default draft, the conservative choice).
- **At-most-once & idempotency**: `source_key` =
  `task:<task_id>:run:<run_id>:delivery:pr`. Creating a PR is **not** idempotent
  on GitHub (a second POST for the same head opens/returns differently), so this
  op is the sharpest case of §3: if the process dies after the POST but before
  the op commits, boot marks it `interrupted` and the delivery `blocked` and does
  **no** network I/O in the boot critical path. A **read-only** reconciliation
  (`GET /repos/{owner}/{repo}/pulls?head=<owner>:<branch>&state=all`) that fills
  in an already-created PR's number/url — never a second create — runs **off**
  the boot barrier: strictly bounded and after recovery completes, or lazily when
  the delivery is next viewed (see §16, S3). Until it resolves, the delivery
  stays `blocked(interrupted)`; if the read is inconclusive it stays there for
  human resolution. A read is safe; a second create is not.
- The op result stores the PR number, html url, and platform state.

## 9. Optional safe merge

Merge is **opt-in per delivery request**, off by default, and conservative.

- Allowed strategies: `fast_forward_only` (default) and `no_conflict_merge`
  (creates a merge commit only if git reports zero conflicts). **Never**
  `--force`, never `-X ours/theirs`, never an octopus/rebase that rewrites the
  base.
- Execution target: the merge happens against the **base branch in the source
  repository** (the board `working_dir` repo), which must be clean and on
  `base_ref` at merge time; a dirty or diverged base refuses the op
  (`base_not_clean` / `base_moved`).
- `conflicted` outcome: if fast-forward is impossible or a conflict is detected,
  the merge op fails cleanly (`git merge --abort` restores the base), the
  delivery becomes `conflicted`, and the UI shows the exact base/attempt refs and
  the manual commands. No partial merge is ever left in the working tree.
- Merge is an external-effect op only when the base branch is itself shared or
  subsequently pushed; the op still records at-most-once and is not auto-retried.
- A merge does not imply a push of the base; pushing the merged base is a
  separate explicit `push`-of-base request (kept out of v1 default flow — the
  common path is PR-then-review-then-merge-on-platform, and local merge exists
  for offline/solo repos). See §23 for the deferred "auto-push merged base."

## 10. Worktree teardown and retention

Teardown is the closing local phase. It must fix the **current bug** that
`cleanup_private_workspace` `rmtree`s the worktree directory without
deregistering it, leaving a stale entry in `<repo>/.git/worktrees/` and a
dangling `attempt_branch`.

Correct teardown, in order:

1. `git worktree remove <path>`. `--force` is used **only** after a **live**
   `git status --porcelain` of the worktree at teardown time confirms it is clean
   (S1) — never on the strength of the `complete`-time porcelain snapshot, which
   may be stale by the time an operator triggers teardown. If the live inspection
   shows uncommitted changes, plain `remove` refuses; `--force` then requires an
   explicit `force_discard_dirty=true` destructive confirmation (§13) so
   unrecorded user edits are never silently discarded. If the worktree directory
   is already gone, this step is a no-op success;
2. `git worktree prune` in the source repo to clear any residual registration;
3. **branch retention decision** (below) → optional `branch_delete` op;
4. filesystem safety net: only if the directory still exists inside the
   validated task-workspace root, `cleanup_private_workspace` removes it (its
   existing containment/2-part-path guard is retained unchanged).

Retention policy — a board setting `git_delivery_retention`:

- `keep` (default): after `delivered`, keep the local branch and (optionally) the
  worktree directory so the operator can inspect. Nothing is deleted
  automatically.
- `remove_worktree_keep_branch`: deregister+remove the worktree dir but keep the
  local branch (safe once pushed).
- `remove_all`: deregister worktree and delete the local branch — permitted only
  when the branch is fully contained in a successfully pushed remote ref
  **or** an explicit human `force_delete_unmerged=true` confirmation is supplied
  (§13). Owlery never deletes an unpushed, unmerged branch silently.

Teardown is idempotent: removing an already-removed worktree or deleting an
already-deleted branch is a no-op success, so recovery can safely re-run the
local teardown (but not the external push/PR).

## 11. Persistence model

Additive tables; no existing row is backfilled. Owned by the same dedicated
`TaskRepository` connection and `BEGIN IMMEDIATE` discipline as `task-board.md`
§5 (no model/network/filesystem work inside a transaction; ops run outside the
lock and their results are committed in a short CAS transaction).

### 11.1 `task_deliveries`

```sql
CREATE TABLE task_deliveries (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    status TEXT NOT NULL,               -- pending|preparing|ready|delivering|delivered|conflicted|blocked|failed
    repository TEXT NOT NULL,           -- absolute source repo path (snapshot)
    base_ref TEXT,                      -- base branch, captured at prepare_workspace time; empty = detached source HEAD; NULL = legacy run (§5.1)
    base_head TEXT,
    attempt_branch TEXT NOT NULL,
    attempt_head TEXT,
    dirty INTEGER NOT NULL DEFAULT 0,
    commits_ahead INTEGER,
    diffstat TEXT,                      -- JSON {files,insertions,deletions}
    remote_name TEXT,
    remote_url TEXT,                    -- secret-stripped
    pushed_ref TEXT,
    pr_number INTEGER,
    pr_url TEXT,
    pr_state TEXT,
    merge_strategy TEXT,
    retention TEXT,
    reason_kind TEXT,                   -- reason for a blocked|conflicted|failed status: no_remote|no_connector|ambiguous_connector|base_ambiguous|conflict|destructive|interrupted|op_failed|push_auth_failed|workspace_gone_no_effect
    reason_detail TEXT,                 -- human-readable specifics
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id)
);
CREATE INDEX task_deliveries_task ON task_deliveries(task_id);
CREATE INDEX task_deliveries_active ON task_deliveries(status)
  WHERE status IN ('preparing','delivering');
```

### 11.2 `task_delivery_ops`

```sql
CREATE TABLE task_delivery_ops (
    id TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL REFERENCES task_deliveries(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                 -- commit|push|pull_request|merge|branch_delete|worktree_remove
    source_key TEXT NOT NULL,           -- stable at-most-once key
    external INTEGER NOT NULL,          -- 1 if the op mutates outside Owlery (§3)
    state TEXT NOT NULL,                -- planned|running|succeeded|failed|interrupted
    request TEXT NOT NULL DEFAULT '{}', -- JSON of the requested parameters
    result TEXT,                        -- JSON of platform/git response, secret-stripped
    error TEXT,
    actor_kind TEXT NOT NULL,           -- user|agent
    actor_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (source_key)
);
CREATE UNIQUE INDEX task_delivery_ops_one_running
  ON task_delivery_ops(delivery_id) WHERE state = 'running';
CREATE INDEX task_delivery_ops_delivery
  ON task_delivery_ops(delivery_id, created_at);
```

`UNIQUE(source_key)` is the at-most-once boundary, exactly as
`session_injections.source_key` is. The partial one-running index guarantees a
delivery never has two outbound ops in flight. Every op transition also writes a
`task_events` row (reusing the existing board timeline) so the delivery history
is visible in the same audit stream as the rest of the task.

## 12. Transaction and concurrency rules

Reuses `task-board.md` §5 verbatim, extended with:

- **op start CAS**: `start_op(delivery_id, op_id)` moves the delivery
  `ready→delivering` and the op `planned→running` in one `BEGIN IMMEDIATE`
  transaction, succeeding only if the delivery has no other running op and is in
  a legal source state. Losing the CAS returns 409 + current delivery.
- **op finish CAS**: `finish_op(op_id, state, result|error)` commits the op
  terminal state, folds its effect into `task_deliveries` (e.g. `pushed_ref`,
  `pr_number`), advances delivery status, and writes the event — atomically.
- The actual git/network work happens **between** `start_op` and `finish_op`,
  outside any DB lock, following `task-board.md` §5's "no I/O inside the writer
  lock" rule. A crash in that window is exactly the `interrupted` case §3
  handles.
- Ops are serialized per delivery (the one-running index); different deliveries
  proceed concurrently under the manager's existing bounded worker model.

## 13. Permissions and destructive-action protection

Owlery is single-user (one `OWLERY_AUTH_TOKEN`) with a per-Agent tool policy; we
add operation-level guards rather than a role system.

- **Who may trigger delivery**: a human via REST, or an orchestrator Agent whose
  policy enables the `tasks` MCP delivery verbs (§15). A **worker** may only
  *request* delivery of its own run (a hint recorded on `complete`), never
  execute a push/PR/merge itself — outbound effects run in the trusted manager,
  not model-driven, so no prompt injection can push code or open a PR.
- **Destructive guards** (each refused unless an explicit typed confirmation flag
  is supplied in the request):
  - non-fast-forward / overwriting an existing remote ref on push
    (`allow_force_push`, default false — and even then `--force-with-lease`,
    never bare `--force`);
  - deleting an unpushed/unmerged local branch (`force_delete_unmerged`);
  - force-removing a worktree that a **live** porcelain check finds dirty at
    teardown time (`force_discard_dirty`, §10 S1) — the stale `complete`-time
    snapshot is never trusted for this decision;
  - merging into a base that is not clean or has moved (never overridable — the
    op refuses outright, no flag);
  - pushing to a remote whose host is not the repo's recorded origin host
    (`allow_foreign_remote`).
- **Confirmation surfacing**: the REST/MCP layer returns a structured
  `requires_confirmation` payload naming the exact destructive action and the
  flag needed; the UI renders a typed confirmation. No destructive op proceeds on
  a default call.
- **Secret hygiene**: tokens are never written to `task_delivery_ops.result`,
  `remote_url`, events, or any error string; URLs are stripped of userinfo. The
  GitHub connector token stays inside the connector-auth call path for the
  platform REST API only and is **never** placed into a remote URL, the `git`
  command line, or the subprocess environment for push/fetch (S4); git transport
  uses the operator's ambient credential store (§7).
- **Reuse of existing safety**: all filesystem removal continues to go through
  `workspaces.cleanup_private_workspace`'s validated-root + 2-part-path guard;
  no delivery code removes a path by a raw model/user string.

## 14. REST and WebSocket contract

Additive endpoints hanging off the existing task/run surface:

```text
GET    /api/tasks/{task_id}/runs/{run_id}/delivery        # delivery + ops, or 404 if none
POST   /api/tasks/{task_id}/runs/{run_id}/delivery/accept # create+capture baseline (idempotent)
POST   /api/tasks/{task_id}/runs/{run_id}/delivery/commit
POST   /api/tasks/{task_id}/runs/{run_id}/delivery/push
POST   /api/tasks/{task_id}/runs/{run_id}/delivery/pull-request
POST   /api/tasks/{task_id}/runs/{run_id}/delivery/merge
POST   /api/tasks/{task_id}/runs/{run_id}/delivery/teardown
GET    /api/tasks/{task_id}/runs/{run_id}/delivery/ops     # append-only op history
```

- All mutating verbs are CAS-guarded and return 409 + current delivery on a lost
  race, matching `task-board.md` §9.1.
- Destructive verbs return `409 requires_confirmation` with the flag name until
  the confirmation flag is present (§13).
- Every committed delivery/op transition broadcasts a `task_event` (reusing the
  existing `task_event` WS message and per-board seq cursor). Old clients ignore
  unknown event `kind`s; the DB row commits before broadcast. No new WS message
  type is introduced.

## 15. MCP surface

The `tasks` MCP server gains delivery verbs; identity comes from trusted env, not
model arguments, per `task-board.md` §8.

- **Worker-scoped** (`OWLERY_TASK_ID`/`OWLERY_TASK_RUN_ID` present): only
  `request_delivery(note?)` — records the worker's intent to have its run
  delivered. It performs **no** external effect. This keeps push/PR strictly in
  the trusted server path.
- **Orchestrator-scoped** (Agents that enable `tasks`): `delivery_status(task,
  run)`, `deliver(task, run, {push?, pull_request?, merge?, retention?,
  confirmations?})` which plans and executes the requested ops through the same
  manager path as REST, and `delivery_teardown(...)`. Destructive confirmations
  must be explicit; an orchestrator cannot bypass a §13 guard by prompt text
  because the repository counts flags, not claims.

No change to the worker terminal protocol (`complete`/`block`) except the new
optional `request_delivery` hint; `complete` semantics are untouched.

## 16. Manager, boot, and shutdown

`TaskBoardManager` gains a `DeliveryCoordinator` collaborator (same lifecycle as
the dispatcher). It owns op execution and the git/connector subprocess calls,
never the DB writer lock during I/O.

Boot delivery reconciliation extends `task-board.md` §6.5. It runs inside the
existing injection-dispatch-paused barrier, **after** task phase-1 run
interruption and terminal-run outbox reconstruction, and **before** the
dispatcher starts. Every step is DB-only or a strictly-bounded local git
command; **no hosting-platform network I/O happens in this critical path** (S3):

1. any `task_delivery_ops` left `running` from the prior process → `interrupted`
   (never re-executed);
2. the owning delivery → `blocked(interrupted)` with a `reason_kind='interrupted'`
   detail naming the op kind, so the operator knows a push/PR/merge may or may
   not have landed;
3. any delivery left `preparing` with **no** running op is reset to `pending`
   (S5): baseline capture is a read-only, fully re-runnable step (§5), so a crash
   mid-prepare simply re-accepts later; it is never left dangling in `preparing`;
4. local, idempotent teardown ops that were mid-flight (`worktree_remove`,
   `branch_delete` of an already-pushed branch) are **not** auto-run; a human may
   re-plan them as new ops;
5. **terminal-delivery outbox reconstruction (B2)**: because the delivery
   terminal state commits on the `TaskRepository` connection while the origin
   `session_injections` row is created on the main `Database` connection, a crash
   between the two leaves a terminal delivery with no notification intent. Boot
   idempotently ensures that **every** terminal delivery
   (`delivered`/`conflicted`/`blocked`/`failed`) whose task points at an existing
   `origin_session_id` has its deterministic source
   `task:<task_id>:run:<run_id>:delivery:terminal`. `source_key` uniqueness makes
   both live creation and this reconstruction idempotent (mirroring
   `task-board.md` §6.5 step 6 for terminal runs). A deleted/absent origin
   produces exactly one durable `notification_unavailable` task event instead.

Any read-only PR reconciliation for an `interrupted` `pull_request` op (§8) is
deliberately **excluded** from the boot barrier (S3). It runs only **after** all
recovery completes and the dispatcher and injection drain are live — either as a
bounded background pass (each GET capped by `task_delivery_op_timeout_seconds`)
or lazily when the delivery is next viewed. It never blocks the injection drain,
the dispatcher start, or any other session's turn; the delivery simply stays
`blocked(interrupted)` until the bounded read resolves it or a human does.

Shutdown stops planning new ops first, lets any single in-flight op finish or be
recorded, and starts no new op after teardown begins — mirroring the dispatcher
shutdown barrier. No outbound op is abandoned silently: a killed op is recovered
as `interrupted` on next boot.

Delivery notifications reuse the durable outbox: a terminal delivery
(`delivered`/`conflicted`/`blocked`/`failed`) aimed at a live
`origin_session_id` emits one `session_injections` row keyed
`task:<task_id>:run:<run_id>:delivery:terminal`, inheriting dedup + boot barrier
from `durable-session-injections.md`, with the boot reconstruction above as its
crash-safety net. A missing origin records one `notification_unavailable` event,
exactly as terminal-run notifications already do.

## 17. Configuration

Board-level settings (with server-side upper bounds, mirroring
`task_boards`' existing knobs):

- `git_delivery_remote` (default `origin`)
- `git_delivery_retention` (`keep` | `remove_worktree_keep_branch` |
  `remove_all`; default `keep`)
- `git_delivery_author_name` / `git_delivery_author_email`
- `git_delivery_default_draft_pr` (default true)
- `git_delivery_default_merge` (default `none`; `fast_forward_only` opt-in)

Global (`server/config.py`): `task_delivery_op_timeout_seconds` (default 60,
bounding each git/network subprocess, reusing the `workspaces._run` timeout
pattern). No new credential config — PR auth is entirely the existing connector.

## 18. Migration and compatibility

- Additive `task_deliveries` / `task_delivery_ops` tables via a new
  `PRAGMA user_version` step in `database.py`; no existing task/run row changes.
- The new board settings are additive columns on `task_boards` with safe
  defaults; existing boards get `keep`/`origin`, i.e. the current
  no-auto-anything behavior, so **no board silently starts pushing or deleting.**
- `cleanup_private_workspace` is extended to deregister worktrees; its public
  signature and containment guard are unchanged, and the extension is a strict
  bug-fix (stale registrations were a latent leak). Existing `copy`-mode cleanup
  is unaffected (no worktree to deregister).
- OpenAPI contracts regenerated after routers/models land; frontend `contracts.ts`
  regenerated. Old clients ignore the new event payloads and endpoints.

## 19. Invariants

1. Delivery is a post-terminal action on a **completed** `git_worktree` run; it
   never reopens `done` or mutates `task_runs`.
2. Exactly one delivery per run; exactly one outbound op in flight per delivery.
3. Every externally-observable mutation is a durable, uniquely-keyed op executed
   at most once and **never** auto-retried — not by tick, heartbeat, or boot.
4. A process death mid-op yields `interrupted` (effect unknown), surfaced to a
   human; recovery may **read** the platform to reconcile a PR but never
   **writes** to reconcile.
5. Push never force-overwrites, merge never force-resolves, branch delete never
   discards unpushed+unmerged work — each requires an explicit typed
   confirmation, and some (dirty/moved base merge) are refused outright.
6. Outbound effects run only in the trusted manager path; no model argument or
   prompt supplies a repo path, remote, credential, or confirmation flag —
   workers may only *request* delivery.
7. Filesystem removal always passes the validated-root containment guard; no
   path is deleted by raw user/model string.
8. Worktree teardown always deregisters before/with directory removal; a delivery
   never leaves a dangling `.git/worktrees/` registration.
9. Terminal delivery notifications use `session_injections` and inherit its
   dedup + boot barrier; every terminal delivery with a live origin eventually
   has exactly one deterministic `:delivery:terminal` source, reconstructed on
   boot when a cross-connection crash prevented live creation (B2). A missing
   origin becomes one `notification_unavailable` event.
10. Secrets never enter delivery rows, events, or op results.
11. The base branch (`base_ref`) is captured at `prepare_workspace` time and is
    an immutable snapshot; accept never re-derives it from the source repo's live
    `HEAD`, and a legacy run without it is `blocked(base_ambiguous)` until an
    operator names a base whose tip has `base_head` as an ancestor (B1).
12. Git transport (push/fetch) uses only the operator's ambient credential store;
    the hosting-platform connector token is used solely for the platform REST API
    and never enters a remote URL, git command line, or subprocess env (S4).
13. No hosting-platform network I/O occurs inside the boot recovery barrier; PR
    read-reconciliation is bounded and runs off the critical path, never blocking
    injection drain or the dispatcher (S3).

## 20. Failure matrix

| Failure | Durable outcome |
|---|---|
| Accept on a run whose worktree is gone, no prior push | Delivery `failed`, `reason_kind='workspace_gone_no_effect'` |
| Accept, worktree gone, but a push op previously succeeded | Delivery stays actionable against `pushed_ref` |
| Legacy def9d08 run with no captured `base_ref` | `blocked(base_ambiguous)`; operator names a base whose tip has `base_head` as ancestor, else stays blocked — never guessed (B1) |
| Clean worktree, zero commits ahead | delivery `ready`; push/PR/merge refused at the verb with a `nothing_to_deliver` 409; teardown allowed (not a stored status) |
| Dirty worktree | Single owned `commit` op; `attempt_head`/`commits_ahead`/`diffstat` refreshed; never amends worker commits |
| No `git_delivery_remote` on repo | Delivery `blocked(no_remote)`, precise reason |
| Push would overwrite an existing non-ancestor remote ref | Refused as destructive; needs `allow_force_push` (then `--force-with-lease`) |
| Repo has no usable ambient push credential | Op `failed`, `reason_kind='push_auth_failed'`; token never spliced into URL (S4) |
| Process dies after `git push` before op commit | Op `interrupted`, delivery `blocked`; no auto re-push |
| Zero live GitHub connectors for the run agent | `blocked(no_connector)` + connect affordance (B3) |
| More than one live GitHub connector for the run agent | `blocked(ambiguous_connector)`; REST/UI must pick an `connector_installation_id` (B3) |
| PR requested with no prior push | Refused; `push` prerequisite |
| Process dies after PR POST before op commit | Op `interrupted`, delivery `blocked(interrupted)`; a **bounded, off-critical-path** read of pulls-by-head reconciles later, never re-POSTs (S3) |
| Merge base not clean / moved | Merge op refused (`base_not_clean`/`base_moved`), no partial merge |
| Merge conflict / non-fast-forward | `git merge --abort`, delivery `conflicted`, manual commands shown |
| Teardown force on a live-dirty worktree | Plain remove refuses; `--force` needs `force_discard_dirty` (live porcelain, not stale snapshot — S1) |
| Teardown of already-removed worktree/branch | Idempotent no-op success |
| `remove_all` retention on unpushed unmerged branch | Refused unless `force_delete_unmerged` |
| Crash between delivery-terminal commit and origin injection creation | Boot reconstructs the deterministic `:delivery:terminal` source idempotently (B2) |
| Origin session deleted at delivery-terminal | One `notification_unavailable` event; delivery truth unchanged |
| Crash mid-`preparing` with no running op | Boot resets delivery to `pending`; re-accept re-captures the baseline (S5) |
| Server restart with a delivery mid-op | Op `interrupted`, delivery `blocked(interrupted)`; dispatcher only starts after DB-only reconciliation |

## 21. Browser experience

The task drawer's run timeline (`web/src/components/tasks/TaskRunTimeline.tsx`)
gains, for a completed `git_worktree` run, a **Delivery** panel:

- baseline card: base ref/head, attempt branch/head, dirty flag, diffstat,
  commits-ahead;
- an action row: Accept → (Commit if dirty) → Push → Open PR → (Merge) →
  Teardown, each button disabled until its prerequisite state is reached and
  showing a typed-confirmation dialog for destructive actions;
- an append-only **op log** (kind, state, timestamps, platform result/error,
  actor), replayed from `task_delivery_ops` and live-updated via `task_event`;
- clear `blocked`/`conflicted` states with the exact git commands and PR link;
- retention selector reflecting the board default, overridable per teardown.

Mobile: the panel stacks under the run entry in the full-screen drawer. No new
top-level route; this lives inside the existing `TaskDrawer`.

## 22. Test plan (gates)

All four `CLAUDE.md` suites must stay green (pytest, Vitest, `tsc`, Playwright).
The current repo baseline **after def9d08**, verified by collection on
2026-07-26, is **pytest 1106 / Vitest 142 / Playwright 95** — this feature only
adds. (Note: `CLAUDE.md`'s "After Every Code Change" counts still read the
pre-Task-Board 967 / 95 / 69; that staleness predates this task and is called out
in §24 for a separate `CLAUDE.md` correction — it is not fixed on this docs
branch.)

**Backend unit (new `tests/test_task_delivery_*.py`)**

- `prepare_workspace` captures `base_ref` (branch / empty on detached) with
  `base_head` into run metadata; accept reads the snapshot and never re-derives
  from a moved source `HEAD` (B1);
- legacy run (no `base_ref`) → `blocked(base_ambiguous)`; a candidate whose tip
  has `base_head` as ancestor is accepted, a non-ancestor candidate is rejected,
  no branch is guessed (B1);
- baseline capture: clean / dirty / empty / worktree-gone(-with/without-push);
- the normative transition table (§4.1.1): every legal transition, and an
  unlisted one returns 409 with the current row unchanged (S2);
- owned-commit authorship, message trailers, no-amend of worker commits,
  clean-by-recheck no-op; post-commit refresh of `attempt_head`/`commits_ahead`/
  `diffstat` while `base_ref`/`base_head` stay immutable (N1);
- `nothing_to_deliver` is a verb-level 409 with the delivery still `ready`, not a
  stored status/`reason_kind` (N2);
- `task_delivery_ops` `UNIQUE(source_key)` and one-running partial index;
- start/finish op CAS, 409 on lost race, delivery status folding;
- push: remote resolution, missing remote block, non-ancestor destructive
  refusal, force-with-lease-only under flag; transport uses ambient credentials
  and never the connector token — a no-credential repo yields `push_auth_failed`
  and the token appears in no URL/arg/env (S4);
- PR connector resolution by the **run's** snapshot agent (B3): exactly-one
  auto-select, zero → `no_connector`, many → `ambiguous_connector`, explicit
  `connector_installation_id` selection recorded in the op request, and a caller
  Agent cannot substitute its own connector for the run agent's;
- PR: token path (mocked `connector_manager`/`_gh`), push-prerequisite,
  interrupted-PR reconciliation is a **read** (mocked GET) that never re-POSTs;
- merge: fast-forward success, conflict abort→`conflicted`, dirty/moved base
  refusal, no partial tree left;
- teardown: worktree deregistration (real `git worktree` in a temp repo),
  `--force` gated on a **live** porcelain check (not the stale `complete`
  snapshot) behind `force_discard_dirty` (S1), idempotent re-run, retention
  `keep`/`remove_worktree_keep_branch`/`remove_all`, unpushed-unmerged refusal;
- boot recovery: running op→interrupted, delivery→blocked, `preparing`-with-no-op
  reset to `pending` (S5), dispatcher-after ordering, **no** platform network I/O
  in the boot barrier, no auto re-execution;
- boot terminal-delivery outbox reconstruction: a terminal delivery whose origin
  survives but whose `:delivery:terminal` source was never created is repaired
  idempotently; a deleted origin yields one `notification_unavailable` event (B2);
- off-critical-path PR reconcile: a bounded read runs only after recovery/
  dispatcher are live and never blocks the injection drain (S3);
- destructive-guard confirmations for every guarded verb;
- terminal delivery outbox exactly-once (+ missing-origin event);
- secret hygiene: token never in row/result/url/env/error.

**Backend real-CLI**: none required — git is exercised with a real temp repo
(as `test_task_workspaces.py` already does); GitHub is mocked. No new
`test_*_real.py`, so quota is untouched.

**Frontend unit (Vitest)**: delivery panel state machine (button
enable/disable), op-log replay + `task_event` live update, typed-confirmation
dialog gating, blocked/conflicted rendering.

**TypeScript**: `contracts.ts` regenerated; strict-mode clean.

**E2E (Playwright, extend `web/e2e/task-board.spec.ts`, fake CLI only)**: a
`git_worktree` task completes against a temp git repo, the drawer shows the
baseline, Accept→Commit(dirty)→Push(to a local bare remote)→Teardown succeeds and
deregisters the worktree; a destructive action shows and requires its
confirmation; PR creation is covered by a stubbed connector so the suite stays
off the network and off quota. Restart-mid-op E2E asserts the delivery lands in
`blocked(interrupted)` without a duplicate op.

## 23. What this deliberately defers

Each needs a real second use case or a separate product decision, and none is
represented by a placeholder column or half-active path:

- non-GitHub platforms (GitLab/Gitea/Bitbucket) — v1 reuses only the GitHub
  connector; the op model is platform-neutral so a second platform is an adapter,
  not a redesign;
- auto-push of a locally-merged base branch;
- a policy gate that blocks push/PR on missing worker verification (§5.2);
- automatic delivery on `complete` (v1 is human/orchestrator-triggered by
  design; auto-deliver would need a per-board trust decision);
- delivery for `shared`/`copy` workspace modes;
- squash/rebase commit shaping of worker history.

## 24. Acceptance criteria

The feature is done only when:

1. a completed `git_worktree` run exposes a durable delivery with an accurate
   base/head/dirty/diffstat baseline;
2. dirty worktrees produce exactly one owned, attributable commit; clean ones are
   delivered as-authored; empty ones cannot be pushed;
3. push, PR, and optional merge each execute as at-most-once ops, are recorded
   terminally, and are **never** auto-retried;
4. a process death mid-op yields `interrupted`/`blocked` and, for PRs, a
   read-only reconciliation — never a duplicate external effect;
5. every destructive action (force push, unmerged branch delete, foreign remote)
   requires an explicit typed confirmation, and dirty/moved-base merges are
   refused outright;
6. worktree teardown deregisters the worktree and honors the retention policy
   without ever deleting outside the validated workspace root;
7. PR creation reuses the existing GitHub connector credential with no new secret
   storage and no `gh` CLI dependency;
8. terminal delivery notifications reach the origin transcript exactly once via
   the durable outbox;
9. existing boards default to no-auto-anything, and no existing Task Board
   behavior changes outside these additive integration points;
10. all four `CLAUDE.md` test suites pass, and independent architecture/code
    review approves.

**`CLAUDE.md` note (N3):** `CLAUDE.md`'s "After Every Code Change" section still
cites the pre-Task-Board counts (pytest 967 / Vitest 95 / e2e 69). The verified
current fact is 1106 / 142 / 95. Correcting those four numbers in `CLAUDE.md` is
a real but **separate** fix belonging to the Task Board landing, not this docs
branch; the implementer of this feature must bump them again (to the new totals
after these tests land) as part of shipping, and should also correct the stale
def9d08 baseline in the same edit. This task book therefore states the true
current baseline rather than `CLAUDE.md`'s stale one.

## 25. Implementation order

One feature delivered completely, staged so each review has a coherent invariant
boundary (mirroring `task-board.md` §17). The UI is not enabled until every prior
stage exists; no throwaway alternate state machine is shipped between stages:

1. **schema + repository**: `task_deliveries` / `task_delivery_ops` migration,
   the `base_ref` capture extension in `prepare_workspace`, the reason/status
   columns, and the CAS/transition transactions (§4.1.1, §11, §12) with their
   full state-machine tests — no ops yet;
2. **local ops**: `accept` (incl. legacy `base_ambiguous`), `commit`, and the
   teardown ops (`worktree_remove` with live-porcelain force gate,
   `branch_delete`, retention), all local-only and idempotent (§5, §6, §10);
3. **external ops + connector**: `push` (ambient-credential transport),
   `pull_request` (run-agent connector resolution + at-most-once), and optional
   `merge`, with destructive guards (§7, §8, §9, §13);
4. **boot / outbox**: delivery reconciliation, `preparing`→`pending` reset,
   terminal-delivery outbox reconstruction, and the off-critical-path bounded PR
   reconcile (§16), plus shutdown barrier;
5. **REST + MCP**: the delivery endpoints and worker/orchestrator verbs as pure
   manager/repository consumers, with OpenAPI/`contracts.ts` regeneration
   (§14, §15);
6. **UI**: the `TaskDrawer` delivery panel, op-log replay, and typed-confirmation
   dialogs (§21);
7. **full matrix**: the complete unit/E2E test matrix (§22) and architecture
   docs, then independent review.
