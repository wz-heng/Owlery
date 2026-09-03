from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

# Built-in MCP servers attached to the Default Agent (and the default for
# any newly-created agent). Kept here so the migration backfill and the
# CREATE TABLE default stay in lock-step.
_DEFAULT_MCP_SERVERS = ["ask", "bg", "ask_agent", "research", "tasks", "skills"]
_DEFAULT_MCP_SERVERS_JSON = json.dumps(_DEFAULT_MCP_SERVERS)

_SCHEMA = """
-- Agents are the durable definition of an assistant (agent-refactor.md §4.1):
-- identity + system prompt + model + credential + built-in MCP set + tool
-- policy. They OWN sessions, schedules and bridge bindings. Memory (the
-- north star) hangs off the agent_id later; not in this refactor.
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,                    -- 12-char hex, same scheme as sessions
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    model TEXT,                             -- e.g. "claude-opus-4-7"; null = backend default
    credential_id TEXT REFERENCES backend_credentials(id) ON DELETE SET NULL,
    backend TEXT NOT NULL DEFAULT 'claude-code',  -- default harness for new sessions
    mcp_servers TEXT NOT NULL DEFAULT '["ask","bg","ask_agent","research","tasks","skills"]',
                                            -- JSON array of built-in Owlery MCP server ids.
    tool_allow TEXT NOT NULL DEFAULT '',    -- newline-separated tool/MCP names; empty = allow all
    tool_deny  TEXT NOT NULL DEFAULT '',    -- newline-separated; deny takes precedence over allow
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS agents_name_unique ON agents(name) WHERE archived = 0;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    working_dir TEXT NOT NULL,
    created_at TEXT NOT NULL,
    claude_session_id TEXT,                -- backend resume id: a Claude session id
                                           -- OR a Codex thread_id (backend-agnostic;
                                           -- name kept for back-compat — codex-backend.md §4.3)
    archived INTEGER NOT NULL DEFAULT 0,   -- hidden from default list; row kept for history
    agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,  -- owner; nullable in SQLite, required by API
    origin TEXT NOT NULL DEFAULT 'user',   -- 'user' | 'schedule' | 'bridge' | 'delegation'
    backend TEXT NOT NULL DEFAULT 'claude-code',  -- 'claude-code' | 'codex' (codex-backend.md §4.1)
    -- Session-level model override (budget-model-routing.md §4.1). NULL means
    -- "inherit the owning agent's model, else the backend default"; a value
    -- wins over the agent's model in resolve_model().
    model TEXT,
    -- Agent-to-agent: a delegation child session points at the parent
    -- session it was spawned from. SET NULL on parent delete (orphan beats
    -- mass-delete; sessions are precious). NULL on every non-delegation
    -- session. (agent-collaboration.md §4.1)
    parent_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    -- The original prompt for a delegation, stored verbatim so the UI can
    -- render "Octo asked: «…»" without rummaging through the first message.
    -- NULL on every non-delegation session.
    delegation_request TEXT,
    -- Session tree-rewind / fork (session-rewind.md §4). A fork is a
    -- clone of a parent session up to (but not including) a chosen user
    -- message. All NULL/0 on non-fork sessions. forked_from_session_id is a
    -- PLAIN reference (no FK action): parent delete leaves it dangling so the
    -- UI can still render "forked from (deleted session)" and the orphan
    -- bucket in buildForkTree anchors correctly (§5.5).
    forked_from_session_id TEXT,           -- parent session id (dangling-ok)
    fork_after_seq INTEGER,                -- last copied seq; rewound user msg
                                           -- lives at fork_after_seq+1 on parent
    fork_needs_replay INTEGER NOT NULL DEFAULT 0,  -- HISTORY_REPLAY backends
                                           -- (Codex) until first result lands
    fork_metadata TEXT,                    -- EPHEMERAL JSON (prefilled prompt,
                                           -- side-effect summary, first-turn
                                           -- note, label); cleared after first
                                           -- result
    fork_revert_record TEXT,               -- DURABLE JSON safe-revert outcome;
                                           -- NEVER cleared
    fork_status TEXT                       -- 'initializing'|'reverting'|'ready'
                                           -- crash-recovery marker; NULL on
                                           -- non-fork rows
);
-- NOTE: the index on forked_from_session_id is created in _apply_migrations
-- (after the additive ALTER), NOT here — a legacy DB reaching this script
-- already has a `sessions` table, so CREATE TABLE IF NOT EXISTS is a no-op and
-- the column wouldn't exist yet at _SCHEMA time.

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    type TEXT NOT NULL,
    content TEXT,
    tool_name TEXT,
    tool_input TEXT,
    tool_use_id TEXT,
    is_error INTEGER,
    session_id_ref TEXT,
    cost REAL,
    attachments TEXT,                       -- JSON list[AttachmentMetadata], null when none
    -- Per-turn git anchor captured when a user message row is written
    -- (session-rewind.md §4 + §5.6.3). Powers the safe-revert preflight.
    git_head TEXT,                          -- `git rev-parse HEAD`; NULL when not a git repo
    git_status_clean INTEGER,               -- 1 iff `git status --porcelain` was empty
    -- Durable async delivery identity.  A non-null value points at the
    -- session_injections row whose prompt became this user message.  The
    -- partial unique index is installed after additive migrations so legacy
    -- databases gain the column before SQLite parses the index.
    injection_id TEXT,
    -- Wall-clock stamp at persist time (attempt-replay.md §3.1 point 1).
    -- Powers turn-internal timelines and tool-call durations. Stamped on
    -- every new write; NULL on rows written before this column existed —
    -- never backfilled (no reliable source of truth for the real time).
    created_at TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);

-- Durable inbox/outbox boundary for system-produced turns (background task
-- results, research reports, delegation questions/replies).  `pending` means
-- the intent is durable but the corresponding user-message row is not;
-- `delivered` is stamped atomically with that message insert.  This deliberately
-- models delivery into the transcript, not whether a model turn subsequently
-- finished consuming it.
CREATE TABLE IF NOT EXISTS session_injections (
    id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', -- pending|delivered|failed
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    error TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_session_injections_pending
  ON session_injections(status, created_at);

-- One durable row per delegation ROUND.  `delegation_id` remains the public
-- child-session continuation handle; `run_id` distinguishes repeated
-- follow-ups in that same session so audit history is append-only.
CREATE TABLE IF NOT EXISTS delegation_runs (
    run_id TEXT PRIMARY KEY,
    delegation_id TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    request TEXT NOT NULL,
    start_seq INTEGER NOT NULL,
    state TEXT NOT NULL,                    -- running|completed|failed|cancelled|interrupted
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (delegation_id) REFERENCES sessions(id) ON DELETE CASCADE,
    UNIQUE (delegation_id, round_no)
);

CREATE INDEX IF NOT EXISTS idx_delegation_runs_session
  ON delegation_runs(delegation_id, round_no);

-- A schedule belongs to the Agent ("every morning, summarize my inbox"),
-- not to a throwaway thread. Each fire materializes a fresh session under
-- the agent (scheduler.py). No persistent session_id here anymore.
--
-- Recurrence is exactly one of:
--   * interval_seconds  — fire every N seconds (APScheduler interval trigger)
--   * cron + timezone   — fire on a 5-field crontab in that tz (cron trigger)
-- recurrence_label is the human-readable description shown in the UI (the AI
-- parser supplies it for natural-language schedules; the interval fast-path
-- derives it). Nullable for legacy rows — the UI falls back to formatting
-- interval_seconds.
-- origin_session_id: when a schedule is created from the `/schedule` chat
-- command it remembers the session it was typed in, so each fire appends the
-- run into that same conversation (the result lands where the user is looking)
-- instead of a throwaway session. Nullable — agent/API-created schedules have
-- none and fall back to a fresh schedule-origin session. No FK: liveness is
-- decided at fire time by session_manager.get_session, so a stale pointer (the
-- origin session was deleted/archived) harmlessly degrades to the fallback.
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
    origin_session_id TEXT,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    interval_seconds INTEGER,
    cron TEXT,
    timezone TEXT,
    recurrence_label TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    run_at TEXT  -- nullable ISO datetime; when set, fires once at that time then auto-deletes
);

-- (platform, chat_id) binds durably to an AGENT. session_id is demoted to a
-- sticky pointer at the currently-open thread (nullable; rolls as sessions
-- come and go). A chat that has never opened a session has session_id NULL.
CREATE TABLE IF NOT EXISTS bridge_mappings (
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
    session_id TEXT,
    -- 0 = quiet (default): only the agent's natural-language replies, errors
    -- and approval prompts reach the chat. 1 = verbose: tool activity too.
    verbose INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, chat_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS backend_credentials (
    id TEXT PRIMARY KEY,
    backend TEXT NOT NULL,                 -- "claude-code" | "codex" | …
    label TEXT NOT NULL,
    auth_type TEXT NOT NULL,               -- "api_key" | "oauth"
    secret_encrypted TEXT NOT NULL,        -- LEGACY: kept for back-compat reads
                                           -- during the storage-split rollout.
                                           -- New writes go into credential_secrets.
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active', -- "active" | "needs_reconnect"
    token_expires_at TEXT,                 -- ISO8601, null for non-expiring keys
    needs_reconnect INTEGER NOT NULL DEFAULT 0,
    last_refresh_error_code TEXT           -- see oauth_errors.RefreshErrorCode
);

CREATE INDEX IF NOT EXISTS idx_credentials_backend
  ON backend_credentials(backend);

-- Storage split (Steal Plan B-4): secrets live in their own table so a
-- future `serverOnly` flag can keep refresh tokens out of subprocess env,
-- and so we can join-or-not on the encrypted blob depending on the caller.
CREATE TABLE IF NOT EXISTS credential_secrets (
    credential_id TEXT PRIMARY KEY,
    secret_encrypted TEXT NOT NULL,
    FOREIGN KEY (credential_id) REFERENCES backend_credentials(id)
        ON DELETE CASCADE
);

-- Connectors (connectors.md) — first-class third-party MCP tools the user
-- installs once (OAuth) and an agent calls during a turn. Two-layer model
-- mirroring backend_credentials: a metadata row + a split-out encrypted
-- secret. Unlike credentials there is no legacy in-table secret column — the
-- token blob lives ONLY in connector_installation_secrets.
CREATE TABLE IF NOT EXISTS connector_installations (
    id TEXT PRIMARY KEY,                   -- 12-char hex
    kind TEXT NOT NULL,                    -- 'gmail' | 'github' | …
    label TEXT NOT NULL,                   -- 'archeryue7@gmail.com'
    auth_type TEXT NOT NULL,               -- 'oauth' | 'api_key'
    external_account_id TEXT,              -- email / github "login:id" / workspace id
    scopes TEXT,                           -- JSON list of granted OAuth scopes
    enable_by_default INTEGER NOT NULL DEFAULT 0,  -- auto-enable on newly-created agents
    needs_reconnect INTEGER NOT NULL DEFAULT 0,
    token_expires_at TEXT,                 -- ISO8601, null = non-expiring
    last_refresh_error_code TEXT,          -- mirrors backend_credentials
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_connector_installations_kind
  ON connector_installations(kind);

-- Dedup: one installation per (kind, external account). The install flow
-- upserts on this — re-authorizing the same account overwrites rather than
-- duplicating. Partial index so rows mid-install (identity not yet known)
-- don't collide on a shared NULL.
CREATE UNIQUE INDEX IF NOT EXISTS connector_installations_account_unique
  ON connector_installations(kind, external_account_id)
  WHERE external_account_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS connector_installation_secrets (
    installation_id TEXT PRIMARY KEY,
    secret_encrypted TEXT NOT NULL,
    FOREIGN KEY (installation_id) REFERENCES connector_installations(id)
        ON DELETE CASCADE
);

-- AGENT-scoped enablement (connectors.md revision 2026-05-20 + agent-refactor
-- §5.5): a row means "this agent has this installation turned on". The
-- effective MCP set for a turn is the agent's built-in mcp_servers ∪ its
-- enabled connectors. Cascades on both sides — deleting an agent or an
-- installation drops the link.
CREATE TABLE IF NOT EXISTS agent_connectors (
    agent_id TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    PRIMARY KEY (agent_id, installation_id),
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE,
    FOREIGN KEY (installation_id) REFERENCES connector_installations(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agent_connectors_agent
  ON agent_connectors(agent_id);

-- Per-kind OAuth *client* credentials (the app registered with the provider),
-- set in-app so a connector works without editing env + restarting. client_id
-- is not secret; the secret is encrypted like connector tokens. When there's
-- no row, resolution falls back to env (OWLERY_<KIND>_OAUTH_CLIENT_ID/_SECRET).
CREATE TABLE IF NOT EXISTS connector_oauth_clients (
    kind TEXT PRIMARY KEY,                 -- 'github' | 'gmail' | …
    client_id TEXT NOT NULL,
    client_secret_encrypted TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- User-defined ("custom") connectors: a brand-new connector kind added
-- entirely from the browser, no server code. The OAuth *client* creds live in
-- connector_oauth_clients (same as built-ins); this row holds the definition
-- the generic OAuth provider + generic MCP server read.
CREATE TABLE IF NOT EXISTS custom_connectors (
    kind TEXT PRIMARY KEY,                 -- user-chosen slug, e.g. 'linear'
    display_name TEXT NOT NULL,
    authorize_url TEXT NOT NULL,
    token_url TEXT NOT NULL,
    scopes TEXT,                           -- JSON list of OAuth scopes
    pkce INTEGER NOT NULL DEFAULT 0,
    api_base TEXT NOT NULL,                -- base URL the agent's request tool calls
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Async notification targets. Each row is one
-- destination Owlery can poke when a session transitions to idle
-- (and, later, when an AskUserQuestion is pending / a schedule fails).
-- `config` is a JSON blob whose shape depends on `type` (e.g. for
-- type='webhook': {"url": "https://…"}).
CREATE TABLE IF NOT EXISTS notifiers (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,                    -- 'webhook' | future: 'email', 'browser_push'
    label TEXT NOT NULL,
    config TEXT NOT NULL,                  -- JSON
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- Cross-turn background tasks. The model calls `bg_run(cmd)` via the
-- bg MCP server; we persist a row here, spawn the subprocess, and on
-- completion synthesize a follow-up user message in the session so the
-- model is told "your bg task finished, here's the result" in its next
-- turn. The whole point is that the bg subprocess lives in the
-- long-running FastAPI process — independent of any one claude --print
-- invocation — so it survives turn boundaries the way Bash's
-- run_in_background does not.
--
-- stdout/stderr are capped (see server.bg_tasks.MAX_STREAM_BYTES);
-- excess content is truncated from the head with a `…[truncated N bytes]`
-- prefix so the model sees the most recent output.
CREATE TABLE IF NOT EXISTS bg_tasks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    command TEXT NOT NULL,
    description TEXT,
    working_dir TEXT NOT NULL,
    status TEXT NOT NULL,                  -- 'pending'|'running'|'completed'|'failed'|'cancelled'|'interrupted'
    exit_code INTEGER,
    stdout TEXT NOT NULL DEFAULT '',
    stderr TEXT NOT NULL DEFAULT '',
    truncated INTEGER NOT NULL DEFAULT 0,  -- bool: at least one stream hit the cap
    started_at TEXT NOT NULL,
    completed_at TEXT,
    -- New tasks require one durable session_injections source. Legacy rows
    -- gain this column with 0 during migration so old output is not replayed.
    delivery_required INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_bg_tasks_session
  ON bg_tasks(session_id, started_at);

-- Native deep-research jobs (native-deep-research.md §6). A job runs the
-- fan-out pipeline as a tracked async task; completion and delivery are
-- tracked SEPARATELY (a job can be `completed` while its report is still
-- queued behind an active turn). New-table-only — CREATE IF NOT EXISTS is a
-- no-op migration on existing DBs.
CREATE TABLE IF NOT EXISTS research_jobs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL,                  -- running|completed|failed|cancelled|interrupted
    phase TEXT,                            -- scope|search|verify|synthesize|done
    error TEXT,
    report_path TEXT,
    cost REAL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    injection_status TEXT NOT NULL DEFAULT 'pending',  -- pending|delivered|failed
    injected_at TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_research_jobs_session
  ON research_jobs(session_id, created_at);

-- Per-turn consumption ledger (usage-tracking.md §3). One row per completed
-- turn ('turn') and one per finished deep-research job ('research'). No FK
-- on session_id: rows must survive session deletion — the tokens were
-- already spent, and the ledger feeds subscription-limit awareness later.
-- New-table-only — CREATE IF NOT EXISTS is a no-op migration.
CREATE TABLE IF NOT EXISTS turn_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,               -- ISO-8601 UTC, stamped at capture
    origin TEXT NOT NULL DEFAULT 'turn',    -- 'turn' | 'research'
    session_id TEXT NOT NULL,
    agent_id TEXT,                          -- denormalized owner at capture time
    backend TEXT NOT NULL,                  -- 'claude-code' | 'codex'
    model TEXT,                             -- session's configured model; NULL = default
    cost REAL,                              -- USD; NULL when the backend reports none (codex)
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    is_error INTEGER NOT NULL DEFAULT 0,
    model_usage TEXT,                       -- Claude modelUsage JSON; NULL otherwise
    -- Turn anchor (attempt-replay.md §3.1 point 4): the `messages.seq` of
    -- this turn's `result` row, so cost/tokens can be placed on the replay
    -- timeline. NULL for research-origin rows (no owning turn) and for rows
    -- written before this column existed.
    message_seq INTEGER
);

CREATE INDEX IF NOT EXISTS idx_turn_usage_agent_time
  ON turn_usage(agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_turn_usage_session
  ON turn_usage(session_id);

-- Turn termination invariant (attempt-replay.md §3.1 point 2 — the spine of
-- the replay feature): every HarnessRun, however it ends — clean result,
-- CLI crash, SIGTERM/SIGKILL escalation, watchdog timeout, user interrupt —
-- writes exactly one row here from the single `finally:` choke point in
-- SessionManager._run_backend. No turn is allowed to die unexplained.
-- New-table-only — CREATE IF NOT EXISTS is a no-op migration.
CREATE TABLE IF NOT EXISTS harness_exits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    -- Last `messages.seq` persisted before this HarnessRun ended; NULL if
    -- none were ever written (e.g. the subprocess never started).
    message_seq INTEGER,
    -- 'completed'|'process_error'|'watchdog_idle'|'watchdog_overall'|
    -- 'interrupted'|'start_failed'. See SessionManager._classify_harness_exit.
    reason TEXT NOT NULL,
    exit_code INTEGER,                     -- POSIX exit status; NULL if killed by signal
    signal INTEGER,                        -- signal number that ended the process; NULL otherwise
    escalation TEXT,                       -- NULL|'sigterm'|'sigkill': did stop() have to force it
    reason_detail TEXT NOT NULL DEFAULT '{}',  -- JSON: e.g. watchdog {"limit": 300}
    stderr_tail TEXT,                      -- truncated tail of the process's stderr
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_harness_exits_session
  ON harness_exits(session_id, created_at);

-- Self-set spend gates (budget-model-routing.md §3.1). A budget caps Claude
-- USD spend (turn_usage.cost) over a natural-calendar window, globally or per
-- agent. Global and per-agent budgets co-exist; the tighter one wins at the
-- pre-run checkpoint. `soft_warned_window` records the window-start key we last
-- surfaced a soft warning for, so the one-time-per-window warning never
-- re-fires within a window (a compare-and-set on write dedupes concurrent
-- turns). No FK on agent_id: a budget outliving an agent-delete is harmless —
-- it resolves to zero applicable spend — and the router already guards create.
CREATE TABLE IF NOT EXISTS budgets (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('global', 'agent')),
    agent_id TEXT,
    window TEXT NOT NULL CHECK (window IN ('daily', 'weekly', 'monthly')),
    limit_usd REAL NOT NULL CHECK (limit_usd > 0),
    soft_pct REAL NOT NULL DEFAULT 0.8 CHECK (soft_pct > 0 AND soft_pct <= 1),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    soft_warned_window TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (scope = 'global' AND agent_id IS NULL)
        OR (scope = 'agent' AND agent_id IS NOT NULL)
    )
);
-- At most one budget per (scope, agent, window): a global daily + global weekly
-- may co-exist, but not two global dailies.
CREATE UNIQUE INDEX IF NOT EXISTS budgets_global_window
  ON budgets(window) WHERE scope = 'global';
CREATE UNIQUE INDEX IF NOT EXISTS budgets_agent_window
  ON budgets(agent_id, window) WHERE scope = 'agent';

-- Durable intent/coordination layer (task-board.md). TaskRepository owns all
-- writes through a dedicated SQLite connection; this connection installs the
-- additive schema and may read it for integration/recovery.
CREATE TABLE IF NOT EXISTS task_boards (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    working_dir TEXT NOT NULL,
    default_workspace_mode TEXT NOT NULL
        CHECK (default_workspace_mode IN ('shared', 'copy', 'git_worktree')),
    max_running INTEGER CHECK (max_running IS NULL OR max_running > 0),
    max_running_per_agent INTEGER
        CHECK (max_running_per_agent IS NULL OR max_running_per_agent > 0),
    max_tree_depth INTEGER NOT NULL DEFAULT 8 CHECK (max_tree_depth > 0),
    max_children_per_run INTEGER NOT NULL DEFAULT 32
        CHECK (max_children_per_run > 0),
    max_open_tasks INTEGER NOT NULL DEFAULT 500 CHECK (max_open_tasks > 0),
    dispatch_enabled INTEGER NOT NULL DEFAULT 1
        CHECK (dispatch_enabled IN (0, 1)),
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    -- Git delivery closure (task-git-delivery.md §17). Safe defaults mean an
    -- existing board keeps the pre-delivery "no auto-anything" behavior: keep
    -- the branch, push to `origin`, never auto-merge. _apply_migrations adds the
    -- same columns to pre-existing rows.
    git_delivery_remote TEXT NOT NULL DEFAULT 'origin',
    git_delivery_retention TEXT NOT NULL DEFAULT 'keep' CHECK (
        git_delivery_retention IN
        ('keep', 'remove_worktree_keep_branch', 'remove_all')
    ),
    git_delivery_author_name TEXT NOT NULL DEFAULT 'Owlery Task',
    git_delivery_author_email TEXT NOT NULL DEFAULT 'owlery-tasks@localhost',
    git_delivery_default_draft_pr INTEGER NOT NULL DEFAULT 1
        CHECK (git_delivery_default_draft_pr IN (0, 1)),
    git_delivery_default_merge TEXT NOT NULL DEFAULT 'none' CHECK (
        git_delivery_default_merge IN ('none', 'fast_forward_only')
    ),
    -- Local deploy (docs/plans/local-deploy.md §9). A board whose runs may
    -- deploy to the production instance is an explicit decision; default 0
    -- (off) backfills every existing board, so no board silently gains the
    -- power to restart production. _apply_migrations adds it to old rows.
    allow_local_deploy INTEGER NOT NULL DEFAULT 0
        CHECK (allow_local_deploy IN (0, 1)),
    -- Release-line deployment defaults to the protected integration branch.
    -- Task branches are delivery artifacts, never a production source.
    deploy_release_ref TEXT NOT NULL DEFAULT 'main',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS task_boards_live_name
  ON task_boards(name) WHERE archived = 0;

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    board_id TEXT NOT NULL REFERENCES task_boards(id) ON DELETE CASCADE,
    parent_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL
        CHECK (status IN ('triage', 'todo', 'ready', 'running', 'blocked', 'done', 'cancelled')),
    assignee_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    origin_session_id TEXT,
    idempotency_key TEXT,
    scheduled_at TEXT,
    workspace_mode TEXT
        CHECK (workspace_mode IS NULL OR workspace_mode IN ('shared', 'copy', 'git_worktree')),
    working_dir_override TEXT,
    -- Per-task model override passed to the worker session at dispatch
    -- (budget-model-routing.md §4.2). NULL = inherit the assignee agent's
    -- model / backend default.
    model TEXT,
    current_run_id TEXT,
    blocked_kind TEXT CHECK (
        blocked_kind IS NULL OR blocked_kind IN
        ('input', 'capability', 'failure', 'protocol', 'cancelled', 'interrupted')
    ),
    blocked_reason TEXT,
    result_summary TEXT,
    -- Review/acceptance gate (task-board-gaps.md §3.1): the worker's verdict
    -- on a done task. NULL = no verdict recorded (legacy tasks, or task
    -- kinds that don't gate anything) and is treated as passing; 'fail' on a
    -- done task means it satisfies no downstream dependency, ever.
    verdict TEXT CHECK (verdict IS NULL OR verdict IN ('pass', 'fail')),
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    created_by_kind TEXT NOT NULL
        CHECK (created_by_kind IN ('user', 'agent', 'schedule', 'api')),
    created_by_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    archived_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS tasks_board_idempotency
  ON tasks(board_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS tasks_dispatch
  ON tasks(board_id, archived, status, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS tasks_assignee
  ON tasks(assignee_agent_id, archived, status);
CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(parent_task_id);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    created_by_kind TEXT NOT NULL,
    created_by_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on_task_id),
    CHECK (task_id != depends_on_task_id)
);

CREATE TABLE IF NOT EXISTS task_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    session_id TEXT,
    state TEXT NOT NULL CHECK (
        state IN ('running', 'completed', 'blocked', 'failed', 'cancelled', 'interrupted')
    ),
    summary TEXT,
    metadata TEXT,
    error TEXT,
    workspace_mode TEXT NOT NULL
        CHECK (workspace_mode IN ('shared', 'copy', 'git_worktree')),
    workspace_path TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    started_at TEXT,
    last_heartbeat_at TEXT,
    lease_expires_at TEXT,
    finished_at TEXT,
    UNIQUE (task_id, attempt_no)
);
CREATE UNIQUE INDEX IF NOT EXISTS task_runs_one_running
  ON task_runs(task_id) WHERE state = 'running';
CREATE INDEX IF NOT EXISTS task_runs_task ON task_runs(task_id, attempt_no);
CREATE INDEX IF NOT EXISTS task_runs_active_workspace
  ON task_runs(workspace_path) WHERE state = 'running';

CREATE TABLE IF NOT EXISTS task_comments (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES task_runs(id) ON DELETE SET NULL,
    author_kind TEXT NOT NULL CHECK (author_kind IN ('user', 'agent', 'system')),
    author_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_comments_task
  ON task_comments(task_id, created_at);

CREATE TABLE IF NOT EXISTS task_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id TEXT NOT NULL REFERENCES task_boards(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES task_runs(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    actor_kind TEXT NOT NULL,
    actor_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_events_task ON task_events(task_id, seq);
CREATE INDEX IF NOT EXISTS task_events_board ON task_events(board_id, seq);

CREATE TABLE IF NOT EXISTS task_artifacts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    source_path TEXT NOT NULL,
    mime_type TEXT,
    size INTEGER NOT NULL CHECK (size >= 0),
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    UNIQUE (run_id, name)
);

-- Experience consolidation (experience-consolidation.md §3.2/§3.3): filed at
-- most once per run, only for a "non-clean-pass" `complete` (attempts > 1,
-- a prior blocked/failed/interrupted run on the same task, or verdict=fail).
-- The worker triages its own retrospective into memory (self, off-DB) /
-- CLAUDE.md (nominated, delivered via the normal PR path) / a skill candidate
-- (this DB, see skill_candidates below) — at least one of the four fields
-- must be set, `nothing_note` covering the "nothing new" case explicitly
-- rather than silently skipping. `complete` refuses to finish a non-clean
-- run until this row exists (task_board/manager.py `complete_worker`).
--
-- `memory_pointer` and `claude_md_note` are gated by a real artifact, not
-- accepted as free text (Snape review point 3 — a DB string alone is
-- checkbox theater): `memory_pointer` is a relative path the manager
-- verifies actually exists (non-empty) under the run's agent's memory dir
-- (server/agent_memory.py `resolve_memory_pointer`) before this row is ever
-- written — the worker wrote that file itself via its normal tools, this
-- column stores a POINTER to it, not the note's substantive content.
-- `claude_md_note` is the human-readable nomination text, but is only
-- accepted alongside a real, already-committed CLAUDE.md diff on the run's
-- own git_worktree branch (verified against `prepared.base_head`) — the
-- text nominates, the commit is the auditable candidate artifact.
CREATE TABLE IF NOT EXISTS task_retrospectives (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    memory_pointer TEXT,
    claude_md_note TEXT,
    skill_candidate_ids TEXT,   -- JSON list of skill_candidates.id
    nothing_note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (run_id)
);
CREATE INDEX IF NOT EXISTS task_retrospectives_task ON task_retrospectives(task_id);

-- Skill candidate review queue (experience-consolidation.md §3.4/§5): the
-- hermes-style pending -> diff -> approve/reject shape. A row starts
-- `pending` (a proposed SKILL.md an agent wrote during retrospective, or ad
-- hoc); a human approves or rejects it (skill_registry.py — never the
-- proposing agent, per §4 "no auto-generated skill takes effect"). There is
-- no separate "landed skills" table: an `approved` row IS the landed skill,
-- and use_count/last_used accrue on it directly, mirroring hermes' single
-- skill entity with a `.usage.json` sidecar.
CREATE TABLE IF NOT EXISTS skill_candidates (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    body_markdown TEXT NOT NULL,        -- full SKILL.md content (frontmatter + body)
    repository TEXT NOT NULL,           -- absolute path of the target repo (board.working_dir snapshot)
    rationale TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    proposed_by_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    proposed_by_session_id TEXT,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    run_id TEXT REFERENCES task_runs(id) ON DELETE SET NULL,
    reviewed_at TEXT,
    review_note TEXT,
    landed_path TEXT,                   -- e.g. ".claude/skills/<slug>/SKILL.md"
    landed_branch TEXT,                 -- local branch the landing commit lives on (never auto-pushed)
    landed_commit TEXT,
    use_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    -- experience-consolidation-v2.md §3③: 'agent-global' (no repo fingerprint,
    -- every session for this agent loads it) or 'agent+repo' (current
    -- behavior, scoped to `repository`). Chosen at propose time, reviewer may
    -- override it on approve.
    scope TEXT NOT NULL DEFAULT 'agent+repo' CHECK (scope IN ('agent-global', 'agent+repo')),
    -- §3③ bundle: JSON {relative_path: content} for files alongside SKILL.md
    -- (scripts/templates/examples/tests). NULL/absent = no bundle files.
    bundle_files TEXT,
    -- §3② evidence chain: static lint computed at propose time (frontmatter
    -- validity, slug conflicts, bundle file references) — informational,
    -- never blocks; shown on the review page.
    lint_results TEXT,
    -- §3④: JSON list of backend kinds ('claude' | 'codex') this candidate was
    -- actually materialized for on approve — what really got double-landed,
    -- not a proposer-declared target.
    materialized_backends TEXT,
    -- Set when a LATER same-(agent, slug, repository) approval relocated
    -- this row's materialized copy to a different scope/location (Snape
    -- review: `status='approved'` alone is a historical fact — it stays
    -- true forever — but "is this the version actually loadable right now"
    -- can change after the fact when a same-repo replacement supersedes it;
    -- without this, get_latest_approved_skill_by_slug would keep returning
    -- a row whose files were already removed from disk). NULL = still the
    -- active landed version.
    superseded_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS skill_candidates_status ON skill_candidates(status, created_at);
CREATE INDEX IF NOT EXISTS skill_candidates_slug ON skill_candidates(slug, status);

-- Skill invocation log (experience-consolidation-v2.md §3⑤): one row per
-- real skill use, naming the consuming run/session — the natural extension
-- of the use_count/last_used aggregate already on skill_candidates (v1 T-B's
-- (agent, repository) scoping fix lives on the same lookup this feeds). To
-- the foreign key and a display list, no further: no aggregation, no rate,
-- no threshold (§4 "不做" — no effectiveness-metrics layer).
CREATE TABLE IF NOT EXISTS skill_invocations (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES skill_candidates(id) ON DELETE CASCADE,
    agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    repository TEXT,
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    run_id TEXT REFERENCES task_runs(id) ON DELETE SET NULL,
    backend TEXT,                       -- 'claude-code' | 'codex', best-effort
    used_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS skill_invocations_candidate
  ON skill_invocations(candidate_id, used_at DESC);

-- Git delivery closure for git_worktree runs (task-git-delivery.md §11). One
-- durable delivery per completed worktree run records the fate of its branch;
-- an append-only op log records each at-most-once local/external operation.
-- New-table-only — CREATE IF NOT EXISTS is a no-op migration.
CREATE TABLE IF NOT EXISTS task_deliveries (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'preparing', 'ready', 'delivering',
        'delivered', 'conflicted', 'blocked', 'failed'
    )),
    repository TEXT NOT NULL,              -- absolute source repo path (snapshot)
    base_ref TEXT,                         -- base branch captured at prepare time; '' = detached; NULL = legacy run
    base_head TEXT,
    attempt_branch TEXT NOT NULL,
    attempt_head TEXT,
    dirty INTEGER NOT NULL DEFAULT 0 CHECK (dirty IN (0, 1)),
    commits_ahead INTEGER,
    diffstat TEXT,                         -- JSON {files,insertions,deletions}
    remote_name TEXT,
    remote_url TEXT,                       -- secret-stripped
    pushed_ref TEXT,
    pr_number INTEGER,
    pr_url TEXT,
    pr_state TEXT,
    merge_strategy TEXT,
    retention TEXT,
    reason_kind TEXT,                      -- blocked|conflicted|failed reason (task-git-delivery.md §11.1)
    reason_detail TEXT,
    deployed_sha TEXT,                     -- sha a successful deploy_switch made live (local-deploy.md §8)
    deployed_slot TEXT,                    -- slot ('a'/'b') that sha runs in
    -- Derived collapse pointer (task-board-overhaul.md §3.1): set when this
    -- delivery's head is a strict git ancestor of another delivery's head on
    -- the same board/repository. Recomputed from git facts, never itself part
    -- of the `status` machine above.
    superseded_by_delivery_id TEXT REFERENCES task_deliveries(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id)
);
CREATE INDEX IF NOT EXISTS task_deliveries_task ON task_deliveries(task_id);
CREATE INDEX IF NOT EXISTS task_deliveries_active
  ON task_deliveries(status) WHERE status IN ('preparing', 'delivering');

CREATE TABLE IF NOT EXISTS task_delivery_ops (
    id TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL REFERENCES task_deliveries(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN (
        'commit', 'push', 'pull_request', 'merge', 'branch_delete', 'worktree_remove',
        'deploy_stage', 'deploy_switch'
    )),
    source_key TEXT NOT NULL,             -- stable at-most-once key
    external INTEGER NOT NULL CHECK (external IN (0, 1)),
    state TEXT NOT NULL CHECK (state IN (
        'planned', 'running', 'succeeded', 'failed', 'interrupted'
    )),
    request TEXT NOT NULL DEFAULT '{}',   -- JSON of requested parameters
    result TEXT,                          -- JSON of git/platform response, secret-stripped
    error TEXT,
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('user', 'agent')),
    actor_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (source_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS task_delivery_ops_one_running
  ON task_delivery_ops(delivery_id) WHERE state = 'running';
CREATE INDEX IF NOT EXISTS task_delivery_ops_delivery
  ON task_delivery_ops(delivery_id, created_at);

-- Local deploy-and-restart (docs/plans/local-deploy.md §6). What version the
-- local production instance is running, and how it got there. Exactly one row
-- is `live` at any time (the partial unique index), and at most one deploy is
-- `staging`/`switching` across all boards — that second index is the global
-- deploy lock (§4). New-table-only — CREATE IF NOT EXISTS is a no-op migration.
CREATE TABLE IF NOT EXISTS deployments (
    id TEXT PRIMARY KEY,
    delivery_id TEXT REFERENCES task_deliveries(id) ON DELETE SET NULL,
    task_id TEXT,                        -- denormalized for display; survives delivery GC
    op_id TEXT,                          -- the deploy_switch op, once one exists
    slot TEXT NOT NULL,                  -- 'a' | 'b'
    sha TEXT NOT NULL,
    source_repo TEXT NOT NULL,
    release_id TEXT,
    -- Lifecycle: staging → staged → switching → live, with rolled_back /
    -- superseded / failed as terminals. `staging` and `switching` are the
    -- in-flight states the deployments_one_active lock (below) covers; `staged`
    -- is the settled post-stage state and is deliberately NOT locked, so future
    -- op code must insert `staging` (not `staged`) while a stage runs or it will
    -- bypass the global lock.
    state TEXT NOT NULL,                 -- staging|staged|switching|live|rolled_back|superseded|failed
    journal TEXT,                        -- JSON: final journal excerpt for this deploy
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS deployments_one_live
  ON deployments(state) WHERE state = 'live';
-- The global deploy lock (§4): at most ONE row is staging-OR-switching across
-- all boards. The predicate spans two state values, so it is indexed on the
-- constant (1) — the literal `ON deployments(state)` of §6 would only bound
-- each value on its own, letting a `staging` and a `switching` coexist (two
-- pipelines), which §4's "one instance, one pipeline at a time" forbids.
CREATE UNIQUE INDEX IF NOT EXISTS deployments_one_active
  ON deployments((1)) WHERE state IN ('staging', 'switching');

-- Board-level release intent. A human-readable release number is an audit aid;
-- `sha` remains the immutable identity used for staging and switching.
CREATE TABLE IF NOT EXISTS release_deployments (
    id TEXT PRIMARY KEY,
    board_id TEXT NOT NULL REFERENCES task_boards(id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    sha TEXT NOT NULL,
    source_repo TEXT NOT NULL,
    deployment_id TEXT REFERENCES deployments(id) ON DELETE SET NULL,
    state TEXT NOT NULL CHECK (state IN (
        'planned', 'staging', 'staged', 'switching', 'live',
        'superseded', 'rolled_back', 'failed'
    )),
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('user', 'agent', 'system')),
    actor_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (board_id, version)
);
CREATE INDEX IF NOT EXISTS release_deployments_board_created
  ON release_deployments(board_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS release_deployments_one_active
  ON release_deployments((1)) WHERE state IN ('staging', 'switching');

-- Release operations deliberately do not reuse task_delivery_ops: a release
-- can include many tasks and must remain auditable even when task retention
-- removes a worktree or delivery record.
CREATE TABLE IF NOT EXISTS release_deployment_ops (
    id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL REFERENCES release_deployments(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('stage', 'switch', 'rollback')),
    state TEXT NOT NULL CHECK (state IN (
        'planned', 'running', 'succeeded', 'failed', 'interrupted'
    )),
    request TEXT NOT NULL DEFAULT '{}',
    result TEXT,
    error TEXT,
    journal_ref TEXT,
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('user', 'agent', 'system')),
    actor_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS release_deployment_ops_one_running
  ON release_deployment_ops(release_id) WHERE state = 'running';
CREATE INDEX IF NOT EXISTS release_deployment_ops_release
  ON release_deployment_ops(release_id, created_at);

-- Parked turns awaiting a usage-limit reset (limit-auto-resume.md §4). A turn
-- that failed on the USER'S OWN limit is persisted here, not slept on: the
-- wait is multi-hour, so it must survive a restart — on boot these rows
-- rebuild their APScheduler wake-up jobs. At most one park per session (the
-- session is single-turn), so session_id is the PK and a re-park UPDATEs.
-- New-table-only — CREATE IF NOT EXISTS is a no-op migration.
CREATE TABLE IF NOT EXISTS parked_turns (
    session_id TEXT PRIMARY KEY,
    -- How to resume, decided at park time and reused verbatim from the
    -- transient-retry two-mode recovery (harness-transient-retry.md §4):
    --   'prompt'   → no output had streamed; re-run `payload` (the original
    --                prompt) from resume_at, discarding the failed attempt's id
    --   'continue' → output streamed and a resume id was captured; resume the
    --                conversation so tools don't re-run and text can't dupe
    resume_mode TEXT NOT NULL,             -- 'prompt' | 'continue'
    payload TEXT NOT NULL,                 -- original prompt ('prompt') | 'continue'
    resume_at_turn_start TEXT,             -- backend resume id as of turn start
    limit_kind TEXT,                       -- backend's window name: five_hour | usage_limit | …
    reset_at TEXT,                         -- ISO-8601 UTC; NULL ⇒ probe mode
    wake_at TEXT NOT NULL,                 -- ISO-8601 UTC; reset_at + stagger, or the probe tick
    attempts INTEGER NOT NULL DEFAULT 0,   -- consecutive parks with no progress (cap 3)
    probes INTEGER NOT NULL DEFAULT 0,     -- consecutive probe ticks (cap 12)
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_parked_turns_wake
  ON parked_turns(wake_at);
"""


