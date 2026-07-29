# Multi-agent Task Board

> **Status:** design task book, implementation pending.
>
> This document defines Owlery's durable, human-visible coordination layer for
> work that spans agents, sessions, restarts, and review cycles. It deliberately
> does **not** replace `delegation_runs`, `bg_tasks`, `research_jobs`,
> `schedules`, or `session_injections`.

## 1. Decision

Owlery will add a first-class **Task Board** with:

- multiple boards, normally one per project or operational domain;
- durable tasks assigned to named Owlery Agents;
- a task tree for decomposition and a separate dependency DAG for execution
  ordering;
- append-only comments, run attempts, events, structured handoffs, and
  artifacts;
- an in-process dispatcher that starts isolated task-worker sessions;
- explicit heartbeat, block, cancel, and human-unblock semantics;
- a browser Kanban view, tree view, task drawer, and live event updates;
- a dedicated `tasks` MCP server used by both orchestrator sessions and
  dispatcher-spawned workers.

The feature is a durable **intent and coordination** layer. Existing subsystem
tables remain the execution truth for their own work:

| Question | Owner |
|---|---|
| What outcome is wanted, who owns it, and what blocks it? | Task Board |
| What happened in one task attempt? | `task_runs` + worker session |
| What happened in an agent-to-agent RPC inside that attempt? | `delegation_runs` |
| What happened in a background command? | `bg_tasks` |
| What happened in deep research? | `research_jobs` |
| Did a system event enter a transcript exactly once? | `session_injections` |

This is why `tasks` is not `work_items` under a new name. A task is an explicit
user/agent planning object with a lifecycle that may contain many heterogeneous
execution attempts. The runtime tables are not migrated into it.

## 2. Product contract

The Task Board is for work with at least one of these properties:

- it may outlive the current model turn or server process;
- a different named Agent may pick it up later;
- it depends on other work;
- a human may need to comment, block, reassign, or approve the next attempt;
- the work needs a durable audit trail and discoverable handoff;
- the result should be visible outside the originating chat.

Use ordinary delegation when a caller needs a short child answer returned to
the same conversation. Use the Task Board when the work itself must remain a
first-class object after either agent's context ends. A task worker may still
delegate short subtasks during an attempt.

### 2.1 User stories

1. A user creates "Ship durable task board" under the Owlery repository board,
   assigns Albus, and links implementation children to a Snape review task.
2. Albus decomposes a goal into parallel children. The reviewer does not become
   runnable until every required implementation task is done.
3. A worker needs a product decision, blocks with a precise question, and the
   user answers from the browser or an originating chat. A new run starts only
   after an explicit unblock.
4. The server restarts during a worker that may already have edited files. The
   run becomes `interrupted`, the task becomes `blocked`, partial transcript and
   workspace remain inspectable, and Owlery does not retry automatically.
5. A user opens a completed task months later and can see its tree position,
   dependencies, comments, every attempt, output session, cost, verification,
   artifacts, and final handoff.
6. Two dispatcher ticks or two API callers race to start the same task. Exactly
   one durable run and one worker session win.

## 3. Concepts and identity

### 3.1 Board

A board is a durable project/domain boundary. Agents remain global; tasks and
their dependency graph never cross boards.

Each board owns:

- display name and description;
- an absolute default working directory;
- a default workspace mode;
- concurrency and task-spawn safety limits;
- a durable dispatcher enabled/paused setting;
- its task graph and archived history.

Boards live in Owlery's existing SQLite database. Separate database files would
break foreign keys to Owlery Agents and make the browser/API reconcile multiple
truth stores for no benefit.

### 3.2 Task

A task is the stable, user-visible intent. Its id survives assignment changes,
blocking, and repeated attempts.

Task status:

```text
triage -> todo -> ready -> running -> done
                     \       |
                      \-> blocked
```

- `triage`: an idea that is not executable yet.
- `todo`: specified, but unassigned, dependency-blocked, or scheduled for later.
- `ready`: assigned, due, and every dependency is done.
- `running`: exactly one active `task_run` owns it.
- `blocked`: stopped for human input, capability, cancellation, interruption,
  or a known failure. It never auto-runs.
- `done`: immutable successful outcome. Revision work is a new linked task, not
  a destructive reopening of history.

`scheduled_at = NULL` means due immediately. A future timestamp keeps an
otherwise executable task in `todo` until the dispatcher reconciliation pass at
or after that instant.

Archival is an independent boolean, not a status. Archiving hides a task without
erasing whether it completed or blocked.

### 3.2.1 Normative transition table

