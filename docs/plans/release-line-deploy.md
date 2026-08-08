# Release-line deploy — deploy from the protected branch, not task branches

> **Status:** design task book. Groundwork (schema + resolvers + board
> config) already exists on branch `owlery/release-line-deploy` (3 commits by
> Aberforth: `29d04cc`, `323febd`, `724a99c`); this document specifies the
> remaining execution pipeline and the removal of the per-run deploy surface.
>
> It builds on `local-deploy.md` and changes none of its mechanics — slots,
> switcher, journal, quiesce, probation, snapshots are reused byte-identical.
> What changes is **what may be deployed**: a board's protected release
> branch, resolved at the configured remote, instead of a task attempt branch.

## 1. Decision and motivation

Production deploys now source from the board's **release branch**
(`deploy_release_ref`, default `main`) at the configured Git remote. Task
branches are delivery artifacts — they integrate through the existing
git-delivery path (commit → push → PR → merge) and are **never** a production
source.

Why: per-run deploys overwrite each other. Task branch B does not contain
task branch A's merged work, so deploying B after A silently un-ships A.
The unified release line makes every deploy a superset of the last: whatever
`main` is at the remote, that is what goes live, with a human-readable
version (`rYYYYMMDD.NN`) and a durable audit trail.

This **supersedes** `local-deploy.md` §2's allowance for trial-deploying an
unmerged attempt branch (user decision, 2026-08-08). The per-run
`deploy_stage`/`deploy_switch` verbs and their delivery-panel buttons are
removed outright — not hidden, not deprecated-but-kept. A worker's
`request_delivery(note)` deploy hint remains as the way an agent asks for a
release.

## 2. What already exists (do not redo)

On `owlery/release-line-deploy`, based on current `main`:

- `release_deployments` table (board-scoped, `version` unique per board,
  `sha` immutable identity, `state` lifecycle, one-active partial index) and
  `release_deployment_ops` (kinds `stage|switch|rollback`, same state set as
  delivery ops, one-running-per-release index) — `server/database.py`;
- `deployments.release_id` nullable column + migration;
- `ReleaseDeploymentRecord` / `ReleaseDeploymentOpRecord` models;
- repository: `plan_release_deployment` (persists a resolved SHA, computes
  `rYYYYMMDD.NN`), `plan_release_op`, `list/get_release_deployments`;
- `workspaces.remote_release_ref_tip(repo, remote, ref)` — resolves the
  configured branch (and only a branch under `refs/heads`) at the remote to
  a full SHA, fail-closed on anything else;
- board config `deploy_release_ref` (default `main`, validated non-empty,
  no leading `-`) through DB, REST `BoardCreate`/`BoardPatch`, and the
  BoardToolbar settings dialog (field appears when local deploy is enabled).

All shared machinery from `local-deploy.md` is on `main` and untouched:
`server/deploy.py` (layout, precheck, `stage_slot`), the switcher + journal,
`deploy_quiesce`, probation, snapshots, the `deployments` table and its
one-live / one-active locks.

## 3. Design points

### 3.1 The release pipeline

One flow, board-level:

1. **Plan + stage** (one verb): resolve `board.deploy_release_ref` at the
   board's `git_delivery_remote` via `remote_release_ref_tip` → immutable
   SHA; `plan_release_deployment`; then run the existing `stage_slot`
   pipeline on the idle slot with that SHA. The `deployments` row is created
   with `release_id` set and `delivery_id`/`task_id` NULL — it remains the
   slot-level source of truth; the release row mirrors its lifecycle
   (`planned → staging → staged`). Re-staging after new commits land is a
   **new** release row; the prior non-live release becomes `superseded`.
2. **Switch**: the same at-most-once handoff as `local-deploy.md` §7 —
   quiesce census, snapshot, journal, detached switcher, probation — driven
   by a `release_deployment_ops` `switch` op instead of a delivery op. The
   release row follows `staged → switching → live` (previous live release →
   `superseded`), reconciled from the journal like everything else.
3. **Rollback**: stays **slot-level** on the `deployments` table (the
   existing superseded-slot mechanism), recorded as a `rollback` op on the
   release being abandoned; release rows are settled accordingly. A rollback
   target from the pre-release era (a `deployments` row with `delivery_id`
   and no `release_id`) must still work — provenance does not matter to a
   slot flip.

Preconditions are unchanged in shape: board `allow_local_deploy`, instance
`deploy_precheck`, the global deploy lock. The git prerequisites of
`local-deploy.md` §2 collapse to one: the release ref must resolve at the
remote (there is no dirty/ahead/attempt_head question — the remote branch
tip IS the release).

### 3.2 Op lifecycle