class Database:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._dirty: bool = False
        self._closed: bool = False

    async def initialize(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(_SCHEMA)
        await self._apply_migrations()
        await self._conn.commit()

    async def _apply_migrations(self) -> None:
        """Idempotent additive migrations for tables that pre-existed."""
        # sessions.credential_id was added when per-backend auth landed.
        try:
            await self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN credential_id TEXT"
            )
        except Exception:
            # Column already exists — SQLite has no IF NOT EXISTS for ALTER COLUMN
            pass

        # sessions.archived for /archive feature (hides old session row from
        # the default list, keeps it in DB so it could be surfaced later).
        try:
            await self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass

        # backend_credentials gained status / refresh-tracking columns (B-4/B-5).
        # Each ALTER is wrapped because SQLite has no IF NOT EXISTS for them.
        for ddl in (
            "ALTER TABLE backend_credentials ADD COLUMN "
            "status TEXT NOT NULL DEFAULT 'active'",
            "ALTER TABLE backend_credentials ADD COLUMN token_expires_at TEXT",
            "ALTER TABLE backend_credentials ADD COLUMN "
            "needs_reconnect INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE backend_credentials ADD COLUMN "
            "last_refresh_error_code TEXT",
        ):
            try:
                await self._conn.execute(ddl)
            except Exception:
                pass

        # Storage split (B-4): copy any existing legacy secrets into the
        # dedicated credential_secrets table. New writes go there directly;
        # this catch-up only runs once per pre-split row.
        try:
            await self._conn.execute(
                "INSERT OR IGNORE INTO credential_secrets "
                "(credential_id, secret_encrypted) "
                "SELECT id, secret_encrypted FROM backend_credentials"
            )
        except Exception:
            logger.exception("credential storage-split backfill failed")

        # messages.attachments was added with the file/image upload feature.
        try:
            await self._conn.execute(
                "ALTER TABLE messages ADD COLUMN attachments TEXT"
            )
        except Exception:
            pass

        # sessions.backend ('claude-code' | 'codex') — codex-backend.md §4.1.
        # DEFAULT backfills existing rows to claude-code → no behavior change.
        try:
            await self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN backend TEXT NOT NULL "
                "DEFAULT 'claude-code'"
            )
        except Exception:
            pass

        # Skill candidate bundle/scope/lint/materialization columns
        # (experience-consolidation-v2.md §3②③④). DEFAULTs backfill every
        # pre-existing candidate to 'agent+repo' scope with no bundle/lint/
        # materialization data — matches exactly what those rows already
        # behaved as before this migration. New DBs get them from _SCHEMA;
        # this catch-up covers candidates created before v2 landed.
        for ddl in (
            "ALTER TABLE skill_candidates ADD COLUMN "
            "scope TEXT NOT NULL DEFAULT 'agent+repo'",
            "ALTER TABLE skill_candidates ADD COLUMN bundle_files TEXT",
            "ALTER TABLE skill_candidates ADD COLUMN lint_results TEXT",
            "ALTER TABLE skill_candidates ADD COLUMN materialized_backends TEXT",
            "ALTER TABLE skill_candidates ADD COLUMN superseded_at TEXT",
        ):
            try:
                await self._conn.execute(ddl)
            except aiosqlite.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    logger.error("skill candidate migration failed: %s (%s)", ddl, exc)
                    raise

        # Git delivery closure (task-git-delivery.md §18). Additive board
        # settings; DEFAULTs backfill existing boards to the pre-delivery
        # behavior (keep branch, push to origin, never auto-merge), so no board
        # silently starts pushing or deleting. New DBs get them from _SCHEMA;
        # this catch-up covers boards created before the feature landed.
        for ddl in (
            "ALTER TABLE task_boards ADD COLUMN "
            "git_delivery_remote TEXT NOT NULL DEFAULT 'origin'",
            "ALTER TABLE task_boards ADD COLUMN "
            "git_delivery_retention TEXT NOT NULL DEFAULT 'keep'",
            "ALTER TABLE task_boards ADD COLUMN "
            "git_delivery_author_name TEXT NOT NULL DEFAULT 'Owlery Task'",
            "ALTER TABLE task_boards ADD COLUMN "
            "git_delivery_author_email TEXT NOT NULL DEFAULT 'owlery-tasks@localhost'",
            "ALTER TABLE task_boards ADD COLUMN "
            "git_delivery_default_draft_pr INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE task_boards ADD COLUMN "
            "git_delivery_default_merge TEXT NOT NULL DEFAULT 'none'",
            # Local deploy (docs/plans/local-deploy.md §9). DEFAULT 0 backfills
            # every existing board to "may not deploy production" — fail-closed.
            "ALTER TABLE task_boards ADD COLUMN "
            "allow_local_deploy INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE task_boards ADD COLUMN "
            "deploy_release_ref TEXT NOT NULL DEFAULT 'main'",
        ):
            try:
                await self._conn.execute(ddl)
            except aiosqlite.OperationalError as exc:
                # The only benign outcome of these catch-up ADD COLUMNs is
                # "duplicate column" — the column already exists on a newer DB.
                # Anything else (a genuinely malformed/failed migration) must
                # surface here, not be swallowed to crash later at a misleading
                # site when a NOT NULL column reads back missing.
                if "duplicate column" not in str(exc).lower():
                    logger.error("board migration failed: %s (%s)", ddl, exc)
                    raise

        # Local deploy (docs/plans/local-deploy.md §8). A successful deploy_switch
        # folds the live sha/slot into the delivery; these back-fill NULL on rows
        # created before the switch op landed. New DBs get them from _SCHEMA.
        for ddl in (
            "ALTER TABLE task_deliveries ADD COLUMN deployed_sha TEXT",
            "ALTER TABLE task_deliveries ADD COLUMN deployed_slot TEXT",
        ):
            try:
                await self._conn.execute(ddl)
            except aiosqlite.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    logger.error("delivery migration failed: %s (%s)", ddl, exc)
                    raise

        # Task Board rectification (docs/plans/task-board-overhaul.md §3.1): the
        # derived supersede-collapse pointer. New DBs get it from _SCHEMA; this
        # catch-up covers deliveries created before the feature landed. The
        # index is created here (not in _SCHEMA) since an old DB's CREATE TABLE
        # IF NOT EXISTS is a no-op — the column only exists after this ALTER.
        superseded_ddl = (
            "ALTER TABLE task_deliveries ADD COLUMN "
            "superseded_by_delivery_id TEXT "
            "REFERENCES task_deliveries(id) ON DELETE SET NULL"
        )
        try:
            await self._conn.execute(superseded_ddl)
        except aiosqlite.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                logger.error("delivery migration failed: %s (%s)", superseded_ddl, exc)
                raise
        try:
            await self._conn.execute(
                "CREATE INDEX IF NOT EXISTS task_deliveries_superseded_by "
                "ON task_deliveries(superseded_by_delivery_id)"
            )
        except Exception:
            logger.exception("task_deliveries_superseded_by index migration failed")

        await self._migrate_delivery_op_kinds()

        # Release-line deploy (§ release workflow). Existing task-scoped
        # deployments remain valid history; only new release records populate
        # this nullable association.
        try:
            await self._conn.execute(
                "ALTER TABLE deployments ADD COLUMN release_id TEXT"
            )
        except aiosqlite.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

        # sessions.model — per-session model override (budget-model-routing.md
        # §4.1). Nullable, no DEFAULT: existing rows stay NULL and keep
        # inheriting the agent/backend default via resolve_model().
        try:
            await self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN model TEXT"
            )
        except Exception:
            pass

        # tasks.model — per-task model override for the worker session
        # (budget-model-routing.md §4.2). Nullable; the task board schema is a
        # CREATE-IF-NOT-EXISTS in _SCHEMA (so new DBs already have it), this
        # ALTER backfills pre-existing rows.
        try:
            await self._conn.execute(
                "ALTER TABLE tasks ADD COLUMN model TEXT"
            )
        except Exception:
            pass
        await self._migrate_agents()
        await self._migrate_schedule_recurrence()
        await self._migrate_schedule_run_at()

        # agents.backend — default harness for an agent's new sessions. DEFAULT
        # backfills existing agents to claude-code → no behavior change.
        try:
            await self._conn.execute(
                "ALTER TABLE agents ADD COLUMN backend TEXT NOT NULL "
                "DEFAULT 'claude-code'"
            )
        except Exception:
            pass

        # bridge_mappings.verbose — per-chat output verbosity (quiet by
        # default: only octo replies/errors/approvals). DEFAULT 0 backfills
        # existing chats to quiet. Runs after `_migrate_agents`, which may
        # rebuild bridge_mappings, so the column survives that rebuild.
        try:
            await self._conn.execute(
                "ALTER TABLE bridge_mappings ADD COLUMN verbose INTEGER "
                "NOT NULL DEFAULT 0"
            )
        except Exception:
            pass

        # Feishu replaced Telegram (feishu-bridge.md §5). Purge leftover
        # telegram chat bindings: the platform is gone, so these rows can never
        # match a live bridge again — dead data that would only confuse
        # broadcast routing. Explicit commit so the purge persists regardless
        # of ambient transaction state; idempotent (a DB with none is a no-op).
        try:
            await self._conn.execute(
                "DELETE FROM bridge_mappings WHERE platform = 'telegram'"
            )
            await self._conn.commit()
        except Exception:
            pass

        # Agent-to-agent collaboration (agent-collaboration.md §4.1). A
        # delegation child session points at the parent session via
        # parent_session_id (SET NULL on parent delete — orphaning beats
        # mass-delete) and carries the original delegation prompt in
        # delegation_request for the UI header. Both columns NULL on
        # non-delegation sessions. origin gains a 'delegation' value
        # (TEXT column — no DDL change, only callers).
        for ddl in (
            "ALTER TABLE sessions ADD COLUMN parent_session_id TEXT "
            "REFERENCES sessions(id) ON DELETE SET NULL",
            "ALTER TABLE sessions ADD COLUMN delegation_request TEXT",
        ):
            try:
                await self._conn.execute(ddl)
            except Exception:
                pass

        # Session tree-rewind / fork (session-rewind.md §4). Six nullable
        # columns on sessions + two on messages, all additive. forked_from has
        # no FK action on purpose (dangling reference survives parent delete).
        for ddl in (
            "ALTER TABLE sessions ADD COLUMN forked_from_session_id TEXT",
            "ALTER TABLE sessions ADD COLUMN fork_after_seq INTEGER",
            "ALTER TABLE sessions ADD COLUMN fork_needs_replay INTEGER "
            "NOT NULL DEFAULT 0",
            "ALTER TABLE sessions ADD COLUMN fork_metadata TEXT",
            "ALTER TABLE sessions ADD COLUMN fork_revert_record TEXT",
            "ALTER TABLE sessions ADD COLUMN fork_status TEXT",
            "ALTER TABLE messages ADD COLUMN git_head TEXT",
            "ALTER TABLE messages ADD COLUMN git_status_clean INTEGER",
            "ALTER TABLE messages ADD COLUMN injection_id TEXT",
            # attempt-replay.md §3.1 point 1: messages.created_at.
            "ALTER TABLE messages ADD COLUMN created_at TEXT",
            # attempt-replay.md §3.1 point 4: turn_usage's anchor onto the
            # messages seq of the turn's `result` row.
            "ALTER TABLE turn_usage ADD COLUMN message_seq INTEGER",
        ):
            try:
                await self._conn.execute(ddl)
            except Exception:
                pass
        try:
            await self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_forked_from "
                "ON sessions(forked_from_session_id)"
            )
        except Exception:
            pass
        try:
            await self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_injection "
                "ON messages(injection_id) WHERE injection_id IS NOT NULL"
            )
        except Exception:
            logger.exception("messages.injection_id index migration failed")
        # Keep the delivery receipt inside the SAME SQLite statement as the
        # transcript insert. Database uses one shared aiosqlite connection, so
        # two Python execute() calls are not an isolation boundary: another
        # coroutine could commit between them. A trigger is part of the INSERT
        # statement itself and closes that window.
        await self._conn.executescript(
            """
            DROP TRIGGER IF EXISTS messages_validate_injection;
            DROP TRIGGER IF EXISTS messages_ack_injection;

            CREATE TRIGGER messages_validate_injection
            BEFORE INSERT ON messages
            WHEN NEW.injection_id IS NOT NULL
            BEGIN
              SELECT CASE WHEN NEW.role != 'user'
                OR NEW.type != 'text'
                OR NOT EXISTS (
                SELECT 1 FROM session_injections
                WHERE id = NEW.injection_id
                  AND session_id = NEW.session_id
                  AND status = 'pending'
                  AND prompt = json_extract(NEW.content, '$')
              ) THEN RAISE(ABORT, 'invalid or already delivered session injection') END;
            END;

            CREATE TRIGGER messages_ack_injection
            AFTER INSERT ON messages
            WHEN NEW.injection_id IS NOT NULL
            BEGIN
              UPDATE session_injections
              SET status = 'delivered',
                  delivered_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                  error = NULL
              WHERE id = NEW.injection_id
                AND session_id = NEW.session_id
                AND status = 'pending';
            END;
            """
        )
        # Delivery-outbox cutover for bg tasks. Existing terminal rows are
        # historical and must not be replayed; fresh-schema rows default to 1
        # and create_bg_task writes 1 explicitly.
        if not await self._has_column("bg_tasks", "delivery_required"):
            await self._conn.execute(
                "ALTER TABLE bg_tasks ADD COLUMN delivery_required "
                "INTEGER NOT NULL DEFAULT 0"
            )
            # Historical terminal output is assumed already delivered and must
            # not be replayed. Work that was still in flight at cutover is
            # different: this boot will mark it interrupted, and the caller
            # must receive that terminal fact.
            await self._conn.execute(
                "UPDATE bg_tasks SET delivery_required = 1 "
                "WHERE status IN ('running', 'pending')"
            )

        # One-time catch-up: enrol every pre-existing agent in the built-in MCP
        # servers that shipped after it was created (`ask_agent` —
        # agent-collaboration.md §5.1; `research` — native-deep-research.md §7).
        # The CREATE TABLE default already covers brand-new rows; this rescues
        # older rows that stored the narrower list.
        #
        # Gated by PRAGMA user_version so it runs ONCE, not on every boot: the
        # agent-settings UI now lets a user deselect any built-in server, and a
        # per-boot re-add would silently overturn that choice on the next
        # restart. The membership check is still additive (re-running would add
        # nothing new), but the gate is what makes a deliberate *removal* stick.
        # A future new built-in gets its own gated step at the next version.
        cursor = await self._conn.execute("PRAGMA user_version")
        (user_version,) = await cursor.fetchone()
        if user_version < 1:
            await self._backfill_builtin_mcp_servers(("ask_agent", "research"))
            await self._conn.execute("PRAGMA user_version = 1")
        if user_version < 2:
            await self._backfill_builtin_mcp_servers(("tasks",))
            await self._conn.execute("PRAGMA user_version = 2")
        if user_version < 3:
            # `skills` — skill candidate proposal, experience-consolidation.md
            # §3.3/§3.4.
            await self._backfill_builtin_mcp_servers(("skills",))
            await self._conn.execute("PRAGMA user_version = 3")

        await self._migrate_task_verdict_and_cancelled_status()
        await self._migrate_skill_invocations_session_fk()

    async def _migrate_skill_invocations_session_fk(self) -> None:
        """``skill_invocations.session_id`` was a bare TEXT column with no FK
        to ``sessions`` (Snape's T-B review, experience-consolidation-v2.md
        §3⑤) — a deleted session left its invocation rows pointing at a dead
        id forever, instead of the ``ON DELETE SET NULL`` behavior every
        other "which session did this" column in this schema gets. SQLite
        cannot ALTER a column to add a REFERENCES clause, so a DB created
        before this migration needs the table rebuilt; new DBs get the FK
        straight from ``_SCHEMA``.

        Guarded on the live table's own DDL text so it runs exactly once,
        and a no-op when the table is absent. Nothing references
        ``skill_invocations`` by foreign key, so the drop-and-rename is safe
        with foreign_keys ON, as with ``_migrate_delivery_op_kinds``. Every
        other column's FK (candidate_id/agent_id/task_id/run_id) already
        existed pre-migration and is copied unchanged; only ``session_id``
        needs reconciling — a value naming a session that no longer exists
        would violate the new FK on INSERT, so it is nulled during the copy,
        which is exactly the ``ON DELETE SET NULL`` outcome that session's
        own deletion should already have produced."""
        cur = await self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='skill_invocations'"
        )
        row = await cur.fetchone()
        if row is None or "REFERENCES sessions" in (row[0] or ""):
            return
        await self._conn.execute("DROP TABLE IF EXISTS skill_invocations__new")
        await self._conn.executescript(
            """
            CREATE TABLE skill_invocations__new (
                id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL REFERENCES skill_candidates(id) ON DELETE CASCADE,
                agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
                repository TEXT,
                session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
                task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                run_id TEXT REFERENCES task_runs(id) ON DELETE SET NULL,
                backend TEXT,
                used_at TEXT NOT NULL
            );
            INSERT INTO skill_invocations__new
                (id, candidate_id, agent_id, repository, session_id, task_id,
                 run_id, backend, used_at)
                SELECT id, candidate_id, agent_id, repository,
                       CASE WHEN session_id IS NOT NULL
                                AND session_id NOT IN (SELECT id FROM sessions)
                            THEN NULL ELSE session_id END,
                       task_id, run_id, backend, used_at
                FROM skill_invocations;
            DROP TABLE skill_invocations;
            ALTER TABLE skill_invocations__new RENAME TO skill_invocations;
            CREATE INDEX IF NOT EXISTS skill_invocations_candidate
              ON skill_invocations(candidate_id, used_at DESC);
            """
        )

    async def _backfill_builtin_mcp_servers(self, names: tuple[str, ...]) -> None:
        cursor = await self._conn.execute("SELECT id, mcp_servers FROM agents")
        rows = list(await cursor.fetchall())
        for agent_id, raw in rows:
            try:
                current = json.loads(raw) if raw else []
            except json.JSONDecodeError:
                # A corrupted blob is left alone — manual rescue from
                # SQLite is safer than guessing what the user meant.
                continue
            if not isinstance(current, list):
                continue
            added = False
            for name in names:
                if name not in current:
                    current.append(name)
                    added = True
            if added:
                await self._conn.execute(
                    "UPDATE agents SET mcp_servers = ? WHERE id = ?",
                    (json.dumps(current), agent_id),
                )

    async def _migrate_schedule_recurrence(self) -> None:
        """Schedules gained cron/timezone/recurrence_label and `interval_seconds`
        became nullable (natural-language + time-of-day scheduling), then later
        an `origin_session_id` (a `/schedule` created in a chat remembers its
        session so fires append into that conversation). Rebuild the table once
        for the recurrence shape — guarded on the `cron` column being absent —
        then additively ensure the origin column. Fresh DBs (already the full
        shape from _SCHEMA) and re-boots no-op. Runs after `_migrate_agents`, so
        the table already has `agent_id` and no `session_id`. Existing rows are
        interval schedules: cron/timezone/recurrence_label stay NULL and the UI
        formats interval_seconds."""
        if not await self._has_column(
            "schedules", "cron"
        ) and await self._has_column("schedules", "interval_seconds"):
            await self._conn.executescript(
                """
                CREATE TABLE schedules__rec (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    interval_seconds INTEGER,
                    cron TEXT,
                    timezone TEXT,
                    recurrence_label TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_run_at TEXT
                );
                INSERT INTO schedules__rec
                    (id, agent_id, name, prompt, interval_seconds, enabled,
                     created_at, last_run_at)
                    SELECT id, agent_id, name, prompt, interval_seconds, enabled,
                           created_at, last_run_at FROM schedules;
                DROP TABLE schedules;
                ALTER TABLE schedules__rec RENAME TO schedules;
                """
            )
        # origin_session_id is additive on top of the recurrence shape. Guarded
        # so re-running (and fresh DBs that already have it from _SCHEMA) no-op.
        if not await self._has_column("schedules", "origin_session_id"):
            await self._conn.execute(
                "ALTER TABLE schedules ADD COLUMN origin_session_id TEXT"
            )

    async def _migrate_schedule_run_at(self) -> None:
        """Add run_at column to schedules (one-time schedule support). Fresh DBs
        already have it from _SCHEMA; re-runs are no-ops."""
        if not await self._has_column("schedules", "run_at"):
            await self._conn.execute(
                "ALTER TABLE schedules ADD COLUMN run_at TEXT"
            )
            await self._conn.commit()

    async def _column_info(self, table: str) -> list[tuple[Any, ...]]:
        cursor = await self._conn.execute(f"PRAGMA table_info({table})")
        return list(await cursor.fetchall())

    async def _has_column(self, table: str, column: str) -> bool:
        return any(row[1] == column for row in await self._column_info(table))

    async def _column_is_not_null(self, table: str, column: str) -> bool:
        # PRAGMA table_info row: (cid, name, type, notnull, dflt_value, pk)
        for row in await self._column_info(table):
            if row[1] == column:
                return bool(row[3])
        return False

    async def _migrate_delivery_op_kinds(self) -> None:
        """Widen ``task_delivery_ops.kind``'s CHECK to admit ``deploy_stage`` and
        ``deploy_switch`` (docs/plans/local-deploy.md §4). SQLite cannot ALTER a
        CHECK constraint,
        so a DB created before local deploy needs the table rebuilt; new DBs get
        the widened CHECK straight from ``_SCHEMA``.

        Guarded on the live table's own DDL text so it runs exactly once, and a
        no-op when the table is absent. Nothing references ``task_delivery_ops``
        by foreign key, so the drop-and-rename is safe with foreign_keys ON, as
        with the other rebuilds in this module. Every existing row already
        satisfies the widened CHECK (it only ADDS an allowed value), so the copy
        never loses a row."""
        cur = await self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_delivery_ops'"
        )
        row = await cur.fetchone()
        # Guard on the NEWEST admitted kind so a DB widened for `deploy_stage`
        # (but not yet `deploy_switch`) is still rebuilt exactly once more.
        if row is None or "deploy_switch" in (row[0] or ""):
            return
        # Clear any scratch table left by a prior rebuild that was interrupted
        # between CREATE and RENAME — otherwise the CREATE below would fail and
        # (because aiosqlite's executescript deadlocks on a mid-script error)
        # hang the boot. Every live op row satisfies the widened table by
        # construction: the CHECK only ADDS a kind, and NOT NULL / the CASCADE
        # and SET-NULL FKs are unchanged, so the copy cannot lose or reject a row
        # (an FK orphan could never have been inserted under the identical old
        # FKs), and thus cannot itself error mid-script.
        await self._conn.executescript(
            """
            DROP TABLE IF EXISTS task_delivery_ops__new;
            CREATE TABLE task_delivery_ops__new (
                id TEXT PRIMARY KEY,
                delivery_id TEXT NOT NULL REFERENCES task_deliveries(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK (kind IN (
                    'commit', 'push', 'pull_request', 'merge', 'branch_delete',
                    'worktree_remove', 'deploy_stage', 'deploy_switch'
                )),
                source_key TEXT NOT NULL,
                external INTEGER NOT NULL CHECK (external IN (0, 1)),
                state TEXT NOT NULL CHECK (state IN (
                    'planned', 'running', 'succeeded', 'failed', 'interrupted'
                )),
                request TEXT NOT NULL DEFAULT '{}',
                result TEXT,
                error TEXT,
                actor_kind TEXT NOT NULL CHECK (actor_kind IN ('user', 'agent')),
                actor_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (source_key)
            );
            INSERT INTO task_delivery_ops__new
                (id, delivery_id, kind, source_key, external, state, request,
                 result, error, actor_kind, actor_agent_id, started_at,
                 finished_at, created_at)
                SELECT id, delivery_id, kind, source_key, external, state, request,
                       result, error, actor_kind, actor_agent_id, started_at,
                       finished_at, created_at FROM task_delivery_ops;
            DROP TABLE task_delivery_ops;
            ALTER TABLE task_delivery_ops__new RENAME TO task_delivery_ops;
            CREATE UNIQUE INDEX IF NOT EXISTS task_delivery_ops_one_running
              ON task_delivery_ops(delivery_id) WHERE state = 'running';
            CREATE INDEX IF NOT EXISTS task_delivery_ops_delivery
              ON task_delivery_ops(delivery_id, created_at);
            """
        )

    async def _migrate_task_verdict_and_cancelled_status(self) -> None:
        """Task Board gaps rectification (docs/plans/task-board-gaps.md §3.1,
        §3.4): add ``tasks.verdict`` and widen ``tasks.status``'s CHECK to
        admit ``cancelled`` as a first-class terminal state. SQLite cannot
        ALTER a CHECK constraint, so a DB predating this migration needs the
        table rebuilt; new DBs get both straight from ``_SCHEMA``.

        Guarded on the new ``verdict`` column's presence so it runs exactly
        once. The copy also folds every legacy
        ``status='blocked' AND blocked_kind='cancelled'`` row (the old
        ``cancel_task()`` terminal shape) into the new
        ``status='cancelled', blocked_kind=NULL`` shape and backfills
        ``completed_at`` for it — the one-time cancelled migration required
        by §3.4. Every other row is copied byte-identical; the widened CHECK
        only ADDS an allowed value, so the copy cannot lose or reject a row.

        ``tasks`` is a parent of several ON DELETE CASCADE children
        (task_dependencies, task_runs, task_comments, task_events,
        task_artifacts, task_deliveries) and is self-referencing
        (parent_task_id), unlike ``task_delivery_ops`` above which nothing
        references. SQLite refuses to DROP a table that is the target of a
        live foreign key while enforcement is on, so — unlike the rebuild
        above — this one brackets the swap with foreign_keys OFF/ON. Both
        toggles live INSIDE the executescript call (not as separate
        `execute()`s around it): SQLite silently no-ops a `PRAGMA
        foreign_keys` change while a transaction is pending, and prior
        migrations earlier in `_apply_migrations` (e.g. `_migrate_agents`'s
        INSERT/UPDATE) can leave one open. `executescript()` always issues an
        implicit COMMIT before running its script, so the OFF toggle as the
        script's first statement is guaranteed to land outside any
        transaction.
        """
        if await self._has_column("tasks", "verdict"):
            return
        await self._conn.execute("DROP TABLE IF EXISTS tasks__new")
        await self._conn.executescript(
            """
                PRAGMA foreign_keys=OFF;
                CREATE TABLE tasks__new (
                    id TEXT PRIMARY KEY,
                    board_id TEXT NOT NULL REFERENCES task_boards(id) ON DELETE CASCADE,
                    parent_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL
                        CHECK (status IN ('triage', 'todo', 'ready', 'running',
                                          'blocked', 'done', 'cancelled')),
                    assignee_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    origin_session_id TEXT,
                    idempotency_key TEXT,
                    scheduled_at TEXT,
                    workspace_mode TEXT
                        CHECK (workspace_mode IS NULL OR workspace_mode IN
                               ('shared', 'copy', 'git_worktree')),
                    working_dir_override TEXT,
                    model TEXT,
                    current_run_id TEXT,
                    blocked_kind TEXT CHECK (
                        blocked_kind IS NULL OR blocked_kind IN
                        ('input', 'capability', 'failure', 'protocol', 'cancelled', 'interrupted')
                    ),
                    blocked_reason TEXT,
                    result_summary TEXT,
                    -- Review/acceptance gate (task-board-gaps.md §3.1): the
                    -- worker's verdict on a done task. NULL = no verdict
                    -- recorded (legacy tasks, or task kinds that don't gate
                    -- anything) and is treated as passing; 'fail' on a done
                    -- task means it satisfies no downstream dependency, ever.
                    verdict TEXT CHECK (verdict IS NULL OR verdict IN ('pass', 'fail')),
                    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
                    created_by_kind TEXT NOT NULL
                        CHECK (created_by_kind IN ('user', 'agent', 'schedule', 'api')),
                    created_by_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    archived_at TEXT
                );
                INSERT INTO tasks__new
                    (id, board_id, parent_task_id, title, body, status,
                     assignee_agent_id, priority, origin_session_id, idempotency_key,
                     scheduled_at, workspace_mode, working_dir_override, model,
                     current_run_id, blocked_kind, blocked_reason, result_summary,
                     verdict, archived, created_by_kind, created_by_agent_id,
                     created_at, updated_at, completed_at, archived_at)
                    SELECT
                        id, board_id, parent_task_id, title, body,
                        CASE WHEN status = 'blocked' AND blocked_kind = 'cancelled'
                             THEN 'cancelled' ELSE status END,
                        assignee_agent_id, priority, origin_session_id, idempotency_key,
                        scheduled_at, workspace_mode, working_dir_override, model,
                        current_run_id,
                        CASE WHEN status = 'blocked' AND blocked_kind = 'cancelled'
                             THEN NULL ELSE blocked_kind END,
                        blocked_reason, result_summary, NULL, archived, created_by_kind,
                        created_by_agent_id, created_at, updated_at,
                        CASE WHEN status = 'blocked' AND blocked_kind = 'cancelled'
                             THEN updated_at ELSE completed_at END,
                        archived_at
                    FROM tasks;
                DROP TABLE tasks;
                ALTER TABLE tasks__new RENAME TO tasks;
                CREATE UNIQUE INDEX IF NOT EXISTS tasks_board_idempotency
                  ON tasks(board_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS tasks_dispatch
                  ON tasks(board_id, archived, status, priority DESC, created_at);
                CREATE INDEX IF NOT EXISTS tasks_assignee
                  ON tasks(assignee_agent_id, archived, status);
                CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(parent_task_id);
                PRAGMA foreign_keys=ON;
            """
        )

    async def _migrate_agents(self) -> None:
        """First-class Agents refactor migration (agent-refactor.md §4.5).

        Adds agent ownership to sessions / schedules / bridge_mappings,
        seeds a starter agent on a brand-new install, and backfills every
        pre-existing row to it. Idempotent: safe on every boot, a second
        run no-ops (agents table non-empty, no null agent_id rows, the
        column-shape rebuilds and column drops already applied). There is
        no longer a "protected" agent — the seeded one is an ordinary,
        deletable agent (agent-identity.md). `schedules.session_id`
        and `bridge_mappings`' NOT NULL `session_id` are removed by
        table-rebuild rather than ALTER … DROP/MODIFY, because SQLite
        forbids dropping a column that's part of a foreign key and can't
        relax NOT NULL in place.
        """
        # 1. Additive columns (wrapped — SQLite has no IF NOT EXISTS for ALTER).
        #    Adding a column with a REFERENCES clause is allowed because the
        #    default value is NULL.
        for ddl in (
            "ALTER TABLE sessions ADD COLUMN agent_id TEXT "
            "REFERENCES agents(id) ON DELETE CASCADE",
            "ALTER TABLE sessions ADD COLUMN origin TEXT NOT NULL DEFAULT 'user'",
            "ALTER TABLE schedules ADD COLUMN agent_id TEXT "
            "REFERENCES agents(id) ON DELETE CASCADE",
            "ALTER TABLE bridge_mappings ADD COLUMN agent_id TEXT "
            "REFERENCES agents(id) ON DELETE CASCADE",
        ):
            try:
                await self._conn.execute(ddl)
            except Exception:
                pass

        # 2. Seed a starter agent — only on a brand-new install, when the
        #    agents table is completely EMPTY (any row, archived or not,
        #    counts as "already populated"). An existing instance keeps
        #    whatever agents it has (including a legacy 'Octo', now an
        #    ordinary deletable agent); nothing is seeded and no agent is
        #    "protected". The seed name is 'Owl', matching the app's world.
        cursor = await self._conn.execute("SELECT id FROM agents LIMIT 1")
        if await cursor.fetchone() is None:
            default_id = uuid.uuid4().hex[:12]
            now = datetime.now(timezone.utc).isoformat()
            await self._conn.execute(
                "INSERT INTO agents "
                "(id, name, description, system_prompt, mcp_servers, "
                " created_at, updated_at) "
                "VALUES (?, 'Owl', '', '', ?, ?, ?)",
                (default_id, _DEFAULT_MCP_SERVERS_JSON, now, now),
            )
        else:
            # Non-empty table: the backfill fallbacks below (orphan sessions /
            # schedules / bridges) land on the SAME agent the runtime would
            # pick as default — the oldest LIVE one (get_default_agent), with
            # a stable created_at,id tiebreak. In practice a non-empty table
            # means the agent refactor already ran, so there are no orphans
            # and this id is never consumed; None (every agent archived) makes
            # the orphan backfills no-op rather than resurrect a dead owner.
            cur = await self._conn.execute(
                "SELECT id FROM agents WHERE archived = 0 "
                "ORDER BY created_at, id LIMIT 1"
            )
            r = await cur.fetchone()
            default_id = r[0] if r else None

        # 3. Backfill sessions → Default Agent. (origin defaults to 'user'.)
        await self._conn.execute(
            "UPDATE sessions SET agent_id = ? WHERE agent_id IS NULL",
            (default_id,),
        )

        # 4. Schedules: derive agent_id through the (about-to-be-removed)
        #    session_id, then rebuild the table without it. Guarded on the
        #    presence of session_id so it runs exactly once.
        if await self._has_column("schedules", "session_id"):
            await self._conn.execute(
                "UPDATE schedules SET agent_id = ("
                "  SELECT s.agent_id FROM sessions s WHERE s.id = schedules.session_id"
                ") WHERE agent_id IS NULL"
            )
            # Orphans whose session was deleted fall back to Default.
            await self._conn.execute(
                "UPDATE schedules SET agent_id = ? WHERE agent_id IS NULL",
                (default_id,),
            )
            await self._conn.executescript(
                """
                CREATE TABLE schedules__new (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_run_at TEXT
                );
                INSERT INTO schedules__new
                    (id, agent_id, name, prompt, interval_seconds, enabled,
                     created_at, last_run_at)
                    SELECT id, agent_id, name, prompt, interval_seconds, enabled,
                           created_at, last_run_at FROM schedules;
                DROP TABLE schedules;
                ALTER TABLE schedules__new RENAME TO schedules;
                """
            )

        # 5. Bridge mappings: derive agent_id, then rebuild to relax
        #    session_id's NOT NULL into a nullable sticky pointer. Guarded
        #    on the old NOT NULL shape so it runs exactly once.
        if await self._column_is_not_null("bridge_mappings", "session_id"):
            await self._conn.execute(
                "UPDATE bridge_mappings SET agent_id = ("
                "  SELECT s.agent_id FROM sessions s "
                "  WHERE s.id = bridge_mappings.session_id"
                ") WHERE agent_id IS NULL"
            )
            await self._conn.execute(
                "UPDATE bridge_mappings SET agent_id = ? WHERE agent_id IS NULL",
                (default_id,),
            )
            await self._conn.executescript(
                """
                CREATE TABLE bridge_mappings__new (
                    platform TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
                    session_id TEXT,
                    PRIMARY KEY (platform, chat_id),
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE SET NULL
                );
                INSERT INTO bridge_mappings__new (platform, chat_id, agent_id, session_id)
                    SELECT platform, chat_id, agent_id, session_id FROM bridge_mappings;
                DROP TABLE bridge_mappings;
                ALTER TABLE bridge_mappings__new RENAME TO bridge_mappings;
                """
            )

        # 6. Retire the "system agent" concept and emoji avatars entirely
        #    (agent-identity.md): drop both columns. Neither is in an index or
        #    foreign key, so a plain DROP COLUMN (SQLite ≥3.35) is safe.
        #    Guarded on presence so it runs exactly once and no-ops on a fresh
        #    DB whose schema never had the columns.
        for col in ("is_system", "avatar"):
            if await self._has_column("agents", col):
                await self._conn.execute(f"ALTER TABLE agents DROP COLUMN {col}")
        await self._conn.commit()

    async def _ensure_connected(self) -> None:
        # A closed Database is dead — never silently re-open. The
        # previous "reconnect" path was load-bearing for nothing in
        # production and was the root cause of a pytest atexit hang:
        # tests that closed the DB still had pending consumer tasks
        # that would call flush() during loop teardown, the reconnect
        # spawned a brand-new aiosqlite worker thread right before
        # the loop died, and that orphaned non-daemon thread pinned
        # the process. We raise CancelledError so in-flight callers
        # (e.g. session_manager._consume_message) exit cleanly via
        # their existing CancelledError handling.
        if self._closed:
            raise asyncio.CancelledError("Database is closed")
        assert self._conn is not None, "Database not initialized"

    async def close(self) -> None:
        if self._conn:
            if self._dirty:
                await self._conn.commit()
                self._dirty = False
            await self._conn.close()
            self._conn = None
        self._closed = True

    async def flush(self) -> None:
        """Commit pending writes."""
        await self._ensure_connected()
        if self._dirty:
            await self._conn.commit()
            self._dirty = False

    async def wal_checkpoint_truncate(self) -> tuple[int, int, int]:
        """Fully checkpoint the WAL and truncate it (`PRAGMA wal_checkpoint(TRUNCATE)`),
        returning SQLite's `(busy, log_frames, checkpointed_frames)` row.

        Used by the deploy snapshot (docs/plans/local-deploy.md §7.2/§7.4): after a
        clean TRUNCATE (``busy == 0``) every committed frame is folded into the main
        DB file and the WAL is emptied, so a plain file copy is a complete, self-
        contained snapshot. ``busy != 0`` means a concurrent reader blocked the
        truncate — the caller must NOT treat a bare file copy as complete
        (the WAL still holds committed frames), which is exactly the silent-busy
        data-loss trap SQLite's TRUNCATE checkpoint hides behind a non-raising
        return."""
        await self._ensure_connected()
        # Commit our own pending writes first so they are in the WAL to fold in.
        if self._dirty:
            await self._conn.commit()
            self._dirty = False
        cursor = await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:  # pragma: no cover - PRAGMA always returns a row
            return (1, -1, -1)
        return (int(row[0]), int(row[1]), int(row[2]))

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not initialized"
        return self._conn

    @property
    def path(self) -> str:
        """The on-disk path of this database file (the deploy snapshot copies it
        and the switcher restores over it, docs/plans/local-deploy.md §7.4)."""
        return self._db_path

    async def save_session(
        self,
        session_id: str,
        name: str,
        working_dir: str,
        created_at: str,
        claude_session_id: str | None = None,
        credential_id: str | None = None,
        agent_id: str | None = None,
        origin: str = "user",
        backend: str = "claude-code",
        parent_session_id: str | None = None,
        delegation_request: str | None = None,
        model: str | None = None,
    ) -> None:
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT INTO sessions "
            "(id, name, working_dir, created_at, claude_session_id, "
            " credential_id, agent_id, origin, backend, "
            " parent_session_id, delegation_request, model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                name,
                working_dir,
                created_at,
                claude_session_id,
                credential_id,
                agent_id,
                origin,
                backend,
                parent_session_id,
                delegation_request,
                model,
            ),
        )
        await self._conn.commit()

    async def delete_session(self, session_id: str) -> None:
        await self._ensure_connected()
        await self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await self._conn.commit()

    async def create_fork_session(
        self,
        *,
        fork_id: str,
        name: str,
        working_dir: str,
        created_at: str,
        parent_id: str,
        backend: str,
        agent_id: str | None,
        credential_id: str | None,
        resume_id: str | None,
        fork_after_seq: int,
        fork_metadata: str | None = None,
        model: str | None = None,
    ) -> None:
        """The DB-only half of the fork saga (session-rewind.md §5.1 step
        5): INSERT the fork `sessions` row (origin='fork',
        fork_status='initializing', pre-minted resume id) and INSERT-SELECT the
        parent's messages with ``seq <= fork_after_seq`` — copied verbatim,
        including their git anchors. For M=0 (`fork_after_seq == -1`) the SELECT
        matches nothing. `fork_metadata` is written at INSERT (not deferred) so a
        /fork duplicate's cleanup-credential pin survives a prepare failure
        (session-fork.md). No FS, no git, no shell here — a clean rollback
        unit: the two writes are wrapped so a failed message-copy rolls back the
        row insert rather than leaving an open transaction a later commit would
        flush (Vera review SHOULD-FIX #1)."""
        await self._ensure_connected()
        try:
            await self._conn.execute(
                "INSERT INTO sessions "
                "(id, name, working_dir, created_at, claude_session_id, "
                " credential_id, agent_id, origin, backend, model, "
                " forked_from_session_id, fork_after_seq, fork_needs_replay, "
                " fork_status, fork_metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'fork', ?, ?, ?, ?, 0, "
                " 'initializing', ?)",
                (
                    fork_id, name, working_dir, created_at, resume_id,
                    credential_id, agent_id, backend, model, parent_id,
                    fork_after_seq, fork_metadata,
                ),
            )
            await self._conn.execute(
                "INSERT INTO messages "
                "(session_id, seq, role, type, content, tool_name, tool_input, "
                " tool_use_id, is_error, session_id_ref, cost, attachments, "
                " git_head, git_status_clean, created_at) "
                "SELECT ?, seq, role, type, content, tool_name, tool_input, "
                " tool_use_id, is_error, session_id_ref, cost, attachments, "
                " git_head, git_status_clean, created_at "
                "FROM messages WHERE session_id = ? AND seq <= ?",
                (fork_id, parent_id, fork_after_seq),
            )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    async def load_sessions(
        self, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        await self._ensure_connected()
        query = (
            "SELECT id, name, working_dir, created_at, claude_session_id, "
            "credential_id, archived, agent_id, origin, backend, model, "
            "parent_session_id, delegation_request, forked_from_session_id, "
            "fork_after_seq, fork_needs_replay, fork_metadata, "
            "fork_revert_record, fork_status FROM sessions"
        )
        if not include_archived:
            query += " WHERE archived = 0"
        cursor = await self._conn.execute(query)
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "name": row[1],
                "working_dir": row[2],
                "created_at": row[3],
                "claude_session_id": row[4],
                "credential_id": row[5],
                "archived": bool(row[6]),
                "agent_id": row[7],
                "origin": row[8] or "user",
                "backend": row[9] or "claude-code",
                "model": row[10],
                "parent_session_id": row[11],
                "delegation_request": row[12],
                "forked_from_session_id": row[13],
                "fork_after_seq": row[14],
                "fork_needs_replay": bool(row[15]),
                "fork_metadata": row[16],
                "fork_revert_record": row[17],
                "fork_status": row[18],
            }
            for row in rows
        ]

    async def count_messages(self, session_id: str) -> int:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        return row[0]

    async def session_exists(self, session_id: str) -> bool:
        """Whether a session row exists, including archived sessions."""
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (session_id,)
        )
        return await cursor.fetchone() is not None

    async def append_message(
        self,
        session_id: str,
        seq: int,
        role: str,
        type: str,
        content: Any = None,
        tool_name: str | None = None,
        tool_input: dict[str, Any] | None = None,
        tool_use_id: str | None = None,
        is_error: bool | None = None,
        session_id_ref: str | None = None,
        cost: float | None = None,
        attachments: list[dict[str, Any]] | None = None,
        git_head: str | None = None,
        git_status_clean: bool | None = None,
        injection_id: str | None = None,
    ) -> None:
        await self._ensure_connected()
        content_str = json.dumps(content) if content is not None else None
        tool_input_str = json.dumps(tool_input) if tool_input is not None else None
        is_error_int = int(is_error) if is_error is not None else None
        attachments_str = (
            json.dumps(attachments) if attachments else None
        )
        git_status_clean_int = (
            int(git_status_clean) if git_status_clean is not None else None
        )

        source_key = ""
        if injection_id is not None:
            # Friendly preflight; the trigger below is the authoritative
            # check and remains race-safe at INSERT time.
            src = await self._conn.execute(
                "SELECT source_key, prompt FROM session_injections "
                "WHERE id = ? AND session_id = ? AND status = 'pending'",
                (injection_id, session_id),
            )
            src_row = await src.fetchone()
            if (
                src_row is None
                or role != "user"
                or type != "text"
                or content != src_row[1]
            ):
                raise ValueError(
                    f"Injection {injection_id!r} is missing, targets another "
                    "session, has a different payload, or is no longer pending"
                )
            source_key = src_row[0]

        await self._conn.execute(
            "INSERT INTO messages "
            "(session_id, seq, role, type, content, tool_name, tool_input, "
            "tool_use_id, is_error, session_id_ref, cost, attachments, "
            "git_head, git_status_clean, injection_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                seq,
                role,
                type,
                content_str,
                tool_name,
                tool_input_str,
                tool_use_id,
                is_error_int,
                session_id_ref,
                cost,
                attachments_str,
                git_head,
                git_status_clean_int,
                injection_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        if injection_id is not None:
            # messages_ack_injection fired inside the INSERT statement. Keep
            # the old research column as a non-authoritative compatibility
            # mirror; correctness no longer depends on a second statement.
            delivered_at = datetime.now(timezone.utc).isoformat()
            # Keep research_jobs.injection_status as a compatibility mirror
            # for one release. session_injections is authoritative; this
            # mirror can be repaired by research recovery after a crash.
            if source_key.startswith("research:"):
                research_id = source_key.split(":", 2)[1]
                await self._conn.execute(
                    "UPDATE research_jobs SET injection_status = 'delivered', "
                    "injected_at = ? WHERE id = ?",
                    (delivered_at, research_id),
                )
        # A persisted/broadcast transcript event must not leave a write
        # transaction open for the rest of the model turn. TaskRepository is
        # an intentional second SQLite writer; batching ordinary messages
        # until turn-idle would hold WAL's single-writer lock across arbitrary
        # model/tool/MCP latency and make worker heartbeat/complete deadlock on
        # SQLITE_BUSY. Commit each event before its caller broadcasts it.
        await self._conn.commit()
        self._dirty = False

    # ------------------------------------------------------- session injections

    _INJECTION_COLS = (
        "id, source_key, session_id, prompt, status, created_at, "
        "delivered_at, error"
    )

    @staticmethod
    def _row_to_session_injection(row: Any) -> dict[str, Any]:
        return {
            "id": row[0],
            "source_key": row[1],
            "session_id": row[2],
            "prompt": row[3],
            "status": row[4],
            "created_at": row[5],
            "delivered_at": row[6],
            "error": row[7],
        }

    async def create_session_injection(
        self,
        *,
        injection_id: str,
        source_key: str,
        session_id: str,
        prompt: str,
        created_at: str,
    ) -> dict[str, Any]:
        """Persist one idempotent system-produced turn.

        `source_key` is the stable producer identity.  Repeating an enqueue
        with the same key returns the original row; changing its target or
        payload is rejected by SessionManager before dispatch.
        """
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT OR IGNORE INTO session_injections "
            "(id, source_key, session_id, prompt, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (injection_id, source_key, session_id, prompt, created_at),
        )
        await self._conn.commit()
        row = await self.get_session_injection_by_source(source_key)
        if row is None:
            raise RuntimeError(f"Failed to create injection {source_key!r}")
        return row

    async def get_session_injection(
        self, injection_id: str
    ) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._INJECTION_COLS} FROM session_injections WHERE id = ?",
            (injection_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_session_injection(row) if row else None

    async def get_session_injection_by_source(
        self, source_key: str
    ) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._INJECTION_COLS} FROM session_injections "
            "WHERE source_key = ?",
            (source_key,),
        )
        row = await cursor.fetchone()
        return self._row_to_session_injection(row) if row else None

    async def list_pending_session_injections(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._INJECTION_COLS} FROM session_injections "
            "WHERE status = 'pending' ORDER BY created_at, id"
        )
        return [
            self._row_to_session_injection(row)
            for row in await cursor.fetchall()
        ]

    async def worker_has_persisted_pending_work(self, session_id: str) -> bool:
        """Whether durable async work still points at a task-worker session.

        TaskBoardManager combines this DB predicate with SessionManager's live
        turn/queue/approval state.  Keeping the SQL in Database prevents the
        terminal-protocol and lease checks from drifting into separate,
        incomplete definitions of "waiting".
        """
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT EXISTS("
            " SELECT 1 FROM bg_tasks WHERE session_id = ? "
            "   AND status IN ('pending', 'running')"
            " UNION ALL"
            " SELECT 1 FROM bg_tasks b WHERE b.session_id = ? "
            "   AND b.delivery_required = 1 "
            "   AND b.status IN ('completed', 'failed', 'cancelled', 'interrupted') "
            "   AND NOT EXISTS ("
            "     SELECT 1 FROM session_injections i "
            "     WHERE i.source_key = 'bg:' || b.id"
            "   )"
            " UNION ALL"
            " SELECT 1 FROM research_jobs WHERE session_id = ? AND status = 'running'"
            " UNION ALL"
            " SELECT 1 FROM research_jobs r WHERE r.session_id = ? "
            "   AND r.status = 'completed' AND r.injection_status = 'pending' "
            "   AND NOT EXISTS ("
            "     SELECT 1 FROM session_injections i "
            "     WHERE i.source_key = 'research:' || r.id"
            "   )"
            " UNION ALL"
            " SELECT 1 FROM delegation_runs dr JOIN sessions child "
            "   ON child.id = dr.delegation_id"
            "   WHERE child.parent_session_id = ? AND dr.state = 'running'"
            " UNION ALL"
            " SELECT 1 FROM delegation_runs dr JOIN sessions child "
            "   ON child.id = dr.delegation_id"
            "   WHERE child.parent_session_id = ? "
            "   AND dr.state IN ('completed', 'failed', 'cancelled', 'interrupted') "
            "   AND NOT EXISTS ("
            "     SELECT 1 FROM session_injections i "
            "     WHERE i.source_key = 'delegation:' || dr.run_id || ':terminal'"
            "   )"
            " UNION ALL"
            " SELECT 1 FROM session_injections WHERE session_id = ? AND status = 'pending'"
            " UNION ALL"
            " SELECT 1 FROM parked_turns WHERE session_id = ?"
            ")",
            (
                session_id,
                session_id,
                session_id,
                session_id,
                session_id,
                session_id,
                session_id,
                session_id,
            ),
        )
        row = await cursor.fetchone()
        return bool(row and row[0])

    async def reconcile_session_injection(self, injection_id: str) -> bool:
        """Acknowledge a legacy half-state if its transcript row exists.

        New writes cannot produce this state because ``messages_ack_injection``
        runs inside the INSERT statement. The check remains necessary when
        upgrading a database written by the pre-trigger implementation or
        recovering from an old crash window. It must happen before replay or
        the unique message index would reject the duplicate while leaving the
        outbox row pending forever.
        """
        await self._ensure_connected()
        delivered_at = datetime.now(timezone.utc).isoformat()
        cursor = await self._conn.execute(
            "UPDATE session_injections SET status = 'delivered', "
            "delivered_at = ?, error = NULL "
            "WHERE id = ? AND status = 'pending' AND EXISTS ("
            "  SELECT 1 FROM messages m "
            "  WHERE m.injection_id = session_injections.id "
            "    AND m.session_id = session_injections.session_id"
            ")",
            (delivered_at, injection_id),
        )
        if cursor.rowcount:
            src = await self._conn.execute(
                "SELECT source_key FROM session_injections WHERE id = ?",
                (injection_id,),
            )
            src_row = await src.fetchone()
            source_key = src_row[0] if src_row else ""
            if source_key.startswith("research:"):
                research_id = source_key.split(":", 2)[1]
                await self._conn.execute(
                    "UPDATE research_jobs SET injection_status = 'delivered', "
                    "injected_at = ? WHERE id = ?",
                    (delivered_at, research_id),
                )
        await self._conn.commit()
        return bool(cursor.rowcount)

    async def fail_session_injection(
        self, injection_id: str, error: str
    ) -> bool:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "UPDATE session_injections SET status = 'failed', error = ? "
            "WHERE id = ? AND status = 'pending'",
            (error, injection_id),
        )
        if cursor.rowcount:
            src = await self._conn.execute(
                "SELECT source_key FROM session_injections WHERE id = ?",
                (injection_id,),
            )
            src_row = await src.fetchone()
            source_key = src_row[0] if src_row else ""
            if source_key.startswith("research:"):
                research_id = source_key.split(":", 2)[1]
                await self._conn.execute(
                    "UPDATE research_jobs SET injection_status = 'failed', "
                    "error = ? WHERE id = ?",
                    (f"delivery failed: {error}", research_id),
                )
        await self._conn.commit()
        return bool(cursor.rowcount)

    async def load_messages(
        self, session_id: str, limit: int = 0, offset: int = 0
    ) -> list[dict[str, Any]]:
        await self._ensure_connected()
        await self.flush()  # ensure pending writes are visible
        query = (
            "SELECT seq, role, type, content, tool_name, tool_input, tool_use_id, "
            "is_error, session_id_ref, cost, attachments, git_head, "
            "git_status_clean, created_at "
            "FROM messages WHERE session_id = ? ORDER BY seq"
        )
        params: list = [session_id]
        if limit > 0:
            query += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            content = json.loads(row[3]) if row[3] is not None else None
            tool_input = json.loads(row[5]) if row[5] is not None else None
            is_error = bool(row[7]) if row[7] is not None else None
            attachments = json.loads(row[10]) if row[10] is not None else []
            git_status_clean = bool(row[12]) if row[12] is not None else None
            results.append(
                {
                    "seq": row[0],
                    "role": row[1],
                    "type": row[2],
                    "content": content,
                    "tool_name": row[4],
                    "tool_input": tool_input,
                    "tool_use_id": row[6],
                    "is_error": is_error,
                    "session_id": row[8],
                    "cost": row[9],
                    "attachments": attachments,
                    "git_head": row[11],
                    "git_status_clean": git_status_clean,
                    "created_at": row[13],
                }
            )
        return results

    # --- Bridge mappings ---

    async def save_bridge_mapping(
        self,
        platform: str,
        chat_id: str,
        agent_id: str,
        session_id: str | None = None,
    ) -> None:
        """Bind (platform, chat_id) to an agent, with an optional sticky
        session pointer (the currently-open thread for this chat). Upserts
        on conflict so a rebind preserves the chat's `verbose` preference
        (a chat-level setting that outlives any single agent/thread)."""
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT INTO bridge_mappings "
            "(platform, chat_id, agent_id, session_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(platform, chat_id) DO UPDATE SET "
            "agent_id = excluded.agent_id, session_id = excluded.session_id",
            (platform, chat_id, agent_id, session_id),
        )
        await self._conn.commit()

    async def set_bridge_verbose(
        self, platform: str, chat_id: str, verbose: bool
    ) -> None:
        """Set a chat's output verbosity (quiet = octo replies only)."""
        await self._ensure_connected()
        await self._conn.execute(
            "UPDATE bridge_mappings SET verbose = ? "
            "WHERE platform = ? AND chat_id = ?",
            (1 if verbose else 0, platform, chat_id),
        )
        await self._conn.commit()

    async def set_bridge_sticky_session(
        self, platform: str, chat_id: str, session_id: str | None
    ) -> None:
        """Repoint a chat's sticky session (or clear it with None) without
        touching its agent binding."""
        await self._ensure_connected()
        await self._conn.execute(
            "UPDATE bridge_mappings SET session_id = ? "
            "WHERE platform = ? AND chat_id = ?",
            (session_id, platform, chat_id),
        )
        await self._conn.commit()

    async def clear_bridge_sticky_for_session(self, session_id: str) -> int:
        """Null every sticky pointer aimed at a session that's going away
        (archived). The chat keeps its agent binding; the next inbound
        message opens a fresh thread. Returns rows updated."""
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "UPDATE bridge_mappings SET session_id = NULL WHERE session_id = ?",
            (session_id,),
        )
        await self._conn.commit()
        return cursor.rowcount

    async def delete_bridge_mapping(self, platform: str, chat_id: str) -> None:
        await self._ensure_connected()
        await self._conn.execute(
            "DELETE FROM bridge_mappings WHERE platform = ? AND chat_id = ?",
            (platform, chat_id),
        )
        await self._conn.commit()

    async def load_bridge_mappings(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT platform, chat_id, agent_id, session_id, verbose "
            "FROM bridge_mappings"
        )
        rows = await cursor.fetchall()
        return [
            {
                "platform": row[0],
                "chat_id": row[1],
                "agent_id": row[2],
                "session_id": row[3],
                "verbose": bool(row[4]),
            }
            for row in rows
        ]

    # --- Schedules ---

    async def save_schedule(
        self,
        schedule_id: str,
        agent_id: str,
        name: str,
        prompt: str,
        created_at: str,
        interval_seconds: int | None = None,
        cron: str | None = None,
        timezone: str | None = None,
        recurrence_label: str | None = None,
        enabled: bool = True,
        origin_session_id: str | None = None,
        run_at: str | None = None,
    ) -> None:
        """Persist a schedule. Recurrence is one of `interval_seconds`, `cron`
        (with `timezone`), or `run_at` (ISO datetime, fires once then auto-deletes).
        `origin_session_id`, when set, is the session the `/schedule` command was
        typed in — fires append into it instead of a throwaway session."""
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT INTO schedules (id, agent_id, origin_session_id, name, prompt, "
            "interval_seconds, cron, timezone, recurrence_label, enabled, "
            "created_at, run_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                schedule_id,
                agent_id,
                origin_session_id,
                name,
                prompt,
                interval_seconds,
                cron,
                timezone,
                recurrence_label,
                int(enabled),
                created_at,
                run_at,
            ),
        )
        await self._conn.commit()

    async def load_schedules(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT id, agent_id, name, prompt, interval_seconds, cron, timezone, "
            "recurrence_label, enabled, created_at, last_run_at, origin_session_id, "
            "run_at FROM schedules"
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "agent_id": row[1],
                "name": row[2],
                "prompt": row[3],
                "interval_seconds": row[4],
                "cron": row[5],
                "timezone": row[6],
                "recurrence_label": row[7],
                "enabled": bool(row[8]),
                "created_at": row[9],
                "last_run_at": row[10],
                "origin_session_id": row[11],
                "run_at": row[12],
            }
            for row in rows
        ]

    async def delete_schedule(self, schedule_id: str) -> None:
        await self._ensure_connected()
        await self._conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        await self._conn.commit()

    async def repoint_schedules_origin(
        self, old_session_id: str, new_session_id: str
    ) -> list[dict[str, Any]]:
        """Move every schedule anchored to `old_session_id` onto
        `new_session_id` (used when a session is archived and replaced — its
        schedules should keep appending into the live successor thread). Returns
        the affected schedule rows (post-update) so the caller can re-register
        their jobs. No-op returning [] when nothing points at the old session."""
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT id FROM schedules WHERE origin_session_id = ?",
            (old_session_id,),
        )
        affected = {row[0] for row in await cursor.fetchall()}
        if not affected:
            return []
        await self._conn.execute(
            "UPDATE schedules SET origin_session_id = ? WHERE origin_session_id = ?",
            (new_session_id, old_session_id),
        )
        await self._conn.commit()
        return [r for r in await self.load_schedules() if r["id"] in affected]

    async def update_schedule(self, schedule_id: str, **fields: Any) -> None:
        await self._ensure_connected()
        allowed = {
            "name",
            "prompt",
            "interval_seconds",
            "cron",
            "timezone",
            "recurrence_label",
            "enabled",
            "last_run_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if "enabled" in updates:
            updates["enabled"] = int(updates["enabled"])
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [schedule_id]
        await self._conn.execute(
            f"UPDATE schedules SET {set_clause} WHERE id = ?",
            values,
        )
        await self._conn.commit()

    # --- Backend credentials ---

    # Credentials are stored across two tables (Steal Plan B-4):
    #   - `backend_credentials` holds metadata + refresh-state columns
    #   - `credential_secrets` holds only the encrypted blob
    # We still write `backend_credentials.secret_encrypted` for back-compat
    # in case anything downstream reads the legacy column; new code should
    # treat `credential_secrets.secret_encrypted` as the source of truth.

    _CREDENTIAL_COLS = (
        "c.id",
        "c.backend",
        "c.label",
        "c.auth_type",
        "COALESCE(s.secret_encrypted, c.secret_encrypted) AS secret_encrypted",
        "c.created_at",
        "c.status",
        "c.token_expires_at",
        "c.needs_reconnect",
        "c.last_refresh_error_code",
    )

    def _row_to_credential(self, row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "id": row[0],
            "backend": row[1],
            "label": row[2],
            "auth_type": row[3],
            "secret_encrypted": row[4],
            "created_at": row[5],
            "status": row[6] or "active",
            "token_expires_at": row[7],
            "needs_reconnect": bool(row[8]),
            "last_refresh_error_code": row[9],
        }

    async def save_credential(
        self,
        credential_id: str,
        backend: str,
        label: str,
        auth_type: str,
        secret_encrypted: str,
        created_at: str,
    ) -> None:
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT INTO backend_credentials "
            "(id, backend, label, auth_type, secret_encrypted, created_at, "
            " status, needs_reconnect) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', 0)",
            (credential_id, backend, label, auth_type, secret_encrypted, created_at),
        )
        await self._conn.execute(
            "INSERT OR REPLACE INTO credential_secrets "
            "(credential_id, secret_encrypted) VALUES (?, ?)",
            (credential_id, secret_encrypted),
        )
        await self._conn.commit()

    async def load_credentials(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cols = ", ".join(self._CREDENTIAL_COLS)
        cursor = await self._conn.execute(
            f"SELECT {cols} FROM backend_credentials c "
            "LEFT JOIN credential_secrets s ON s.credential_id = c.id "
            "ORDER BY c.created_at"
        )
        rows = await cursor.fetchall()
        return [self._row_to_credential(row) for row in rows]

    async def get_credential(self, credential_id: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cols = ", ".join(self._CREDENTIAL_COLS)
        cursor = await self._conn.execute(
            f"SELECT {cols} FROM backend_credentials c "
            "LEFT JOIN credential_secrets s ON s.credential_id = c.id "
            "WHERE c.id = ?",
            (credential_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_credential(row)

    async def update_credential(self, credential_id: str, **fields: Any) -> None:
        await self._ensure_connected()
        meta_allowed = {
            "label",
            "status",
            "token_expires_at",
            "needs_reconnect",
            "last_refresh_error_code",
        }
        # Nullable columns need to be writable to NULL (e.g. clearing a
        # stale `last_refresh_error_code` after a successful refresh).
        # Callers that want to leave a column alone should just not pass it.
        nullable_meta = {"token_expires_at", "last_refresh_error_code"}
        meta_updates = {
            k: v
            for k, v in fields.items()
            if k in meta_allowed and (v is not None or k in nullable_meta)
        }
        if "needs_reconnect" in meta_updates and meta_updates["needs_reconnect"] is not None:
            meta_updates["needs_reconnect"] = int(bool(meta_updates["needs_reconnect"]))

        secret_value = fields.get("secret_encrypted")

        if meta_updates:
            # Legacy column gets the same secret to keep readers consistent
            # if they bypass the JOIN.
            applied = dict(meta_updates)
            if secret_value is not None:
                applied["secret_encrypted"] = secret_value
            set_clause = ", ".join(f"{k} = ?" for k in applied)
            values = list(applied.values()) + [credential_id]
            await self._conn.execute(
                f"UPDATE backend_credentials SET {set_clause} WHERE id = ?",
                values,
            )
        elif secret_value is not None:
            await self._conn.execute(
                "UPDATE backend_credentials SET secret_encrypted = ? WHERE id = ?",
                (secret_value, credential_id),
            )

        if secret_value is not None:
            await self._conn.execute(
                "INSERT OR REPLACE INTO credential_secrets "
                "(credential_id, secret_encrypted) VALUES (?, ?)",
                (credential_id, secret_value),
            )

        if meta_updates or secret_value is not None:
            await self._conn.commit()

    async def delete_credential(self, credential_id: str) -> bool:
        await self._ensure_connected()
        # ON DELETE CASCADE on credential_secrets handles the secret row.
        cursor = await self._conn.execute(
            "DELETE FROM backend_credentials WHERE id = ?", (credential_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Connectors (connectors.md). Installations mirror the credential
    # split-secret pattern; agent_connectors is the agent-scoped enable
    # join. The encrypted token blob lives only in
    # connector_installation_secrets and is fetched on demand by the
    # connector MCP subprocess via the internal /token route.
    # ------------------------------------------------------------------

    _CONNECTOR_COLS = (
        "id, kind, label, auth_type, external_account_id, scopes, "
        "enable_by_default, needs_reconnect, token_expires_at, "
        "last_refresh_error_code, created_at"
    )

    @staticmethod
    def _row_to_connector(row: tuple[Any, ...]) -> dict[str, Any]:
        try:
            scopes = json.loads(row[5]) if row[5] else []
        except (json.JSONDecodeError, TypeError):
            scopes = []
        return {
            "id": row[0],
            "kind": row[1],
            "label": row[2],
            "auth_type": row[3],
            "external_account_id": row[4],
            "scopes": scopes,
            "enable_by_default": bool(row[6]),
            "needs_reconnect": bool(row[7]),
            "token_expires_at": row[8],
            "last_refresh_error_code": row[9],
            "created_at": row[10],
        }

    async def save_connector_installation(
        self,
        *,
        installation_id: str,
        kind: str,
        label: str,
        auth_type: str,
        secret_encrypted: str,
        created_at: str,
        external_account_id: str | None = None,
        scopes: list[str] | None = None,
        enable_by_default: bool = False,
        token_expires_at: str | None = None,
    ) -> None:
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT INTO connector_installations "
            "(id, kind, label, auth_type, external_account_id, scopes, "
            " enable_by_default, needs_reconnect, token_expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                installation_id, kind, label, auth_type, external_account_id,
                json.dumps(scopes) if scopes is not None else None,
                int(bool(enable_by_default)), token_expires_at, created_at,
            ),
        )
        await self._conn.execute(
            "INSERT OR REPLACE INTO connector_installation_secrets "
            "(installation_id, secret_encrypted) VALUES (?, ?)",
            (installation_id, secret_encrypted),
        )
        await self._conn.commit()

    async def load_connector_installations(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._CONNECTOR_COLS} FROM connector_installations "
            "ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [self._row_to_connector(row) for row in rows]

    async def get_connector_installation(
        self, installation_id: str
    ) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._CONNECTOR_COLS} FROM connector_installations "
            "WHERE id = ?",
            (installation_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_connector(row) if row else None

    async def get_connector_installation_by_account(
        self, kind: str, external_account_id: str
    ) -> dict[str, Any] | None:
        """Look up by (kind, external account) — the dedup key the install
        flow upserts on."""
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._CONNECTOR_COLS} FROM connector_installations "
            "WHERE kind = ? AND external_account_id = ?",
            (kind, external_account_id),
        )
        row = await cursor.fetchone()
        return self._row_to_connector(row) if row else None

    async def get_connector_secret(self, installation_id: str) -> str | None:
        """The encrypted token blob — only the internal /token route reads
        this."""
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT secret_encrypted FROM connector_installation_secrets "
            "WHERE installation_id = ?",
            (installation_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def update_connector_installation(
        self, installation_id: str, **fields: Any
    ) -> None:
        await self._ensure_connected()
        meta_allowed = {
            "label",
            "external_account_id",
            "scopes",
            "enable_by_default",
            "needs_reconnect",
            "token_expires_at",
            "last_refresh_error_code",
        }
        # Nullable columns must be writable to NULL (e.g. clearing a stale
        # last_refresh_error_code after a good refresh). Columns omitted by
        # the caller are left untouched.
        nullable_meta = {
            "external_account_id",
            "scopes",
            "token_expires_at",
            "last_refresh_error_code",
        }
        meta_updates = {
            k: v
            for k, v in fields.items()
            if k in meta_allowed and (v is not None or k in nullable_meta)
        }
        if "scopes" in meta_updates and meta_updates["scopes"] is not None:
            meta_updates["scopes"] = json.dumps(meta_updates["scopes"])
        for boolish in ("enable_by_default", "needs_reconnect"):
            if boolish in meta_updates and meta_updates[boolish] is not None:
                meta_updates[boolish] = int(bool(meta_updates[boolish]))

        secret_value = fields.get("secret_encrypted")

        if meta_updates:
            set_clause = ", ".join(f"{k} = ?" for k in meta_updates)
            values = list(meta_updates.values()) + [installation_id]
            await self._conn.execute(
                f"UPDATE connector_installations SET {set_clause} WHERE id = ?",
                values,
            )

        if secret_value is not None:
            await self._conn.execute(
                "INSERT OR REPLACE INTO connector_installation_secrets "
                "(installation_id, secret_encrypted) VALUES (?, ?)",
                (installation_id, secret_value),
            )

        if meta_updates or secret_value is not None:
            await self._conn.commit()

    async def delete_connector_installation(self, installation_id: str) -> bool:
        await self._ensure_connected()
        # ON DELETE CASCADE drops the secret row and any agent_connectors links.
        cursor = await self._conn.execute(
            "DELETE FROM connector_installations WHERE id = ?", (installation_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    # --- agent-scoped enablement join -------------------------------------

    async def set_agent_connector(
        self, agent_id: str, installation_id: str, enabled: bool
    ) -> None:
        """Toggle one connector for one agent (presence in the join = on)."""
        await self._ensure_connected()
        if enabled:
            await self._conn.execute(
                "INSERT OR IGNORE INTO agent_connectors "
                "(agent_id, installation_id) VALUES (?, ?)",
                (agent_id, installation_id),
            )
        else:
            await self._conn.execute(
                "DELETE FROM agent_connectors "
                "WHERE agent_id = ? AND installation_id = ?",
                (agent_id, installation_id),
            )
        await self._conn.commit()

    async def get_agent_connector_ids(self, agent_id: str) -> list[str]:
        """Installation ids enabled for an agent."""
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT installation_id FROM agent_connectors WHERE agent_id = ?",
            (agent_id,),
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def get_enabled_connectors_for_agent(
        self, agent_id: str
    ) -> list[dict[str, Any]]:
        """Full installation rows for an agent's enabled connectors — the
        join SessionManager reads at spawn time to build the MCP set."""
        await self._ensure_connected()
        cols = ", ".join(f"ci.{c}" for c in self._CONNECTOR_COLS.split(", "))
        cursor = await self._conn.execute(
            f"SELECT {cols} FROM connector_installations ci "
            "JOIN agent_connectors ac ON ac.installation_id = ci.id "
            "WHERE ac.agent_id = ? ORDER BY ci.created_at",
            (agent_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_connector(row) for row in rows]

    # --- per-kind OAuth client credentials (in-app config) ----------------

    async def set_connector_oauth_client(
        self, kind: str, client_id: str, client_secret_encrypted: str, now: str
    ) -> None:
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT INTO connector_oauth_clients "
            "(kind, client_id, client_secret_encrypted, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(kind) DO UPDATE SET "
            "client_id=excluded.client_id, "
            "client_secret_encrypted=excluded.client_secret_encrypted, "
            "updated_at=excluded.updated_at",
            (kind, client_id, client_secret_encrypted, now, now),
        )
        await self._conn.commit()

    async def get_connector_oauth_client(
        self, kind: str
    ) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT kind, client_id, client_secret_encrypted "
            "FROM connector_oauth_clients WHERE kind = ?",
            (kind,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "kind": row[0],
            "client_id": row[1],
            "client_secret_encrypted": row[2],
        }

    async def delete_connector_oauth_client(self, kind: str) -> bool:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "DELETE FROM connector_oauth_clients WHERE kind = ?", (kind,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def delete_connector_installations_by_kind(self, kind: str) -> int:
        """Delete every installation of a kind (cascades to secrets +
        agent_connectors). Used when a custom connector is removed."""
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "DELETE FROM connector_installations WHERE kind = ?", (kind,)
        )
        await self._conn.commit()
        return cursor.rowcount

    # --- custom (user-defined) connector definitions ----------------------

    @staticmethod
    def _row_to_custom(row: tuple[Any, ...]) -> dict[str, Any]:
        # Columns: kind, display_name, authorize_url, token_url, scopes, pkce,
        # api_base, created_at, updated_at.
        try:
            scopes = json.loads(row[4]) if row[4] else []
        except (json.JSONDecodeError, TypeError):
            scopes = []
        return {
            "kind": row[0],
            "display_name": row[1],
            "authorize_url": row[2],
            "token_url": row[3],
            "scopes": scopes,
            "pkce": bool(row[5]),
            "api_base": row[6],
            "created_at": row[7],
            "updated_at": row[8],
        }

    async def save_custom_connector(
        self,
        *,
        kind: str,
        display_name: str,
        authorize_url: str,
        token_url: str,
        scopes: list[str],
        pkce: bool,
        api_base: str,
        now: str,
    ) -> None:
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT INTO custom_connectors "
            "(kind, display_name, authorize_url, token_url, scopes, pkce, "
            " api_base, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(kind) DO UPDATE SET "
            "display_name=excluded.display_name, "
            "authorize_url=excluded.authorize_url, token_url=excluded.token_url, "
            "scopes=excluded.scopes, pkce=excluded.pkce, "
            "api_base=excluded.api_base, updated_at=excluded.updated_at",
            (
                kind, display_name, authorize_url, token_url,
                json.dumps(scopes), int(bool(pkce)), api_base, now, now,
            ),
        )
        await self._conn.commit()

    _CUSTOM_COLS = (
        "kind, display_name, authorize_url, token_url, scopes, pkce, "
        "api_base, created_at, updated_at"
    )

    async def get_custom_connector(self, kind: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._CUSTOM_COLS} FROM custom_connectors WHERE kind = ?",
            (kind,),
        )
        row = await cursor.fetchone()
        return self._row_to_custom(row) if row else None

    async def list_custom_connectors(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._CUSTOM_COLS} FROM custom_connectors ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [self._row_to_custom(row) for row in rows]

    async def delete_custom_connector(self, kind: str) -> bool:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "DELETE FROM custom_connectors WHERE kind = ?", (kind,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def load_incomplete_forks(self) -> list[dict[str, Any]]:
        """Fork rows whose §5.1 saga didn't reach 'ready' — startup recovery
        dispatches on `fork_status` (session-rewind.md §5.6.7). The
        resume id rides in `claude_session_id` (the pre-minted handle stored in
        the saga's step-5 INSERT)."""
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT id, forked_from_session_id, working_dir, backend, "
            "claude_session_id, fork_status, fork_revert_record, credential_id, "
            "agent_id, fork_metadata "
            "FROM sessions WHERE origin = 'fork' AND fork_status IN "
            "('initializing', 'reverting')"
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0],
                "forked_from_session_id": r[1],
                "working_dir": r[2],
                "backend": r[3] or "claude-code",
                "resume_id": r[4],
                "fork_status": r[5],
                "fork_revert_record": r[6],
                "credential_id": r[7],
                "agent_id": r[8],
                "fork_metadata": r[9],
            }
            for r in rows
        ]

    async def update_session_field(self, session_id: str, **fields: Any) -> None:
        await self._ensure_connected()
        allowed = {
            "name",
            "working_dir",
            "claude_session_id",
            "credential_id",
            "archived",
            "agent_id",
            "origin",
            "backend",
            # Fork columns (session-rewind.md §4). fork_metadata is
            # nullable+clearable (set to None to clear after first turn);
            # the others are written across the §5.1 saga.
            "forked_from_session_id",
            "fork_after_seq",
            "fork_needs_replay",
            "fork_metadata",
            "fork_revert_record",
            "fork_status",
        }
        # Columns that must be writable to NULL. `fork_metadata` clears after
        # the fork's first result; `claude_session_id` must be clearable so a
        # HISTORY_REPLAY fork (Codex) can overwrite the pre-minted resume_id
        # hint with NULL post-prepare_fork — otherwise a restart before the
        # first turn reloads the bogus hint and spawns `codex resume <bogus>`
        # (Vera review BLOCKING #1). Other columns omitted by the caller are
        # left untouched.
        nullable = {"fork_metadata", "claude_session_id"}
        bool_fields = {"archived", "fork_needs_replay"}
        updates: dict[str, Any] = {}
        for k, v in fields.items():
            if k not in allowed:
                continue
            if v is None and k not in nullable:
                continue
            updates[k] = int(bool(v)) if k in bool_fields else v
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [session_id]
        await self._conn.execute(
            f"UPDATE sessions SET {set_clause} WHERE id = ?",
            values,
        )
        await self._conn.commit()

    # --- Agents ---

    # Agents own sessions, schedules and bridge bindings (agent-refactor.md
    # §4.1). Stateless rows — AgentManager wraps these for the routes;
    # SessionManager reads them directly at spawn time so editing an agent
    # affects its open sessions on their next turn.

    _AGENT_COLS = (
        "id, name, description, system_prompt, model, credential_id, "
        "mcp_servers, tool_allow, tool_deny, archived, "
        "created_at, updated_at, backend"
    )

    @staticmethod
    def _row_to_agent(row: tuple[Any, ...]) -> dict[str, Any]:
        try:
            mcp_servers = json.loads(row[6]) if row[6] else []
        except (json.JSONDecodeError, TypeError):
            mcp_servers = []
        agent = {
            "id": row[0],
            "name": row[1],
            "description": row[2] or "",
            "system_prompt": row[3] or "",
            "model": row[4],
            "credential_id": row[5],
            "mcp_servers": mcp_servers,
            "tool_allow": row[7] or "",
            "tool_deny": row[8] or "",
            "archived": bool(row[9]),
            "created_at": row[10],
            "updated_at": row[11],
            "backend": row[12] or "claude-code",
        }
        # Optional active-session count appended by load_agents / get_agent.
        if len(row) > 13:
            agent["active_session_count"] = row[13]
        return agent

    # Subquery counting live (non-archived) sessions for an agent — shared
    # by load_agents and get_agent so the UI can show "3 sessions".
    _ACTIVE_SESSION_COUNT = (
        "(SELECT COUNT(*) FROM sessions s "
        " WHERE s.agent_id = a.id AND s.archived = 0)"
    )

    async def save_agent(
        self,
        *,
        agent_id: str,
        name: str,
        created_at: str,
        updated_at: str,
        description: str = "",
        system_prompt: str = "",
        model: str | None = None,
        credential_id: str | None = None,
        backend: str = "claude-code",
        mcp_servers: list[str] | None = None,
        tool_allow: str = "",
        tool_deny: str = "",
    ) -> None:
        await self._ensure_connected()
        servers_json = json.dumps(
            mcp_servers if mcp_servers is not None else _DEFAULT_MCP_SERVERS
        )
        await self._conn.execute(
            "INSERT INTO agents "
            "(id, name, description, system_prompt, model, "
            " credential_id, backend, mcp_servers, tool_allow, tool_deny, "
            " archived, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                agent_id, name, description, system_prompt, model,
                credential_id, backend or "claude-code", servers_json,
                tool_allow, tool_deny,
                created_at, updated_at,
            ),
        )
        await self._conn.commit()

    async def load_agents(
        self, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cols = ", ".join(f"a.{c}" for c in self._AGENT_COLS.split(", "))
        query = (
            f"SELECT {cols}, {self._ACTIVE_SESSION_COUNT} FROM agents a"
        )
        if not include_archived:
            query += " WHERE a.archived = 0"
        query += " ORDER BY a.created_at, a.id"
        cursor = await self._conn.execute(query)
        rows = await cursor.fetchall()
        return [self._row_to_agent(row) for row in rows]

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cols = ", ".join(f"a.{c}" for c in self._AGENT_COLS.split(", "))
        cursor = await self._conn.execute(
            f"SELECT {cols}, {self._ACTIVE_SESSION_COUNT} FROM agents a "
            "WHERE a.id = ?",
            (agent_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_agent(row) if row else None

    async def get_agent_by_name(
        self, name: str, *, include_archived: bool = False
    ) -> dict[str, Any] | None:
        await self._ensure_connected()
        cols = ", ".join(f"a.{c}" for c in self._AGENT_COLS.split(", "))
        query = (
            f"SELECT {cols}, {self._ACTIVE_SESSION_COUNT} FROM agents a "
            "WHERE a.name = ?"
        )
        params: list[Any] = [name]
        if not include_archived:
            query += " AND a.archived = 0"
        cursor = await self._conn.execute(query, params)
        row = await cursor.fetchone()
        return self._row_to_agent(row) if row else None

    async def get_default_agent(self) -> dict[str, Any] | None:
        """Some agent to fall back on when a caller supplies none — the first
        non-archived agent by creation order. Replaces the retired protected
        system agent (agent-identity.md); there is nothing special about the
        agent returned beyond being the oldest live one."""
        await self._ensure_connected()
        cols = ", ".join(f"a.{c}" for c in self._AGENT_COLS.split(", "))
        cursor = await self._conn.execute(
            f"SELECT {cols}, {self._ACTIVE_SESSION_COUNT} FROM agents a "
            "WHERE a.archived = 0 ORDER BY a.created_at, a.id LIMIT 1"
        )
        row = await cursor.fetchone()
        return self._row_to_agent(row) if row else None

    async def update_agent(self, agent_id: str, **fields: Any) -> None:
        await self._ensure_connected()
        allowed = {
            "name", "description", "system_prompt", "model",
            "credential_id", "backend", "mcp_servers", "tool_allow", "tool_deny",
            "archived",
        }
        # credential_id / model are nullable and may be cleared.
        nullable = {"credential_id", "model"}
        updates: dict[str, Any] = {}
        for k, v in fields.items():
            if k not in allowed:
                continue
            if v is None and k not in nullable:
                continue
            if k == "mcp_servers":
                updates[k] = json.dumps(v if v is not None else [])
            elif k == "archived":
                updates[k] = int(bool(v))
            else:
                updates[k] = v
        if not updates:
            return
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [agent_id]
        await self._conn.execute(
            f"UPDATE agents SET {set_clause} WHERE id = ?", values
        )
        await self._conn.commit()

    async def count_active_sessions_for_agent(self, agent_id: str) -> int:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE agent_id = ? AND archived = 0",
            (agent_id,),
        )
        row = await cursor.fetchone()
        return row[0]

    async def count_sessions_for_agent(self, agent_id: str) -> int:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE agent_id = ?",
            (agent_id,),
        )
        row = await cursor.fetchone()
        return row[0]

    async def archive_agent(self, agent_id: str) -> None:
        """Soft-delete an agent and cascade-archive its sessions."""
        await self._ensure_connected()
        await self._conn.execute(
            "UPDATE agents SET archived = 1, updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), agent_id),
        )
        await self._conn.execute(
            "UPDATE sessions SET archived = 1 WHERE agent_id = ?",
            (agent_id,),
        )
        await self._conn.commit()

    async def delete_agent(self, agent_id: str) -> bool:
        """Hard-delete an agent. FK ON DELETE CASCADE removes its sessions,
        schedules and bridge bindings — guarded by AgentManager so this is
        only reached when the agent has no sessions."""
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "DELETE FROM agents WHERE id = ?", (agent_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    # --- Notifiers ---

    async def save_notifier(
        self,
        notifier_id: str,
        type: str,
        label: str,
        config: dict[str, Any],
        created_at: str,
        enabled: bool = True,
    ) -> None:
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT INTO notifiers (id, type, label, config, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (notifier_id, type, label, json.dumps(config), int(enabled), created_at),
        )
        await self._conn.commit()

    async def load_notifiers(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT id, type, label, config, enabled, created_at "
            "FROM notifiers ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "type": row[1],
                "label": row[2],
                "config": json.loads(row[3]) if row[3] else {},
                "enabled": bool(row[4]),
                "created_at": row[5],
            }
            for row in rows
        ]

    async def delete_notifier(self, notifier_id: str) -> bool:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "DELETE FROM notifiers WHERE id = ?", (notifier_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def update_notifier(self, notifier_id: str, **fields: Any) -> None:
        await self._ensure_connected()
        allowed = {"label", "enabled", "config"}
        updates: dict[str, Any] = {}
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "enabled":
                updates[k] = int(bool(v))
            elif k == "config":
                updates[k] = json.dumps(v)
            else:
                updates[k] = v
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [notifier_id]
        await self._conn.execute(
            f"UPDATE notifiers SET {set_clause} WHERE id = ?", values
        )
        await self._conn.commit()

    # --- Background tasks (cross-turn) ---

    @staticmethod
    def _row_to_bg_task(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "id": row[0],
            "session_id": row[1],
            "command": row[2],
            "description": row[3],
            "working_dir": row[4],
            "status": row[5],
            "exit_code": row[6],
            "stdout": row[7] or "",
            "stderr": row[8] or "",
            "truncated": bool(row[9]),
            "started_at": row[10],
            "completed_at": row[11],
            "delivery_required": bool(row[12]),
        }

    _BG_TASK_COLS = (
        "id, session_id, command, description, working_dir, status, "
        "exit_code, stdout, stderr, truncated, started_at, completed_at, "
        "delivery_required"
    )

    async def create_bg_task(
        self,
        task_id: str,
        session_id: str,
        command: str,
        description: str | None,
        working_dir: str,
        started_at: str,
    ) -> None:
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT INTO bg_tasks "
            "(id, session_id, command, description, working_dir, status, "
            " stdout, stderr, truncated, started_at, delivery_required) "
            "VALUES (?, ?, ?, ?, ?, 'running', '', '', 0, ?, 1)",
            (task_id, session_id, command, description, working_dir, started_at),
        )
        await self._conn.commit()

    async def update_bg_task(self, task_id: str, **fields: Any) -> None:
        """Patch any of: status, exit_code, stdout, stderr, truncated, completed_at."""
        await self._ensure_connected()
        allowed = {
            "status",
            "exit_code",
            "stdout",
            "stderr",
            "truncated",
            "completed_at",
        }
        updates: dict[str, Any] = {}
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "truncated":
                updates[k] = int(bool(v))
            else:
                updates[k] = v
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [task_id]
        await self._conn.execute(
            f"UPDATE bg_tasks SET {set_clause} WHERE id = ?", values
        )
        await self._conn.commit()

    async def get_bg_task(self, task_id: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._BG_TASK_COLS} FROM bg_tasks WHERE id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_bg_task(row) if row else None

    async def list_bg_tasks_for_session(
        self, session_id: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._BG_TASK_COLS} FROM bg_tasks "
            "WHERE session_id = ? ORDER BY started_at DESC LIMIT ?",
            (session_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_bg_task(r) for r in rows]

    async def list_in_flight_bg_tasks(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._BG_TASK_COLS} FROM bg_tasks "
            "WHERE status IN ('running', 'pending') ORDER BY started_at"
        )
        return [self._row_to_bg_task(r) for r in await cursor.fetchall()]

    async def list_bg_tasks_missing_delivery(self) -> list[dict[str, Any]]:
        """Terminal post-cutover tasks with no durable outbox source."""
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._BG_TASK_COLS} FROM bg_tasks b "
            "WHERE b.delivery_required = 1 "
            "AND b.status IN ('completed', 'failed', 'cancelled', 'interrupted') "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM session_injections i "
            "  WHERE i.source_key = 'bg:' || b.id"
            ") ORDER BY b.completed_at, b.id"
        )
        return [self._row_to_bg_task(r) for r in await cursor.fetchall()]

    async def mark_in_flight_bg_tasks_interrupted(
        self, completed_at: str
    ) -> int:
        """Called once at startup: any row left in `running` belongs to a
        prior FastAPI process that crashed or was restarted. The
        subprocess is gone (child of the dead parent), so the row is
        garbage — flip it to `interrupted` so the chat doesn't show a
        spinner that will never resolve. Returns rows updated.
        """
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "UPDATE bg_tasks SET status = 'interrupted', completed_at = ? "
            "WHERE status IN ('running', 'pending')",
            (completed_at,),
        )
        await self._conn.commit()
        return cursor.rowcount

    # --- Research jobs (native-deep-research.md §6) ---

    _RESEARCH_COLS = (
        "id, session_id, question, status, phase, error, report_path, cost, "
        "created_at, completed_at, injection_status, injected_at"
    )

    @staticmethod
    def _row_to_research_job(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "id": row[0],
            "session_id": row[1],
            "question": row[2],
            "status": row[3],
            "phase": row[4],
            "error": row[5],
            "report_path": row[6],
            "cost": row[7],
            "created_at": row[8],
            "completed_at": row[9],
            "injection_status": row[10],
            "injected_at": row[11],
        }

    async def create_research_job(
        self, job_id: str, session_id: str, question: str, created_at: str
    ) -> None:
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT INTO research_jobs "
            "(id, session_id, question, status, phase, created_at, injection_status) "
            "VALUES (?, ?, ?, 'running', 'scope', ?, 'pending')",
            (job_id, session_id, question, created_at),
        )
        await self._conn.commit()

    async def update_research_job(self, job_id: str, **fields: Any) -> None:
        """Patch any of: status, phase, error, report_path, cost, completed_at,
        injection_status, injected_at."""
        await self._ensure_connected()
        allowed = {
            "status", "phase", "error", "report_path", "cost", "completed_at",
            "injection_status", "injected_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        await self._conn.execute(
            f"UPDATE research_jobs SET {set_clause} WHERE id = ?",
            list(updates.values()) + [job_id],
        )
        await self._conn.commit()

    async def get_research_job(self, job_id: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._RESEARCH_COLS} FROM research_jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_research_job(row) if row else None

    # A terminal `research_failed`/`research_cancelled`/`research_completed`
    # broadcast can be missed by a client that's mid-reconnect (unlike a
    # completed job, failed/cancelled/interrupted jobs have no fallback
    # transcript message). Widen the snapshot to also cover jobs that went
    # terminal within this window, so a reconnect shortly after the miss
    # still surfaces the card instead of the job vanishing without a trace
    # (Snape review — research-card dismiss task).
    _RESEARCH_SNAPSHOT_TERMINAL_WINDOW_SECONDS = 30

    async def list_research_jobs_for_session(
        self, session_id: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Snapshot for session load / WS reconnect: `running` jobs, plus
        anything that went terminal in the last
        `_RESEARCH_SNAPSHOT_TERMINAL_WINDOW_SECONDS`. The card is a progress
        indicator, not a history log — a terminal job outside that window
        must never come back from a page refresh once the frontend's own
        linger-then-dismiss timer has (or will have) removed it. Browsing
        finished research jobs is a separate, unbuilt feature (research-card
        dismiss task, §"不做")."""
        await self._ensure_connected()
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=self._RESEARCH_SNAPSHOT_TERMINAL_WINDOW_SECONDS)
        ).isoformat()
        cursor = await self._conn.execute(
            f"SELECT {self._RESEARCH_COLS} FROM research_jobs "
            "WHERE session_id = ? AND (status = 'running' OR completed_at > ?) "
            "ORDER BY created_at DESC LIMIT ?",
            (session_id, cutoff, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_research_job(r) for r in rows]

    async def list_pending_research_deliveries(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._RESEARCH_COLS} FROM research_jobs "
            "WHERE status = 'completed' AND injection_status = 'pending' "
            "ORDER BY completed_at, id"
        )
        return [self._row_to_research_job(r) for r in await cursor.fetchall()]

    async def mark_in_flight_research_jobs_interrupted(self, completed_at: str) -> int:
        """Boot sweep: a `running` row belongs to a prior process — its task is
        gone, so flip it to `interrupted` (native-deep-research.md §6). v1 does
        not resume mid-pipeline. Returns rows updated."""
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "UPDATE research_jobs SET status = 'interrupted', completed_at = ? "
            "WHERE status = 'running'",
            (completed_at,),
        )
        await self._conn.commit()
        return cursor.rowcount

    # --- Skill candidates (experience-consolidation.md §3.4/§5) ---

    _SKILL_CANDIDATE_COLS = (
        "id, slug, title, description, body_markdown, repository, rationale, "
        "status, proposed_by_agent_id, proposed_by_session_id, task_id, run_id, "
        "reviewed_at, review_note, landed_path, landed_branch, landed_commit, "
        "use_count, last_used_at, scope, bundle_files, lint_results, "
        "materialized_backends, superseded_at, created_at, updated_at"
    )

    @staticmethod
    def _row_to_skill_candidate(row: Any) -> dict[str, Any]:
        return {
            "id": row[0],
            "slug": row[1],
            "title": row[2],
            "description": row[3],
            "body_markdown": row[4],
            "repository": row[5],
            "rationale": row[6],
            "status": row[7],
            "proposed_by_agent_id": row[8],
            "proposed_by_session_id": row[9],
            "task_id": row[10],
            "run_id": row[11],
            "reviewed_at": row[12],
            "review_note": row[13],
            "landed_path": row[14],
            "landed_branch": row[15],
            "landed_commit": row[16],
            "use_count": row[17],
            "last_used_at": row[18],
            "scope": row[19],
            "bundle_files": json.loads(row[20]) if row[20] else None,
            "lint_results": json.loads(row[21]) if row[21] else None,
            "materialized_backends": json.loads(row[22]) if row[22] else None,
            "superseded_at": row[23],
            "created_at": row[24],
            "updated_at": row[25],
        }

    async def create_skill_candidate(
        self,
        *,
        candidate_id: str,
        slug: str,
        title: str,
        description: str,
        body_markdown: str,
        repository: str,
        rationale: str,
        proposed_by_agent_id: str | None,
        proposed_by_session_id: str | None,
        task_id: str | None,
        run_id: str | None,
        scope: str,
        bundle_files: dict[str, str] | None,
        lint_results: dict[str, Any] | None,
        created_at: str,
    ) -> dict[str, Any]:
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT INTO skill_candidates "
            "(id, slug, title, description, body_markdown, repository, rationale, "
            "status, proposed_by_agent_id, proposed_by_session_id, task_id, run_id, "
            "use_count, scope, bundle_files, lint_results, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
            (
                candidate_id, slug, title, description, body_markdown, repository,
                rationale, proposed_by_agent_id, proposed_by_session_id, task_id,
                run_id, scope,
                json.dumps(bundle_files) if bundle_files else None,
                json.dumps(lint_results) if lint_results else None,
                created_at, created_at,
            ),
        )
        await self._conn.commit()
        candidate = await self.get_skill_candidate(candidate_id)
        assert candidate is not None
        return candidate

    async def get_skill_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._SKILL_CANDIDATE_COLS} FROM skill_candidates WHERE id = ?",
            (candidate_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_skill_candidate(row) if row else None

    async def list_skill_candidates(
        self, *, status: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        await self._ensure_connected()
        if status:
            cursor = await self._conn.execute(
                f"SELECT {self._SKILL_CANDIDATE_COLS} FROM skill_candidates "
                "WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            cursor = await self._conn.execute(
                f"SELECT {self._SKILL_CANDIDATE_COLS} FROM skill_candidates "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        return [self._row_to_skill_candidate(r) for r in await cursor.fetchall()]

    async def get_latest_approved_skill_by_slug(
        self,
        slug: str,
        *,
        agent_id: str | None = None,
        repository: str | None = None,
        scope: str | None = None,
    ) -> dict[str, Any] | None:
        """The current landed skill for `slug`, if any — the row use_count/
        last_used tracking accrues on (there is no separate skills table).

        `agent_id`/`repository`, when given, scope the lookup to the
        candidate proposed by that agent for that repository OR to an
        `agent-global` candidate from that same agent (experience-
        consolidation-v2.md §3③ — a global candidate has no repository to
        match, so it must be reachable from every repository that agent
        works in) — without this, two different repositories (or agents)
        independently landing a skill under the same slug collide and
        whichever was approved most recently wins the lookup regardless of
        which one a given session actually has loaded (Snape review,
        experience-consolidation.md). Callers that genuinely want the newest
        approved candidate across all scopes (e.g. the review-queue diff view
        before a scope is known) may omit both.

        When `repository` is given and BOTH an exact-repository and an
        agent-global candidate exist for the same slug, the exact-repository
        one always ranks first regardless of which was approved more
        recently — matching `sync_codex_skills_dir`'s own "repo-scoped wins
        over global" precedence (Snape review: an unqualified
        `ORDER BY updated_at` could attribute a real invocation to the
        candidate that ISN'T actually the one loaded/discovered).

        `scope`, when given, replaces that (repository OR agent-global)
        heuristic with an EXACT scope match and no priority tie-break
        (T-B review round 2: namespace→scope misattribution). A real `Skill`
        tool_use's plugin namespace already encodes exactly which one was
        loaded — `_global` for `agent-global`, a specific repository's
        fingerprint for `agent+repo` — so a caller that has parsed that
        namespace knows the true scope outright and must not fall through
        to the ambiguous heuristic, which could resolve to a DIFFERENT
        same-slug candidate (e.g. a real `agent-global` invocation getting
        attributed to an `agent+repo` candidate that merely wins the
        heuristic's repo-scoped-first tie-break). `scope='agent+repo'`
        still requires `repository`; without one, no candidate can be
        identified and this returns `None` rather than guessing.

        Excludes superseded rows (`superseded_at IS NOT NULL`, Snape review)
        — a row a later same-repository approval relocated is no longer the
        active landed version, even though `status='approved'` stays true
        forever as the historical fact that it once was."""
        await self._ensure_connected()
        if scope == "agent+repo" and repository is None:
            return None
        query = (
            f"SELECT {self._SKILL_CANDIDATE_COLS} FROM skill_candidates "
            "WHERE slug = ? AND status = 'approved' AND superseded_at IS NULL"
        )
        params: list[Any] = [slug]
        if agent_id is not None:
            query += " AND proposed_by_agent_id = ?"
            params.append(agent_id)
        order_by = "updated_at DESC"
        if scope is not None:
            query += " AND scope = ?"
            params.append(scope)
            if scope == "agent+repo":
                query += " AND repository = ?"
                params.append(repository)
        elif repository is not None:
            query += " AND (repository = ? OR scope = 'agent-global')"
            params.append(repository)
            # An 'agent-global' row still has SOME `repository` value stored
            # (propose() always resolves one, regardless of scope — it's
            # just not what determines that row's landing location), so the
            # CASE must also require scope='agent+repo' or a global row
            # proposed from this same repository would wrongly tie for
            # priority 0 instead of correctly ranking behind an actual
            # repo-scoped match.
            order_by = (
                "(CASE WHEN scope = 'agent+repo' AND repository = ? "
                "THEN 0 ELSE 1 END), " + order_by
            )
            params.append(repository)
        query += f" ORDER BY {order_by} LIMIT 1"
        cursor = await self._conn.execute(query, params)
        row = await cursor.fetchone()
        return self._row_to_skill_candidate(row) if row else None

    async def review_skill_candidate(
        self,
        candidate_id: str,
        *,
        status: str,
        review_note: str | None,
        reviewed_at: str,
        scope: str | None = None,
        landed_path: str | None = None,
        landed_branch: str | None = None,
        landed_commit: str | None = None,
        materialized_backends: list[str] | None = None,
    ) -> dict[str, Any] | None:
        await self._ensure_connected()
        if scope is not None:
            await self._conn.execute(
                "UPDATE skill_candidates SET scope = ? WHERE id = ?",
                (scope, candidate_id),
            )
        await self._conn.execute(
            "UPDATE skill_candidates SET status = ?, review_note = ?, "
            "reviewed_at = ?, landed_path = ?, landed_branch = ?, landed_commit = ?, "
            "materialized_backends = ?, updated_at = ? WHERE id = ?",
            (
                status, review_note, reviewed_at, landed_path, landed_branch,
                landed_commit,
                json.dumps(materialized_backends) if materialized_backends else None,
                reviewed_at, candidate_id,
            ),
        )
        await self._conn.commit()
        return await self.get_skill_candidate(candidate_id)

    async def mark_skill_candidate_superseded(
        self, candidate_id: str, *, superseded_at: str
    ) -> None:
        """A later same-(agent, slug, repository) approval relocated this
        row's materialized copy elsewhere — exclude it from
        `get_latest_approved_skill_by_slug` from now on (Snape review)."""
        await self._ensure_connected()
        await self._conn.execute(
            "UPDATE skill_candidates SET superseded_at = ? WHERE id = ?",
            (superseded_at, candidate_id),
        )
        await self._conn.commit()

    async def record_skill_candidate_usage(
        self, candidate_id: str, *, used_at: str
    ) -> None:
        await self._ensure_connected()
        await self._conn.execute(
            "UPDATE skill_candidates SET use_count = use_count + 1, "
            "last_used_at = ? WHERE id = ?",
            (used_at, candidate_id),
        )
        await self._conn.commit()

    async def skill_candidates_with_slug_exist(
        self, slug: str, *, exclude_id: str | None = None
    ) -> bool:
        """Whether a pending/approved candidate already claims `slug` — the
        §3② "slug 冲突" static-lint check, informational only (reusing an
        approved slug is a legitimate replacement proposal; this just
        surfaces it on the review page rather than silently hiding it)."""
        await self._ensure_connected()
        query = (
            "SELECT 1 FROM skill_candidates "
            "WHERE slug = ? AND status IN ('pending', 'approved')"
        )
        params: list[Any] = [slug]
        if exclude_id is not None:
            query += " AND id != ?"
            params.append(exclude_id)
        query += " LIMIT 1"
        cursor = await self._conn.execute(query, params)
        return (await cursor.fetchone()) is not None

    async def create_skill_invocation(
        self,
        *,
        invocation_id: str,
        candidate_id: str,
        agent_id: str | None,
        repository: str | None,
        session_id: str | None,
        task_id: str | None,
        run_id: str | None,
        backend: str | None,
        used_at: str,
    ) -> None:
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT INTO skill_invocations "
            "(id, candidate_id, agent_id, repository, session_id, task_id, "
            "run_id, backend, used_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                invocation_id, candidate_id, agent_id, repository, session_id,
                task_id, run_id, backend, used_at,
            ),
        )
        await self._conn.commit()

    async def list_skill_invocations(
        self, candidate_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT id, candidate_id, agent_id, repository, session_id, task_id, "
            "run_id, backend, used_at FROM skill_invocations "
            "WHERE candidate_id = ? ORDER BY used_at DESC LIMIT ?",
            (candidate_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0], "candidate_id": r[1], "agent_id": r[2],
                "repository": r[3], "session_id": r[4], "task_id": r[5],
                "run_id": r[6], "backend": r[7], "used_at": r[8],
            }
            for r in rows
        ]

    # --- Minimal read-only summaries for the skill review-page evidence
    # chain (experience-consolidation-v2.md §3②) — tasks/task_runs/sessions
    # already live in this same DB file under their own owning modules
    # (task_board.repository, session_manager); these are read-only lookups
    # a skill candidate's stored task_id/run_id/proposed_by_session_id can
    # resolve to a human-meaningful label without pulling in those modules.

    async def get_task_summary(self, task_id: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT id, board_id, title, status FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {"id": row[0], "board_id": row[1], "title": row[2], "status": row[3]}

    async def get_run_summary(self, run_id: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT id, task_id, attempt_no, state FROM task_runs WHERE id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {"id": row[0], "task_id": row[1], "attempt_no": row[2], "state": row[3]}

    async def get_session_summary(self, session_id: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT id, backend, archived FROM sessions WHERE id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {"id": row[0], "backend": row[1], "archived": bool(row[2])}

    # --- Per-round agent delegation execution ledger ---

    _DELEGATION_RUN_COLS = (
        "r.run_id, r.delegation_id, r.round_no, r.request, r.start_seq, "
        "r.state, r.error, r.created_at, r.finished_at, "
        "s.parent_session_id, s.agent_id, a.name"
    )

    @staticmethod
    def _row_to_delegation_run(row: Any) -> dict[str, Any]:
        return {
            "run_id": row[0],
            "delegation_id": row[1],
            "round_no": row[2],
            "request": row[3],
            "start_seq": row[4],
            "state": row[5],
            "error": row[6],
            "created_at": row[7],
            "finished_at": row[8],
            "parent_session_id": row[9],
            "target_agent_id": row[10],
            "target_agent_name": row[11] or "unknown agent",
        }

    async def create_delegation_run(
        self,
        *,
        run_id: str,
        delegation_id: str,
        round_no: int,
        request: str,
        start_seq: int,
        created_at: str,
        state: str = "running",
        error: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT INTO delegation_runs "
            "(run_id, delegation_id, round_no, request, start_seq, state, "
            "error, created_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, delegation_id, round_no, request, start_seq, state,
                error, created_at, finished_at,
            ),
        )
        await self._conn.commit()

    async def finish_delegation_run(
        self,
        run_id: str,
        *,
        state: str,
        error: str | None,
        finished_at: str,
    ) -> bool:
        """Transition a running round exactly once to a terminal state."""
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "UPDATE delegation_runs SET state = ?, error = ?, finished_at = ? "
            "WHERE run_id = ? AND state = 'running'",
            (state, error, finished_at, run_id),
        )
        await self._conn.commit()
        return bool(cursor.rowcount)

    async def get_latest_delegation_run(
        self, delegation_id: str
    ) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._DELEGATION_RUN_COLS} FROM delegation_runs r "
            "JOIN sessions s ON s.id = r.delegation_id "
            "LEFT JOIN agents a ON a.id = s.agent_id "
            "WHERE r.delegation_id = ? ORDER BY r.round_no DESC LIMIT 1",
            (delegation_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_delegation_run(row) if row else None

    async def list_delegation_runs(
        self, delegation_id: str
    ) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._DELEGATION_RUN_COLS} FROM delegation_runs r "
            "JOIN sessions s ON s.id = r.delegation_id "
            "LEFT JOIN agents a ON a.id = s.agent_id "
            "WHERE r.delegation_id = ? ORDER BY r.round_no",
            (delegation_id,),
        )
        return [
            self._row_to_delegation_run(row)
            for row in await cursor.fetchall()
        ]

    async def list_all_delegation_runs_for_parent(
        self, parent_session_id: str
    ) -> list[dict[str, Any]]:
        """EVERY delegation round spawned from this parent session, across
        all children — for the replay assembly (attempt-replay.md §3.2),
        which anchors each round on the parent's timeline at `start_seq` and
        needs the full history, not just each child's latest round (unlike
        `list_latest_delegation_runs_for_parent`, which is the chat-UI
        status view)."""
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._DELEGATION_RUN_COLS} FROM delegation_runs r "
            "JOIN sessions s ON s.id = r.delegation_id "
            "LEFT JOIN agents a ON a.id = s.agent_id "
            "WHERE s.parent_session_id = ? ORDER BY r.start_seq, r.round_no",
            (parent_session_id,),
        )
        return [
            self._row_to_delegation_run(row)
            for row in await cursor.fetchall()
        ]

    async def list_latest_delegation_runs_for_parent(
        self, parent_session_id: str, *, limit: int = 25
    ) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._DELEGATION_RUN_COLS} FROM delegation_runs r "
            "JOIN sessions s ON s.id = r.delegation_id "
            "LEFT JOIN agents a ON a.id = s.agent_id "
            "WHERE s.parent_session_id = ? AND r.round_no = ("
            "  SELECT MAX(r2.round_no) FROM delegation_runs r2 "
            "  WHERE r2.delegation_id = r.delegation_id"
            ") ORDER BY r.created_at DESC LIMIT ?",
            (parent_session_id, limit),
        )
        return [
            self._row_to_delegation_run(row)
            for row in await cursor.fetchall()
        ]

    async def list_running_delegation_runs(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._DELEGATION_RUN_COLS} FROM delegation_runs r "
            "JOIN sessions s ON s.id = r.delegation_id "
            "LEFT JOIN agents a ON a.id = s.agent_id "
            "WHERE r.state = 'running' ORDER BY r.created_at"
        )
        return [
            self._row_to_delegation_run(row)
            for row in await cursor.fetchall()
        ]

    async def list_terminal_delegation_runs_missing_delivery(
        self,
    ) -> list[dict[str, Any]]:
        """Terminal rounds whose terminal outbox intent was never created."""
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._DELEGATION_RUN_COLS} FROM delegation_runs r "
            "JOIN sessions s ON s.id = r.delegation_id "
            "LEFT JOIN agents a ON a.id = s.agent_id "
            "WHERE r.state IN ('completed', 'failed', 'cancelled', 'interrupted') "
            "AND s.parent_session_id IS NOT NULL "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM session_injections i "
            "  WHERE i.source_key = 'delegation:' || r.run_id || ':terminal'"
            ") ORDER BY r.finished_at, r.run_id"
        )
        return [
            self._row_to_delegation_run(row)
            for row in await cursor.fetchall()
        ]

    async def delegation_session_has_runs(self, delegation_id: str) -> bool:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT 1 FROM delegation_runs WHERE delegation_id = ? LIMIT 1",
            (delegation_id,),
        )
        return await cursor.fetchone() is not None

    async def max_message_seq(self, session_id: str) -> int:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT COALESCE(MAX(seq), -1) FROM messages WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        return int(row[0])

    async def load_delegation_output(
        self, delegation_id: str, *, after_seq: int
    ) -> list[str]:
        """Durably rebuild the assistant text produced by one round."""
        await self._ensure_connected()
        await self.flush()
        cursor = await self._conn.execute(
            "SELECT content FROM messages WHERE session_id = ? AND seq > ? "
            "AND role = 'assistant' AND type = 'text' ORDER BY seq",
            (delegation_id, after_seq),
        )
        rows = await cursor.fetchall()
        output: list[str] = []
        for row in rows:
            if row[0] is None:
                continue
            try:
                value = json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(value, str):
                output.append(value)
        return output

    # ------------------------------------------------------------------ parked turns

    _PARKED_COLS = (
        "session_id, resume_mode, payload, resume_at_turn_start, limit_kind, "
        "reset_at, wake_at, attempts, probes, created_at"
    )

    @staticmethod
    def _row_to_parked_turn(row: Any) -> dict[str, Any]:
        return {
            "session_id": row[0],
            "resume_mode": row[1],
            "payload": row[2],
            "resume_at_turn_start": row[3],
            "limit_kind": row[4],
            "reset_at": row[5],
            "wake_at": row[6],
            "attempts": row[7],
            "probes": row[8],
            "created_at": row[9],
        }

    async def upsert_parked_turn(self, row: dict[str, Any]) -> None:
        """Park a limit-failed turn, or re-park an existing one (the PK is the
        session, so a re-park overwrites). limit-auto-resume.md §4."""
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT INTO parked_turns "
            "(session_id, resume_mode, payload, resume_at_turn_start, limit_kind, "
            " reset_at, wake_at, attempts, probes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "  resume_mode = excluded.resume_mode, "
            "  payload = excluded.payload, "
            "  resume_at_turn_start = excluded.resume_at_turn_start, "
            "  limit_kind = excluded.limit_kind, "
            "  reset_at = excluded.reset_at, "
            "  wake_at = excluded.wake_at, "
            "  attempts = excluded.attempts, "
            "  probes = excluded.probes",
            (
                row["session_id"], row["resume_mode"], row["payload"],
                row.get("resume_at_turn_start"), row.get("limit_kind"),
                row.get("reset_at"), row["wake_at"],
                row.get("attempts", 0), row.get("probes", 0), row["created_at"],
            ),
        )
        await self._conn.commit()

    async def get_parked_turn(self, session_id: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._PARKED_COLS} FROM parked_turns WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_parked_turn(row) if row else None

    async def list_parked_turns(self) -> list[dict[str, Any]]:
        """Every pending park, oldest wake-up first — the boot rebuild reads
        this to recreate the APScheduler jobs a restart destroyed."""
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._PARKED_COLS} FROM parked_turns ORDER BY wake_at"
        )
        rows = await cursor.fetchall()
        return [self._row_to_parked_turn(r) for r in rows]

    async def delete_parked_turn(self, session_id: str) -> bool:
        """Drop a park (it resumed, or the user cancelled it). True if a row went."""
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "DELETE FROM parked_turns WHERE session_id = ?", (session_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------ turn usage

    # The aggregate expressions every summarize_usage row/total carries.
    # SUM(cost) is NULL-safe by SQL semantics (all-NULL group → NULL, which
    # the mapper turns into None; mixed → sum of non-NULLs).
    _USAGE_AGGREGATES = (
        "COUNT(*), SUM(cost), SUM(input_tokens), SUM(cache_read_tokens), "
        "SUM(cache_creation_tokens), SUM(output_tokens), SUM(reasoning_tokens), "
        "SUM(total_tokens)"
    )

    _USAGE_GROUP_EXPRS = {
        "agent": "agent_id",
        "session": "session_id",
        "backend": "backend",
        "day": "substr(created_at, 1, 10)",  # ISO-8601 UTC → YYYY-MM-DD
    }

    async def add_turn_usage(
        self,
        *,
        created_at: str,
        session_id: str,
        backend: str,
        agent_id: str | None = None,
        model: str | None = None,
        cost: float | None = None,
        input_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        output_tokens: int = 0,
        reasoning_tokens: int = 0,
        duration_ms: int | None = None,
        is_error: bool = False,
        model_usage: dict[str, Any] | None = None,
        origin: str = "turn",
        message_seq: int | None = None,
    ) -> None:
        """Append one consumption row (usage-tracking.md §3). `total_tokens`
        is denormalized here so window SUMs never re-derive it.

        `message_seq` anchors this row onto `messages.seq` (attempt-replay.md
        §3.1 point 4) — the seq of the turn's `result` row. None for
        research-origin rows, which have no owning turn message."""
        await self._ensure_connected()
        total = (
            input_tokens + cache_read_tokens + cache_creation_tokens + output_tokens
        )
        await self._conn.execute(
            "INSERT INTO turn_usage "
            "(created_at, origin, session_id, agent_id, backend, model, cost, "
            "input_tokens, cache_read_tokens, cache_creation_tokens, "
            "output_tokens, reasoning_tokens, total_tokens, duration_ms, "
            "is_error, model_usage, message_seq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                created_at,
                origin,
                session_id,
                agent_id,
                backend,
                model,
                cost,
                input_tokens,
                cache_read_tokens,
                cache_creation_tokens,
                output_tokens,
                reasoning_tokens,
                total,
                duration_ms,
                int(is_error),
                json.dumps(model_usage) if model_usage else None,
                message_seq,
            ),
        )
        await self._conn.commit()

    async def list_turn_usage_for_session(self, session_id: str) -> list[dict[str, Any]]:
        """Per-turn cost/token rows for the replay assembly (attempt-replay.md
        §3.2), ordered oldest-first."""
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT created_at, origin, model, cost, input_tokens, "
            "cache_read_tokens, cache_creation_tokens, output_tokens, "
            "reasoning_tokens, total_tokens, duration_ms, is_error, message_seq "
            "FROM turn_usage WHERE session_id = ? ORDER BY id",
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "created_at": r[0],
                "origin": r[1],
                "model": r[2],
                "cost": r[3],
                "input_tokens": r[4],
                "cache_read_tokens": r[5],
                "cache_creation_tokens": r[6],
                "output_tokens": r[7],
                "reasoning_tokens": r[8],
                "total_tokens": r[9],
                "duration_ms": r[10],
                "is_error": bool(r[11]),
                "message_seq": r[12],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ harness exits

    async def add_harness_exit(
        self,
        *,
        session_id: str,
        reason: str,
        created_at: str,
        message_seq: int | None = None,
        exit_code: int | None = None,
        signal: int | None = None,
        escalation: str | None = None,
        reason_detail: dict[str, Any] | None = None,
        stderr_tail: str | None = None,
    ) -> None:
        """Persist the turn-termination invariant row (attempt-replay.md §3.1
        point 2). Called exactly once per HarnessRun from the single
        `finally:` choke point in `SessionManager._run_backend` — never
        skipped, regardless of how the run ended."""
        await self._ensure_connected()
        await self._conn.execute(
            "INSERT INTO harness_exits "
            "(session_id, message_seq, reason, exit_code, signal, escalation, "
            "reason_detail, stderr_tail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                message_seq,
                reason,
                exit_code,
                signal,
                escalation,
                json.dumps(reason_detail or {}),
                stderr_tail,
                created_at,
            ),
        )
        await self._conn.commit()

    async def list_harness_exits_for_session(self, session_id: str) -> list[dict[str, Any]]:
        """Terminal records for the replay assembly (attempt-replay.md §3.2),
        ordered oldest-first."""
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "SELECT message_seq, reason, exit_code, signal, escalation, "
            "reason_detail, stderr_tail, created_at "
            "FROM harness_exits WHERE session_id = ? ORDER BY id",
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "message_seq": r[0],
                "reason": r[1],
                "exit_code": r[2],
                "signal": r[3],
                "escalation": r[4],
                "reason_detail": json.loads(r[5]) if r[5] else {},
                "stderr_tail": r[6],
                "created_at": r[7],
            }
            for r in rows
        ]

    async def summarize_usage(
        self,
        *,
        group_by: str = "agent",
        since: str | None = None,
        until: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Window aggregation over turn_usage (usage-tracking.md §5).

        `since` (inclusive) / `until` (exclusive) are ISO-8601 UTC strings —
        plain TEXT compares are valid because created_at has a fixed layout.
        Returns `{"group_by", "rows", "totals"}`; id-keyed groupings are
        ordered by total_tokens DESC, `day` by key ASC."""
        expr = self._USAGE_GROUP_EXPRS.get(group_by)
        if expr is None:
            raise ValueError(f"unknown group_by: {group_by!r}")
        await self._ensure_connected()

        where: list[str] = []
        params: list[Any] = []
        if since is not None:
            where.append("created_at >= ?")
            params.append(since)
        if until is not None:
            where.append("created_at < ?")
            params.append(until)
        if agent_id is not None:
            where.append("agent_id = ?")
            params.append(agent_id)
        if session_id is not None:
            where.append("session_id = ?")
            params.append(session_id)
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        order_sql = (
            "ORDER BY 1 ASC" if group_by == "day" else "ORDER BY SUM(total_tokens) DESC"
        )

        cursor = await self._conn.execute(
            f"SELECT {expr}, {self._USAGE_AGGREGATES} FROM turn_usage"
            f"{where_sql} GROUP BY 1 {order_sql}",
            params,
        )
        rows = [self._usage_row(r[0], r[1:]) for r in await cursor.fetchall()]

        cursor = await self._conn.execute(
            f"SELECT {self._USAGE_AGGREGATES} FROM turn_usage{where_sql}", params
        )
        total_row = await cursor.fetchone()
        totals = self._usage_row(None, total_row)
        del totals["key"]
        if not rows:  # COUNT(*) is 0 but SUMs are NULL on an empty window
            totals = {"turns": 0, "cost": None} | {
                k: 0 for k in totals if k not in ("turns", "cost")
            }
        return {"group_by": group_by, "rows": rows, "totals": totals}

    @staticmethod
    def _usage_row(key: Any, agg: Any) -> dict[str, Any]:
        return {
            "key": key,
            "turns": agg[0],
            "cost": agg[1],
            "input_tokens": agg[2] or 0,
            "cache_read_tokens": agg[3] or 0,
            "cache_creation_tokens": agg[4] or 0,
            "output_tokens": agg[5] or 0,
            "reasoning_tokens": agg[6] or 0,
            "total_tokens": agg[7] or 0,
        }

    # --- Budgets (budget-model-routing.md §3) ---

    _BUDGET_COLS = (
        "id, scope, agent_id, window, limit_usd, soft_pct, enabled, "
        "soft_warned_window, created_at, updated_at"
    )

    @staticmethod
    def _row_to_budget(row: Any) -> dict[str, Any]:
        return {
            "id": row[0],
            "scope": row[1],
            "agent_id": row[2],
            "window": row[3],
            "limit_usd": row[4],
            "soft_pct": row[5],
            "enabled": bool(row[6]),
            "soft_warned_window": row[7],
            "created_at": row[8],
            "updated_at": row[9],
        }

    async def create_budget(
        self,
        *,
        scope: str,
        window: str,
        limit_usd: float,
        agent_id: str | None = None,
        soft_pct: float = 0.8,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Insert a budget. Raises sqlite3.IntegrityError on a duplicate
        (scope, agent, window) or a CHECK violation — the router maps those
        to 409/422 respectively."""
        await self._ensure_connected()
        budget_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO budgets (id, scope, agent_id, window, limit_usd, "
            "soft_pct, enabled, soft_warned_window, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                budget_id,
                scope,
                agent_id,
                window,
                limit_usd,
                soft_pct,
                int(bool(enabled)),
                now,
                now,
            ),
        )
        await self._conn.commit()
        created = await self.get_budget(budget_id)
        assert created is not None
        return created

    async def list_budgets(
        self, *, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        await self._ensure_connected()
        query = f"SELECT {self._BUDGET_COLS} FROM budgets"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY created_at, id"
        cursor = await self._conn.execute(query)
        return [self._row_to_budget(r) for r in await cursor.fetchall()]

    async def get_budget(self, budget_id: str) -> dict[str, Any] | None:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            f"SELECT {self._BUDGET_COLS} FROM budgets WHERE id = ?",
            (budget_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_budget(row) if row else None

    async def update_budget(
        self, budget_id: str, **fields: Any
    ) -> dict[str, Any] | None:
        """Patch mutable fields (window/limit_usd/soft_pct/enabled). scope and
        agent_id are identity and never change here. Unknown/None fields are
        ignored; a no-op patch still returns the current row."""
        await self._ensure_connected()
        allowed = {"window", "limit_usd", "soft_pct", "enabled"}
        updates: dict[str, Any] = {}
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            updates[k] = int(bool(v)) if k == "enabled" else v
        if updates:
            updates["updated_at"] = datetime.now(timezone.utc).isoformat()
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [budget_id]
            await self._conn.execute(
                f"UPDATE budgets SET {set_clause} WHERE id = ?", values
            )
            await self._conn.commit()
        return await self.get_budget(budget_id)

    async def delete_budget(self, budget_id: str) -> bool:
        await self._ensure_connected()
        cursor = await self._conn.execute(
            "DELETE FROM budgets WHERE id = ?", (budget_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def budget_spent_usd(
        self, *, window_start: str, agent_id: str | None = None
    ) -> float:
        """Claude USD spent since `window_start` (inclusive).

        `window_start` is a UTC `datetime.isoformat()` lower bound, the same
        vocabulary as `turn_usage.created_at`, so the plain TEXT `>=` compare
        the usage aggregation already relies on stays correct. SUM(cost)
        ignores NULL costs (Codex turns → 0); COALESCE turns an all-NULL or
        empty window into 0.0. When `agent_id` is given (agent-scoped budget)
        only that agent's rows count; global budgets pass None and see all
        spend, including `origin='research'`."""
        await self._ensure_connected()
        query = "SELECT COALESCE(SUM(cost), 0.0) FROM turn_usage WHERE created_at >= ?"
        params: list[Any] = [window_start]
        if agent_id is not None:
            query += " AND agent_id = ?"
            params.append(agent_id)
        cursor = await self._conn.execute(query, params)
        row = await cursor.fetchone()
        return float(row[0] or 0.0)

    async def mark_budget_soft_warned(
        self, budget_id: str, window_key: str
    ) -> bool:
        """Compare-and-set the soft-warned marker to `window_key`. Returns True
        only for the caller that actually flipped it — i.e. the first turn to
        cross the soft threshold this window. Concurrent turns for the same
        agent race here and all but one get False, so the warning fires once
        per window (§3.2)."""
        await self._ensure_connected()
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self._conn.execute(
            "UPDATE budgets SET soft_warned_window = ?, updated_at = ? "
            "WHERE id = ? AND (soft_warned_window IS NULL "
            "OR soft_warned_window != ?)",
            (window_key, now, budget_id, window_key),
        )
        await self._conn.commit()
        return cursor.rowcount > 0