| From | Operation | To | Guard |
|---|---|---|---|
| create | save as idea | `triage` | none |
| create/`triage` | specify | `ready` or `todo` | eligibility is recomputed |
| `todo` | eligibility becomes true | `ready` | live assignee, due, all dependencies done |
| `ready` | eligibility becomes false | `todo` | assignment/dependency/schedule/board edit |
| `ready` | dispatcher claim | `running` | atomic claim and concurrency limits |
| `running` | worker `complete` | `done` | owning run id + structured handoff |
| `running` | block/fail/cancel/interrupt/protocol violation | `blocked` | owning run id |
| `blocked` | explicit unblock | `ready` or `todo` | eligibility is recomputed; never direct-run |
| `triage`/`todo`/`ready` | cancel | `blocked(cancelled)` | no active run |
| any non-running | archive | same status + `archived=1` | explicit human/API action |

`triage` may move back and forth with specified work through explicit human or
orchestrator edits. `done` is terminal except archival; revisions create a new
linked task. Archiving `running` is rejected until cancellation closes its run.
Every transition not listed above returns 409 and leaves the durable row
unchanged.

### 3.3 Tree versus dependency graph

Two relationships are persisted because they answer different questions:

- `parent_task_id`: decomposition tree — "this work is part of that goal."
- `task_dependencies`: execution DAG — "this task cannot start until those
  tasks are done."

Creating a child does not implicitly make it a dependency. The caller may add
both in one request, but the database concepts remain separate. Both
relationships reject self-links, cross-board links, and cycles.

Model-driven fan-out has hard guards: the default maximum decomposition depth
is 8, one run may create at most 32 tasks, and a board may have at most 500
non-archived, non-done tasks. These values are board settings within server-side
upper bounds. Human/API creation returns the same capacity error unless the
board limit is deliberately raised; no caller can bypass cycle/depth checks.

### 3.4 Run

`task_runs` is append-only. One row represents one attempt by one Agent in one
worker session. Reassignment or explicit unblock creates a new attempt; it never
overwrites the prior run.

Run state:

```text
running | completed | blocked | failed | cancelled | interrupted
```

`interrupted` means the worker process disappeared and the business outcome may
be unknown. It is never automatically retried.

### 3.5 Comment, event, handoff, artifact

- Comments are the durable human/agent discussion thread.
- Events are append-only machine audit records for every state/assignment/link/
  claim/heartbeat transition.
- A completed or blocked run stores a human-readable `summary` and structured
  JSON `metadata`.
- Declared artifacts are verified against the worker workspace and copied into
  durable task storage before an ephemeral workspace may be cleaned.

## 4. Persistence model

All timestamps are UTC ISO strings. Public ids use Owlery's existing 12-hex
style with prefixes in the UI only; database ids remain plain text.

### 4.1 `task_boards`

```sql
CREATE TABLE task_boards (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    working_dir TEXT NOT NULL,
    default_workspace_mode TEXT NOT NULL, -- shared|copy|git_worktree
    max_running INTEGER,
    max_running_per_agent INTEGER,
    max_tree_depth INTEGER NOT NULL DEFAULT 8,
    max_children_per_run INTEGER NOT NULL DEFAULT 32,
    max_open_tasks INTEGER NOT NULL DEFAULT 500,
    dispatch_enabled INTEGER NOT NULL DEFAULT 1,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX task_boards_live_name
ON task_boards(name) WHERE archived = 0;
```

`working_dir` is normalized and validated as an absolute existing directory at
write time. A task may override it only with another absolute path explicitly
chosen by the user; model-created tasks inherit the board path.

### 4.2 `tasks`

```sql
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    board_id TEXT NOT NULL REFERENCES task_boards(id) ON DELETE CASCADE,
    parent_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,                  -- triage|todo|ready|running|blocked|done
    assignee_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    origin_session_id TEXT,                -- plain ref; origin may be archived/deleted
    idempotency_key TEXT,
    scheduled_at TEXT,
    workspace_mode TEXT,                   -- NULL inherits board
    working_dir_override TEXT,
    current_run_id TEXT,                   -- deliberate non-FK claim token; see below
    blocked_kind TEXT,                     -- input|capability|failure|protocol|cancelled|interrupted
    blocked_reason TEXT,
    result_summary TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    created_by_kind TEXT NOT NULL,          -- user|agent|schedule|api
    created_by_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    archived_at TEXT
);

CREATE UNIQUE INDEX tasks_board_idempotency
ON tasks(board_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX tasks_dispatch
ON tasks(board_id, archived, status, priority DESC, created_at);
CREATE INDEX tasks_assignee
ON tasks(assignee_agent_id, archived, status);
CREATE INDEX tasks_parent ON tasks(parent_task_id);
```