`release_deployment_ops` gets the same start/finish CAS discipline as
`task_delivery_ops` (§4 of `local-deploy.md`): planned → running under the
one-running index, terminal states durable, `switch` ops `external=1` in
spirit — journal-backed, never auto-retried, every repeat an explicit new op.
Do not reuse `task_delivery_ops` for this (the schema comment explains why:
a release outlives task retention).

### 3.3 Boot reconciliation and census

- `recover_deliveries` / `reconcile_deploy_switch_ops` must settle running
  **release** switch ops by the same §8 journal table — the journal format
  and steps are identical; only the table the verdict lands in differs.
  Orphan `staging` release rows release the lock the same way orphan staging
  deployments do today.
- The quiesce census must count a running release op as busy, exactly as it
  counts running delivery ops.

### 3.4 REST and UI

- Board-level endpoints (final paths are the executor's choice, keep them
  RESTful and CAS-guarded like every delivery verb):
  `POST` stage-release, `POST` switch (human-only, same
  `deploy_switch_user_only` refusal as today), `GET` release history +
  live/staged status, rollback (human-only, `confirm_rollback` typed
  confirmation, reusing the existing confirmation surfacing).
- **UI**: a board-level Releases surface (placement: near the board header /
  toolbar — executor's judgment) showing: configured release branch, its
  current remote tip vs the live sha, staged release if any, Stage / Switch
  buttons with prerequisite-disabled states and the busy census on refusal,
  release history with version numbers, and Rollback with typed
  confirmation. The `server_restarting` banner and build-sha reload already
  exist and are untouched.
- **Removal**: the delivery panel's Local-deploy section (Stage/Switch/
  Rollback buttons), the per-run deploy REST verbs
  (`.../delivery/deploy/stage`, `.../delivery/deploy/switch`), and the
  coordinator's delivery-scoped `deploy_stage`/`deploy_switch`/
  `deploy_rollback` entry points go away. The shared machinery they wrapped
  (`stage_slot`, `_perform_switch_handoff`, snapshot, journal, quiesce) is
  what the release coordinator now drives. Existing tests of the shared
  machinery are adapted to the release entry points, not deleted.
  `GET /api/deployments` and its history/rollback semantics survive (slot
  truth), now carrying `release_id`.

### 3.5 Permissions

Unchanged from `local-deploy.md` §10: stage may be human or agent-via-MCP if
ever exposed (v1 exposes REST only); **switch and rollback are human-only**.
Board opt-in `allow_local_deploy` still gates everything.

## 4. What this does NOT do

- No tags or arbitrary revisions as release sources — a branch under
  `refs/heads` only (already enforced by `remote_release_ref_tip`).
- No auto-deploy on merge; a release is always an explicit human action.
- No per-task/trial deploys in any form — removed, not hidden.
- No CI/smoke gates beyond the existing import probe + health check.
- No release-notes generation, no changelog tooling.
- No remote-host deploys, no zero-downtime handover (unchanged deferrals).

## 5. Test gates

All four suites green after every child task (CLAUDE.md). New coverage:
release repository ops (version sequencing, one-active lock, op CAS), the
release coordinator (plan+stage happy path, unresolvable ref, lock held,
stage failure settles release+lock, switch refusals and handoff, rollback
incl. a pre-release-era target), boot reconciliation of release ops (every
§8 journal-tail row), census counts a running release op. Existing
delivery-scoped deploy tests are rewired to the release path where they
tested the shared pipeline, and dropped only where they tested the removed
per-run surface itself. Frontend: release panel state machine, removal of
delivery-panel deploy controls, BoardToolbar release-branch field (exists).
No real-model quota anywhere.

## 6. Acceptance criteria

1. A board with `allow_local_deploy` stages and switches the remote release
   branch tip; `/health`, the live `deployments` row, and the `live` release
   row (with its `rYYYYMMDD.NN` version) all agree.
2. Two successive releases: the second contains the first (both are `main`
   tips); rollback returns to the first; no overwrite semantics remain.
3. Per-run deploy verbs are gone from REST and UI; the git-delivery flow
   (commit/push/PR/merge) is untouched.
4. Interrupted release ops boot-reconcile per the §8 table with no
   auto-retry and no network in the boot barrier.
5. All four suites pass; independent review (Snape) approves.

## 7. Execution plan (child tasks)

- **T-A (Dobby)** — backend, additive: merge `owlery/release-line-deploy`
  and this plan's branch; op lifecycle CAS; release coordinator
  (plan+stage, switch, rollback); boot reconciliation + census; REST
  endpoints. Old per-run path left intact so every suite stays green.
- **T-B (Dobby, after T-A)** — the swap: board Releases UI; remove per-run
  deploy verbs, coordinator entry points, and delivery-panel controls;
  adapt/rewire tests. Every suite green.
- **T-C (Snape, after T-B)** — independent code review of the combined
  branch against this document.
- **T-D (Albus, after T-C)** — final acceptance against §6.