`current_run_id` is a claim token as well as a pointer. A compare-and-swap from
`ready + NULL` to `running + run_id` is the single winner boundary. It
deliberately has no foreign key: adding one would create a circular
`tasks <-> task_runs` insertion dependency. Repository transactions and the
owning-run CAS keep both rows consistent, while boot reconciliation detects any
legacy/manual corruption.

### 4.3 `task_dependencies`

```sql
CREATE TABLE task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    created_by_kind TEXT NOT NULL,
    created_by_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on_task_id),
    CHECK (task_id != depends_on_task_id)
);
```

Application validation performs a recursive CTE before insert to reject DAG
cycles and verifies both tasks belong to the same board. Adding a dependency to
`ready` demotes it to `todo`. Adding one to `running` or `done` returns 409.

### 4.4 `task_runs`

```sql
CREATE TABLE task_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL,
    agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    session_id TEXT,                       -- plain ref; audit survives session deletion
    state TEXT NOT NULL,
    summary TEXT,
    metadata TEXT,                         -- JSON object
    error TEXT,
    workspace_mode TEXT NOT NULL,
    workspace_path TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    started_at TEXT,
    last_heartbeat_at TEXT,
    lease_expires_at TEXT,
    finished_at TEXT,
    UNIQUE (task_id, attempt_no)
);

CREATE UNIQUE INDEX task_runs_one_running
ON task_runs(task_id) WHERE state = 'running';
CREATE INDEX task_runs_task ON task_runs(task_id, attempt_no);
CREATE INDEX task_runs_active_workspace
ON task_runs(workspace_path) WHERE state = 'running';
```

The Agent id and workspace are snapshotted on the run so later board/task edits
cannot rewrite history.

### 4.5 `task_comments`

```sql
CREATE TABLE task_comments (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES task_runs(id) ON DELETE SET NULL,
    author_kind TEXT NOT NULL,             -- user|agent|system
    author_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX task_comments_task ON task_comments(task_id, created_at);
```

Comments are append-only. Corrections are new comments; the audit trail is not
edited in place.

### 4.6 `task_events`

```sql
CREATE TABLE task_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id TEXT NOT NULL REFERENCES task_boards(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE, -- NULL for board events
    run_id TEXT REFERENCES task_runs(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    actor_kind TEXT NOT NULL,
    actor_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    payload TEXT NOT NULL DEFAULT '{}',    -- JSON object
    created_at TEXT NOT NULL
);

CREATE INDEX task_events_task ON task_events(task_id, seq);
CREATE INDEX task_events_board ON task_events(board_id, seq);
```

Events power the task/board timeline and WebSocket catch-up. `task_id` is NULL
for board lifecycle/dispatcher events; `board_id` is denormalized
for cursor reads and is validated against the task inside every repository
transaction. Events do not replace the current-state columns in `tasks`.

### 4.7 `task_artifacts`

```sql
CREATE TABLE task_artifacts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    source_path TEXT NOT NULL,
    mime_type TEXT,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    UNIQUE (run_id, name)
);
```

Artifacts live under `<home_dir>/task-artifacts/<task>/<run>/`. Paths are
resolved and containment-checked; symlinks that escape the workspace are
rejected. Deleting an artifact removes its bytes but tombstones the metadata so
the run audit still records what once existed and its hash.

## 5. Transaction and concurrency rules

Task mutation requires real multi-statement transactions. Owlery's general
`Database` currently shares one `aiosqlite` connection, so an application-level
sequence of `execute()` calls is not an isolation boundary.

The main connection must also release SQLite's single-writer lock at every
persisted transcript-event boundary. Ordinary `append_message()` writes commit
before their event is broadcast; they are never batched across a model turn.
Otherwise a streaming worker (or any concurrent chat) could lock out the
TaskRepository connection for the entire duration of model/tool/MCP work, which
no bounded `busy_timeout` can make correct.

The Task Board therefore owns a small `TaskRepository` with a dedicated SQLite
connection to the same database file:

- WAL, foreign keys, and `PRAGMA busy_timeout = 5000` are enabled on both
  connections. Adding the timeout to Owlery's existing main connection is a
  required behavior change when the second writer is introduced, not merely a
  task-table migration detail.
- Every graph mutation and lifecycle transition uses `BEGIN IMMEDIATE`.
- The repository is the only writer of task tables.
- API, MCP, dispatcher, and recovery all call the same repository methods.
- Tests use temporary file databases; a second `:memory:` connection would be a
  different database and is forbidden for this subsystem.

Task transactions contain bounded SQLite work only. They never await model,
filesystem, network, or WebSocket operations while holding the WAL writer lock;
the busy timeout absorbs short contention rather than hiding long lock holders.

Critical repository operations are compare-and-swap:

- `claim_ready(task_id, run_id)` succeeds only for live `ready` + no current run
  + assigned + due + dependencies done.
- `complete_run(task_id, run_id, ...)` succeeds only when both task and run
  still name that active claim.
- `block_run`, `cancel_run`, and `interrupt_run` have the same ownership guard.
- Losing a CAS returns the durable current row and becomes HTTP 409, never a
  blind second mutation.

The transaction that completes a run:

1. updates the run;
2. updates the task and clears `current_run_id`;
3. inserts the event;
4. promotes newly unblocked dependents from `todo` to `ready`;
5. commits.

No model or filesystem operation happens inside the transaction.

## 6. Dispatcher and worker lifecycle

`TaskBoardManager` is a long-lived FastAPI collaborator, like
`BgTaskManager`/`ResearchManager`.

### 6.1 Dispatch

The dispatcher wakes from both an `asyncio.Event` (task mutation) and a bounded
periodic tick (recovery from a missed wake).

On each pass it:

1. reconciles `todo` tasks whose assignment, due time, and dependencies now
   permit `ready`;
2. skips boards whose durable `dispatch_enabled` flag is false, while leaving
   their eligible tasks visibly `ready`;
3. applies board-wide and per-Agent concurrency limits;
4. orders eligible work by priority descending, then creation ascending;
5. atomically claims one task and creates its `task_run`;
6. prepares its workspace;
7. creates a fresh session with `origin='task'`, the assigned Agent, and the
   run workspace;
8. records `session_id` on the run;
9. starts the assignment turn.

There is no automatic retry after step 7: model/tool side effects may have
occurred. Failures before the session starts are known-safe setup failures and
move the task to `blocked(kind='capability'|'failure')` with a precise reason.

### 6.2 Worker prompt

The task session receives a small assignment message and a system-prompt
protocol:

1. call `mcp__tasks__show()` before acting;
2. operate only in the supplied workspace;
3. use comments for durable intermediate findings;
4. heartbeat during long work;
5. call `complete` or `block` as the final board mutation;
6. never claim success only in prose;
7. never automatically retry interrupted external work.

The full task context returned by `show` includes:

- title/body, board, tree ancestry;
- dependency handoffs and artifacts;
- comment thread;
- prior run summaries/errors/metadata;
- current workspace and attempt number;
- originating-session reference when present.

### 6.3 Terminal protocol

`complete` validates and copies declared artifacts, then atomically completes
the run/task. `block` atomically closes the run and records kind, reason,
summary, and metadata.

The manager listens to session broadcasts:

- a session `result` is not itself terminal for the task run. A worker may end
  an intermediate turn while waiting for bg, delegation, research, a queued
  injection, a parked turn, or a pending approval/question;
- only after the session is idle **and** `worker_has_pending_work(session_id)`
  is false does a missing `complete`/`block` call become a protocol violation;
  the run then becomes `failed` and the task `blocked(protocol)`;
- a session error/timeout/cancel while still running closes the run with the
  matching state and blocks the task;
- if the MCP terminal call already won, a later session result/error does not
  rewrite the completed/blocked truth;
- the worker session auto-archives only after the turn is idle and every
  pending injection aimed at it is durable.

`worker_has_pending_work(session_id)` is one shared repository/manager predicate
used by terminal detection, lease handling, recovery, status API, and tests. It
is true when any of the following points at the worker session:

- an active/queued harness turn, waiting approval/question, or usage-limit park;
- a `pending`/`running` bg task;
- a running delegation round whose parent is the worker;
- a running research job;
- a pending `session_injections` row or queued prompt.

The check runs after SessionManager finishes its idle/queue transition, not in
the raw `result` callback. This makes a task attempt a sequence of related turns,
not a false one-turn wrapper.

### 6.4 Heartbeat and lease

Heartbeat updates `last_heartbeat_at`, extends `lease_expires_at`, and emits a
throttled progress/audit event. It is not the sole liveness signal.

The lease is kept alive by any of:

- a model-authored heartbeat;
- harness stream activity from an active worker turn;
- `worker_has_pending_work(session_id)` remaining true.

A long bg/delegation/research wait therefore cannot be killed merely because no
model process is currently able to call `heartbeat`. Only when the lease has
expired **and** all three liveness sources are absent does Owlery interrupt any
remaining worker process and mark the run `interrupted`, task
`blocked(interrupted)`. It is not returned to `ready` automatically. Only a
human or orchestrator may inspect the transcript/workspace and unblock it into
a new attempt.

### 6.5 Boot and shutdown

Task recovery participates in the existing session-injection barrier:

1. boot pauses session injection dispatch;
2. Task Board binds its broadcast listener;
3. bg/research recover;
4. task phase 1 changes prior `running` runs to `interrupted`, blocks their
   tasks, and records the terminal runs without archiving workers;
5. delegation recovery materializes nested delegation events;
6. task recovery idempotently ensures a terminal outbox source exists for every
   terminal run whose task points at an existing `origin_session_id`; this
   repairs a crash after the TaskRepository transaction committed but before
   the main Database connection inserted the notification intent. A deleted
   origin produces one durable `notification_unavailable` task event instead;
7. task phase 2 materializes every remaining pending injection aimed at an
   interrupted task worker directly into that transcript, then archives it;
8. centralized injection drain notifies live originating sessions;
9. the task dispatcher starts only after all recovery is complete.

The terminal notification source for an existing origin is deterministic:
`task:<task_id>:run:<run_id>:terminal`. `source_key` uniqueness makes both live
creation and boot reconstruction idempotent. New task tables have no historical
cutover rows, so every terminal run with a live origin is eligible for repair.

Shutdown stops dispatch first, prevents new claims, interrupts active task
workers through the normal task lifecycle, persists their terminal facts, and
leaves origin notifications pending for next boot. No worker may start after
teardown begins.

## 7. Workspace model

Every board has a default; each human-created task may override it.
Model-created tasks inherit and cannot inject arbitrary host paths.

### 7.1 `shared`

The worker uses the board directory directly. This is appropriate for
operational directories and intentionally shared knowledge. The dispatcher
refuses to run two `shared` tasks concurrently on the same canonical path unless
the board explicitly opts into that concurrency. The conflict index is global
across every board: two boards resolving to the same real path cannot evade the
guard merely because their dependency graphs are isolated.

### 7.2 `copy`

Owlery makes a durable filesystem copy under
`<home_dir>/task-workspaces/<task>/<run>/`. It reuses the safe-copy and cleanup
principles from `/fork` without copying conversation history.

The copy remains after completion/block/interruption so the user can inspect
side effects. Cleanup is explicit from the archived task drawer and refuses to
delete paths outside the configured task-workspace root.

### 7.3 `git_worktree`

For a clean Git repository, Owlery creates a dedicated worktree and branch
`owlery/task-<task>-run-<attempt>`. Dirty source repositories fail setup rather
than silently copying uncommitted state.

Completion records branch, HEAD, and porcelain status in run metadata. Owlery
does not auto-merge or auto-delete the worktree. The UI exposes the exact
commands/state and requires explicit cleanup after changes are merged or
discarded.

## 8. MCP surface

New built-in server key: `tasks`.

### 8.1 Worker-scoped tools

When `OWLERY_TASK_ID`/`OWLERY_TASK_RUN_ID` are present, the server derives the
task and caller identity from trusted environment variables:

- `show()`
- `comment(body)`
- `heartbeat(note?)`
- `complete(summary, metadata?, artifacts?)`
- `block(reason, kind='input', summary?, metadata?)`
- `create(title, assignee, body?, parent_id?, dependencies?, priority?)`
- `link(task_id, depends_on_task_id)`

Terminal tools can mutate only the current task/run. Child creation/linking is
limited to the current board and is rejected when it would exceed the board's
tree-depth, per-run child-count, or open-task cap. The repository counts rows,
not model claims, so prompt text cannot opt out of these guards.

### 8.2 Orchestrator tools

Normal sessions whose Agent enables the `tasks` MCP server receive:

- `list(board_id?, status?, assignee?, parent_id?, limit?)`
- `show(task_id)`
- `create(...)`
- `triage(task_id)`
- `specify(task_id, body?)`
- `assign(task_id, agent_name?)`
- `comment(task_id, body)`
- `link(task_id, depends_on_task_id)`
- `unlink(task_id, depends_on_task_id)`
- `unblock(task_id, comment?)`
- `cancel(task_id, reason?)`

The API resolves Agent names case-insensitively and rejects ambiguous matches,
mirroring `ask_agent`. Model input never supplies the caller session id or
author identity.

`tasks` is added to the new-Agent default MCP set and receives a one-time,
user-version-gated backfill. A user may later disable it per Agent without boot
silently re-adding it.

## 9. REST and WebSocket contract

### 9.1 REST

```text
GET/POST                 /api/task-boards
GET/PATCH                /api/task-boards/{board_id}
POST                     /api/task-boards/{board_id}/archive
POST                     /api/task-boards/{board_id}/unarchive
GET                      /api/task-boards/{board_id}/dispatcher
POST                     /api/task-boards/{board_id}/dispatcher/pause
POST                     /api/task-boards/{board_id}/dispatcher/resume
GET                      /api/task-boards/{board_id}/events?after_seq={seq}

GET/POST                 /api/task-boards/{board_id}/tasks
GET/PATCH                /api/tasks/{task_id}
POST                     /api/tasks/{task_id}/assign
POST                     /api/tasks/{task_id}/dependencies
DELETE                   /api/tasks/{task_id}/dependencies/{dependency_id}
POST                     /api/tasks/{task_id}/comments
POST                     /api/tasks/{task_id}/triage
POST                     /api/tasks/{task_id}/specify
POST                     /api/tasks/{task_id}/ready
POST                     /api/tasks/{task_id}/block
POST                     /api/tasks/{task_id}/unblock
POST                     /api/tasks/{task_id}/cancel
POST                     /api/tasks/{task_id}/archive
POST                     /api/tasks/{task_id}/unarchive
GET                      /api/tasks/{task_id}/runs
GET                      /api/tasks/{task_id}/events
GET/DELETE               /api/tasks/{task_id}/artifacts[/{artifact_id}]
GET                      /api/task-boards/{board_id}/tree
```

Task create supports a board-scoped `idempotency_key`. Repeating a create with
the same key returns the existing row with 200; the first returns 201.

ETag/version or `updated_at` preconditions protect human edits from stale
drawer state. Lifecycle verbs use CAS and return 409 with the current task on a
lost race.

`triage`/`specify`/`ready` are guarded lifecycle verbs implementing the
normative transition table; `PATCH` cannot smuggle a direct status change.
Archive/unarchive preserve the underlying status and history.

The dispatcher status response contains the durable enabled/paused flag,
running counts, last completed tick, and last error. Pause/resume changes the
board flag transactionally and emits an event; pausing prevents new claims but
does not interrupt already-running tasks. Task artifacts are produced only by
validated worker `complete` calls. There is no ambiguous manual-upload POST in
this feature.

### 9.2 WebSocket

Every committed `task_events` row broadcasts:

```json
{
  "type": "task_event",
  "board_id": "...",
  "task_id": "...",
  "event": {"seq": 123, "kind": "...", "payload": {}}
}
```

The client tracks the last event seq per board. Reconnect calls the board-level
events endpoint after that cursor, then falls back to a board snapshot if the
cursor was compacted.
The database row commits before broadcast; WebSocket is a cache invalidation
hint, never the source of truth.

## 10. Browser experience

The sidebar gains a first-class **Tasks** entry next to Schedules. It opens a
full main-area surface, not a modal.

### 10.1 Board view

- board selector and create/archive/settings controls; the create/settings
  dialog edits name, description, working dir, default workspace, the
  concurrency caps (`max_running` and `max_running_per_agent` — each a whole
  number ≥ 1, or blank for unlimited), and the Git-delivery defaults;
- six columns: Triage, Todo, Ready, Running, Blocked, Done;
- cards show title, assignee seal, priority, child/dependency counts, heartbeat/
  blocked age, workspace mode, and latest run state;
- drag/drop only invokes legal lifecycle APIs; it never writes status directly;
- filters for assignee, priority, text, archived, and "mine";
- global/per-Agent running counters and dispatcher-paused indicator.

### 10.2 Tree view

A toggle switches to the decomposition tree:

- expandable parent/child rows;
- status and assignee on every node;
- dependency edges summarized separately so tree nesting is never mistaken for
  an execution prerequisite;
- orphan roots remain visible if a parent was archived;
- selecting any node opens the same task drawer.

### 10.3 Task drawer

The drawer contains:

- editable title/body/priority/assignee/schedule/workspace controls;
- parent, children, dependencies, and dependents;
- comment composer and chronological thread;
- run timeline with Agent, timestamps, heartbeat, state, summary, structured
  evidence, cost, workspace, and "Open session";
- artifacts with safe download/open actions;
- precise unblock/cancel/archive controls and conflict messages.

Mobile uses a single-column board selector and horizontally scrollable columns;
the drawer becomes full-screen.

## 11. Chat, notification, and usage integration

When a task is created from an Owlery session, `origin_session_id` is stored.
Terminal user-relevant events use the existing durable outbox:

```text
task:<task_id>:run:<run_id>:terminal
```

The terminal run state selects the completed/blocked/failed/cancelled/
interrupted card content. The card includes task id, title, Agent,
summary/reason, and a browser action to open the task. Delivery means the event
is in the transcript, not that the origin model successfully consumed it.

Task worker usage needs no new billing path: every run links its worker
`session_id`, and `turn_usage` already records Agent/session cost. The task API
aggregates run cost from that ledger; it does not copy token counts into tasks.

Schedules remain a separate automation primitive. A scheduled Agent may create
idempotent tasks through the MCP/API, but schedules do not become task rows and
tasks do not embed cron expressions.

## 12. Invariants

1. A task is user-visible intent, never a polymorphic execution row.
2. Task tree and dependency DAG are separate and cycle-free.
3. At most one `running` run exists per task.
4. Every lifecycle mutation and its audit event commit atomically.
5. A task cannot become `ready` without a live assignee, a due time (`NULL`
   means due now), and completed dependencies. A paused dispatcher leaves
   already-eligible tasks `ready` but cannot claim them.
6. `done` requires a completed run/human handoff; plain model prose is not
   completion.
7. `interrupted` is not `failed`, and neither state auto-retries work that may
   have external side effects.
8. An active claim is changed only by its owning run id.
9. Worker identity, task identity, and author identity come from trusted server
   context, never model arguments.
10. WebSocket loss cannot lose task truth.
11. Origin-session notifications use `session_injections` and inherit its
    deduplication and boot barrier.
12. No cleanup operation follows an unresolved or user-supplied path outside
    Owlery's validated workspace/artifact roots.
13. Worker terminal detection and lease expiry use the same
    `worker_has_pending_work` predicate. It includes the short durable window
    where async work is terminal but its deterministic outbox source has not
    yet been created; an asynchronous wait is neither a protocol violation nor
    evidence of death.
14. Every terminal run with an existing origin session eventually has exactly
    one deterministic terminal outbox source, reconstructed on boot when
    necessary. A missing origin is recorded as an undeliverable audit event.

## 13. Failure matrix

| Failure | Durable outcome |
|---|---|
| Claim race | One CAS winner; loser sees 409/current row |
| Agent archived before claim | Task remains/demotes `todo`, event explains why |
| Workspace preparation fails | Run `failed`, task `blocked(capability)` |
| Session creation fails before turn | Run `failed`, task `blocked(failure)` |
| Idle worker has no terminal task tool and no tracked pending work | Run `failed`, task `blocked(protocol)` |
| Worker turn ends while bg/delegation/research/injection/park/question remains pending | Run stays `running`; wait is tracked, not a protocol failure |
| Worker asks for input | Run `blocked`, task `blocked(input)`, origin notified |
| User cancels running task | Session interrupted, run `cancelled`, task `blocked(cancelled)` |
| Server restarts mid-run | Run `interrupted`, task `blocked(interrupted)`, no retry |
| Terminal run commits before outbox intent is created | Boot reconstructs deterministic terminal source |
| Outbox intent commits before origin delivery | Outbox boot recovery delivers once |
| Origin session deleted | Task remains correct; one `notification_unavailable` event records why no outbox row exists |
| Origin session archived | Outbox intent exists and becomes failed without changing task truth |
| Worker workspace has partial edits | Workspace preserved and surfaced |
| Dependency completes during dispatcher sleep | Completion transaction promotes child and wakes dispatcher |
| Dependency/task link cycle | Transaction rejected, graph unchanged |

## 14. Migration and compatibility

- Additive tables and `sessions.origin='task'`; no existing row is backfilled.
- `tasks` MCP default is added with a new `PRAGMA user_version` migration step,
  not an every-boot membership repair.
- `TaskRepository` opens only after `Database.initialize()` has installed the
  schema.
- Introducing the second writer also changes the main `Database` connection to
  use `PRAGMA busy_timeout = 5000`; `database.py` therefore owns additive schema
  **and** this bounded contention setting. It also commits each transcript
  message before broadcast so no model turn holds the writer lock while waiting
  on tools or MCP callbacks.
- Old clients ignore `task_event` WebSocket messages.
- OpenAPI contracts are regenerated after routers/models land.
- Board/task ids are opaque; no task id is derived from a session or run id.

## 15. Implementation map

Backend:

```text
server/task_board/
  models.py          domain enums and records
  repository.py      dedicated-connection transactions and graph queries
  manager.py         dispatcher, recovery, broadcast listener, notifications
  workspaces.py      shared/copy/git-worktree preparation and cleanup
  prompts.py         worker assignment/context rendering
server/mcp_servers/tasks.py
server/routers/task_boards.py
```

Frontend:

```text
web/src/components/tasks/
  TaskBoardPage.tsx
  BoardToolbar.tsx
  KanbanColumns.tsx
  TaskTree.tsx
  TaskCard.tsx
  TaskDrawer.tsx
  TaskRunTimeline.tsx
  TaskComments.tsx
web/src/stores/taskStore.ts
web/src/api/tasks.ts
```

Existing integration points:

- `server/database.py`: additive schema/migration plus main-connection
  `busy_timeout`;
- `server/main.py`: bind, phased recovery, centralized start/stop;
- `server/session_manager.py`: `origin='task'` auto-archive eligibility;
- `server/harness/assembly.py`: tasks MCP and worker env/system prompt;
- `server/models.py`: API DTOs/default MCP set;
- `web/src/App.tsx`: chat/tasks main-surface switch;
- `web/src/hooks/useWebSocket.ts`: task-event cache invalidation;
- `web/src/api/contracts.ts`: generated types.

## 16. Test plan

### 16.1 Repository and state machine

- board/task CRUD and archive behavior;
- every legal and illegal row in the normative transition table;
- tree-cycle and dependency-cycle rejection;
- cross-board link rejection;
- idempotent create;
- ready eligibility and dependent promotion;
- atomic claim race across two repository connections;
- bounded main/repository writer contention under `busy_timeout`;
- stale CAS returns 409/current row;
- one-running-run partial index;
- completion/block transaction includes run/task/event;
- done immutability;
- archived Agent and deleted origin behavior.

### 16.2 Manager and recovery

- priority/fairness and both concurrency caps;
- durable board pause/resume leaves ready rows unclaimed;
- each workspace mode, containment, cleanup refusal, dirty-Git refusal;
- worker session gets correct Agent/backend/credential/memory/tool policy;
- terminal MCP versus session-result race;
- multi-turn worker waits for bg/delegation/research/injections without false
  protocol failure;
- protocol violation only after idle + no tracked pending work;
- heartbeat/stream/pending-work lease extension and true lease timeout;
- cancel propagation;
- boot phase ordering with a task worker that has nested delegation/bg/research
  injections;
- shutdown no-new-worker barrier;
- origin outbox exactly-once delivery, including terminal-run/missing-source
  boot reconstruction.

### 16.3 MCP/API

- worker own-task scope cannot mutate another task;
- orchestrator identity derived from session;
- ambiguous/missing Agent assignment errors;
- artifact traversal/symlink/size checks;
- ETag/update conflict;
- all lifecycle 409 shapes;
- tree-depth/per-run-child/open-task fan-out guards;
- board events cursor and dispatcher pause/status endpoints;
- OpenAPI contract generation.

### 16.4 Frontend

- reducer/store event replay and snapshot fallback;
- legal/illegal drag transitions;
- board filters and counts;
- tree rendering distinct from dependencies;
- live comments/runs/heartbeat;
- mobile board/drawer;
- archived/history visibility;
- task card injected into originating chat;
- E2E: create dependency chain, dispatch two Agents, block/unblock, complete,
  review handoff, inspect sessions/artifacts;
- restart E2E: active worker becomes blocked/interrupted without a duplicate
  attempt.

All four repository suites in `CLAUDE.md` run after every implementation
change; the final task-board E2E includes one fake deterministic chain and one
real-model smoke proving the worker uses the MCP lifecycle.

## 17. Delivery order

This is one feature delivered completely, but implementation is staged so each
review has a coherent invariant boundary:

1. schema + `TaskRepository` + exhaustive state/graph transaction tests;
2. tasks MCP + REST + OpenAPI contracts as pure repository consumers;
3. workspace manager + `TaskBoardManager` dispatcher/recovery, now exercised
   against the real terminal tool surface;
4. origin notifications + delegation/bg/research boot-order integration;
5. Task Board page, tree, drawer, live store, and chat card;
6. full unit/E2E matrix, architecture docs, independent review.

The UI is not enabled until all six stages are present. No temporary alternate
state machine or throwaway API is shipped between stages.

## 18. What this deliberately defers

These require a separate product decision or a real second use case:

- cross-board dependencies;
- automatic Git merging, PR creation, or worktree deletion;
- organization/role permissions beyond Owlery's current single-user token and
  per-Agent tool policy;
- recurring task definitions (Schedules create idempotent tasks instead);
- budgets and automatic model/provider routing;
- arbitrary webhook/plugin workflow steps;
- automatic retry of any attempt that reached a model/tool process.

They are not necessary to make the Task Board internally complete, safe, and
useful. None is represented by a placeholder column or half-active code path.

## 19. Acceptance criteria

The feature is done only when:

1. a user can create a board, build a task tree and dependency DAG, assign named
   Agents, and observe legal transitions in real time;
2. ready work dispatches exactly once into an auditable worker session;
3. workers can comment, heartbeat, block, create/link children, and complete
   with structured evidence/artifacts through MCP;
4. humans can inspect, comment, cancel, reassign, and unblock from the browser;
5. dependency completion wakes downstream tasks without polling-only races;
6. restart/shutdown never lies about running work, duplicates an attempt, or
   silently auto-retries unknown side effects;
7. task, run, event, comment, artifact, session, delegation, and usage history
   remain navigable after completion and archival;
8. task notifications inherit the durable injection contract;
9. all tests and independent architecture/code reviews pass;
10. existing delegation, bg, research, schedule, session, and memory behavior
    remains unchanged outside their explicit integration points.
