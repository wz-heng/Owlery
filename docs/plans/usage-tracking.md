# Usage Tracking — Tech Plan (per-turn cost + tokens, persisted and queryable)

## 0. What we're building, and why this shape

Every turn Octopus runs today reports its consumption — Claude in USD +
tokens, Codex in tokens — and we throw almost all of it away: the only
number that survives is `cost` on the per-turn `result` message row.
Tokens never leave the parser's `raw` dict; nothing carries a timestamp;
nothing is queryable per agent or per day.

This plan adds the **data foundation** for the later subscription-limit
awareness + cross-backend failover work:

1. a normalized token vocabulary on `HarnessEvent`, filled by both
   backend parsers instead of dropped;
2. a dedicated `turn_usage` table — one row per completed turn (and one
   per finished deep-research job), with its own timestamp, denormalized
   `agent_id`/`backend`, and no destructive FK so history survives
   session deletion;
3. an aggregation REST API (`GET /api/usage/summary`) grouping by
   agent / session / day / backend over a time window;
4. a simple usage page in the web UI (tables only — no charts).

Explicitly **not** in scope (task statement): chart visualisation, limit
logic, backfill of historical turns.

Why a dedicated table instead of widening `messages`: the `messages`
table has no timestamp (ordering is `seq`) and is on the hot render
path; usage queries are time-windowed aggregations with a completely
different access pattern. A narrow append-only table with
`(agent_id, created_at)` and `(session_id)` indexes keeps both sides
simple, and rows deliberately survive session deletion (consumption
already happened; the subscription doesn't get the tokens back).

## 1. Empirical grounding (probed against the real CLIs, not assumed)

**Claude** (`claude --print --output-format json`, probed 2026-07-07;
the `result` event carries the same shape in `stream-json`):

```json
"total_cost_usd": 0.0123,
"usage": {
  "input_tokens": 0,                       // fresh input — EXCLUDES cache reads
  "cache_creation_input_tokens": 0,
  "cache_read_input_tokens": 0,
  "output_tokens": 0,
  "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
  "service_tier": "standard",
  "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0},
  ...
},
"modelUsage": { "<model-id>": {"inputTokens": …, "outputTokens": …,
  "cacheReadInputTokens": …, "cacheCreationInputTokens": …, "costUSD": …, …} }
```

The probe ran unauthenticated (`is_error: true`) — the *structure* above
is what the CLI emits either way; `modelUsage` is `{}` on such runs, so
the parser must treat every key as optional and default to 0/None.

**Codex** (`codex exec --experimental-json`, probed 2026-07-07):

```json
{"type":"turn.completed","usage":{"input_tokens":11321,"cached_input_tokens":8064,"output_tokens":5,"reasoning_output_tokens":0}}
```

No USD anywhere (Codex reports tokens only). **`input_tokens` INCLUDES
`cached_input_tokens`** (11321 total, of which 8064 cached) — the
opposite convention from Claude. `reasoning_output_tokens` is a subset
of `output_tokens`, kept as an informational column.

**Where the data dies today** (all verified in-tree):

- `server/harness/claude_code.py:343` — `_result` keeps
  `total_cost_usd` / `duration_ms` / `num_turns`; `usage` + `modelUsage`
  stay in `raw` and go no further.
- `server/harness/codex.py:209-218` — `turn.completed` keeps `usage`
  only in `raw`, `cost=None`.
- `server/session_manager.py:2928-2934` — `_event_to_message_content`
  maps `result` → a `messages` row storing only `session_id_ref` +
  `cost`; `event.raw` is dropped.
- `server/session_manager.py:2974-2984` — the WS `result` event carries
  `cost` / `turns` / `duration_ms`, no tokens.
- `messages` has no timestamp column (`database.py:92-111`) — nothing
  time-windowed can be answered from it.
- Deep research: web leaves observe `result` events and sum only `cost`
  (`server/research/leaf.py:82-84`); reasoning leaves go through
  `run_oneshot` which returns text only. Job cost lands in
  `research_jobs.cost` — tokens are dropped there too.

## 2. Normalized token vocabulary

A `TokenUsage` dataclass in `server/harness/events.py`, plus
`usage: TokenUsage | None` and `model_usage: dict | None` fields on
`HarnessEvent` (filled only on `result`):

```python
@dataclass
class TokenUsage:
    input_tokens: int = 0           # fresh input, EXCLUDING cache reads
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    output_tokens: int = 0          # includes reasoning (codex)
    reasoning_tokens: int = 0       # codex only; informational subset of output

    @property
    def total_tokens(self) -> int:  # denormalized into the DB row for cheap SUMs
        return (self.input_tokens + self.cache_read_tokens
                + self.cache_creation_tokens + self.output_tokens)
```

Per-backend mapping (each parser fills it from its own native shape):

| normalized | Claude `usage` | Codex `usage` |
|---|---|---|
| `input_tokens` | `input_tokens` (already fresh-only) | `input_tokens - cached_input_tokens` (clamped ≥ 0) |
| `cache_read_tokens` | `cache_read_input_tokens` | `cached_input_tokens` |
| `cache_creation_tokens` | `cache_creation_input_tokens` | 0 (not reported) |
| `output_tokens` | `output_tokens` | `output_tokens` |
| `reasoning_tokens` | 0 (not reported) | `reasoning_output_tokens` |

`HarnessEvent.model_usage` carries Claude's `modelUsage` dict verbatim
(None for Codex / when absent) — persisted as JSON for future per-model
attribution without committing to its key names now.

## 3. Data model

New table in `_SCHEMA` (`CREATE TABLE IF NOT EXISTS` — the established
no-op-on-existing-DBs pattern used by `research_jobs` / `bg_tasks`; no
migration entry needed):

```sql
CREATE TABLE IF NOT EXISTS turn_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,               -- ISO-8601 UTC, stamped at capture
    origin TEXT NOT NULL DEFAULT 'turn',    -- 'turn' | 'research'
    session_id TEXT NOT NULL,               -- plain ref (no FK): rows must survive
                                            -- session deletion — the tokens were
                                            -- already spent
    agent_id TEXT,                          -- denormalized owner at capture time
    backend TEXT NOT NULL,                  -- 'claude-code' | 'codex'
    model TEXT,                             -- session's configured model; NULL = default
    cost REAL,                              -- USD; NULL when backend reports none (codex)
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    is_error INTEGER NOT NULL DEFAULT 0,
    model_usage TEXT                        -- Claude modelUsage JSON; NULL otherwise
);
CREATE INDEX IF NOT EXISTS idx_turn_usage_agent_time ON turn_usage(agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_turn_usage_session ON turn_usage(session_id);
```

`Database` methods (following the `save_*`/`load_*` conventions, cursor
idiom, immediate commit — this is not the hot message path):

- `add_turn_usage(...)` — one INSERT.
- `summarize_usage(group_by, since, until, agent_id, session_id)` — one
  `SELECT … SUM(…) … GROUP BY` returning `list[dict]`. `group_by=day`
  buckets on `substr(created_at, 1, 10)` (UTC date). Every row carries
  the same aggregate columns: `turns` (COUNT), `cost` (SUM, NULL-safe),
  the five token SUMs, and `total_tokens`.

A row is recorded for **every** `result` event, including `is_error`
ones (a failed turn may still have consumed tokens; zero-usage error
rows still count turns). Rows with all-zero usage and no cost are
normal for some error paths and are not filtered.

## 4. Capture paths

**Session turns** — the single funnel every session-based turn already
flows through (user chat, delegations, schedule fires, bridge turns):
the `result` branch of the event loop in
`server/session_manager.py` (~`:2164`), where `session` (id, `agent_id`,
`backend`, `model`) is in scope:

```python
if event.type == "result":
    ...existing resume-id handling...
    if self.db:
        await self.db.add_turn_usage(
            session_id=session.id, agent_id=session.agent_id,
            backend=session.backend, model=session.model,
            cost=event.cost, usage=event.usage,
            duration_ms=event.duration_ms, is_error=event.is_error,
            model_usage=event.model_usage, origin="turn",
        )
```

Failure to record must never fail the turn: the call is wrapped in
`try/except Exception: logger.exception(...)`.

**WS enrichment** — `_event_to_ws_message` adds a `usage` object (the
five normalized token fields) to the `result` WS event so a future UI
can show tokens live. Additive only; the frontend result handling is
untouched by this plan beyond the new usage page.

**Deep research** — web leaves flow through the same parsers, so
`LeafResult` gains `usage: TokenUsage | None`; the orchestrator sums
leaf usages into `ResearchReport.usage` exactly as it sums `cost`; the
manager records **one `turn_usage` row per completed job** with
`origin='research'` and the job's `session_id` / `agent_id`, stamped at
completion. Failed/cancelled jobs lose their partial usage — the exact
boundary `research_jobs.cost` already has today (the accumulators live
inside `run_research`, which raised). Rationale for one-row-per-job:
`research_jobs.cost` already aggregates at job granularity, and jobs
run for minutes while limit windows span hours — per-leaf timestamps
add rows, not information. Reasoning leaves (`run_oneshot`) are *not*
counted — same boundary as today's job `cost` (§9).

## 5. Aggregation API

New router `server/routers/usage.py`, registered in `main.py` like the
others; module-level `_db` injected at startup (the `schedules.py`
pattern); `verify_token` auth on every route.

```
GET /api/usage/summary
    ?group_by=agent|session|day|backend   (default: agent)
    &since=<ISO date/datetime>            (optional, inclusive)
    &until=<ISO date/datetime>            (optional, exclusive)
    &agent_id=<id>                        (optional filter)
    &session_id=<id>                      (optional filter)
```

Response:

```json
{
  "group_by": "agent",
  "rows": [
    {"key": "<agent-id>", "turns": 42, "cost": 1.2345,
     "input_tokens": 1000, "cache_read_tokens": 50000,
     "cache_creation_tokens": 2000, "output_tokens": 9000,
     "reasoning_tokens": 0, "total_tokens": 62000}
  ],
  "totals": { ...same aggregate fields summed over rows... }
}
```

`key` is the grouped value (`agent_id` — nullable → `null` key,
`session_id`, `YYYY-MM-DD`, or backend name). Rows are ordered by
`total_tokens` DESC for the id groupings and by `key` ASC for `day`.
Invalid `group_by` → 422 (FastAPI `Literal` validation); ISO parse
failures → 422. Comparisons are plain TEXT compares — valid because
`created_at` is ISO-8601 UTC with a fixed layout. Since agent/session
names live in already-fetched frontend state (and sessions may be
deleted), the API returns ids; the UI resolves names client-side.

## 6. Frontend — usage page

Follows the archived-sessions manage-page idiom exactly (there is no
router; "pages" are dialogs):

- `AccountDropdown` gains a "Usage" `DropdownMenuItem` →
  `onOpenUsage` prop; `App.tsx` holds `usageOpen` state and mounts
  `<UsageDialog open={usageOpen} onOpenChange={setUsageOpen} />`.
- `UsageDialog` (`src/components/UsageDialog.tsx`):
  - group-by toggle (Agent / Session / Day / Backend) + window select
    (7 / 30 / all days) as plain local state;
  - fetches `/api/usage/summary` with the `AgentList`-style inline
    `fetch` + bearer-token pattern on open and on control change;
  - renders one table: key column (agent names resolved from the
    store's `agents`; session key shows the id tail; day/backend keys
    verbatim), turns, cost (`$x.xxxx`, "—" when null), tokens
    (`toLocaleString()`), plus the `totals` footer row.
  - no charts, no store additions — all state is local to the dialog.
- Types for the response are hand-declared next to the component (the
  OpenAPI `contracts.ts` is generator-owned; regenerating it is not
  part of this change).

## 7. Verification

Backend (`tests/test_usage_tracking.py` + parser cases in the existing
harness test files):

- parser: Claude `result` with the probed `usage`/`modelUsage` shape →
  normalized `TokenUsage` (fresh-input semantics preserved, missing
  keys → 0, `model_usage` passthrough); Codex `turn.completed` →
  cached-subtraction mapping, clamping, `reasoning_tokens`.
- db: `add_turn_usage` + `summarize_usage` for each `group_by`, window
  filtering (since/until boundaries), agent/session filters, NULL cost
  summing, totals.
- capture: fake-CLI turn through the session manager writes a
  `turn_usage` row with the session's agent/backend; a failing record
  call doesn't fail the turn.
- routes: auth 401, 422 on bad `group_by`/dates, happy paths per
  grouping, empty-DB shape.
- research: `LeafResult.usage` summing + one job-level row on
  completion.

Frontend: `UsageDialog.test.tsx` (CredentialList-test pattern —
`vi.stubGlobal("fetch")`, real store via `setState`): renders rows +
totals, resolves agent names, switches group_by (asserts refetch URL),
"—" for null cost. Then `bun run test`, `npx tsc --noEmit`,
`bun run build`, e2e `:fast` bucket (the dialog is pure-UI; no new
`@llm` test — existing suites must stay green).

Update the CLAUDE.md / README / architecture.md test counts and the
suite tables when done.

## 8. Accepted trade-offs (explicit)

- **No per-model rows.** `model_usage` JSON is stored verbatim per row;
  per-model aggregation can be built later without a migration, at the
  cost of JSON parsing then. Committing to Claude's `modelUsage` key
  names today, from an unauthenticated probe, would be guessing.
- **Codex `cache_creation_tokens` is 0, not NULL.** Codex simply
  doesn't report it; 0 keeps SUMs trivially correct.
- **`agent_id` is capture-time denormalized.** If a session were ever
  re-owned, history reflects who spent it — which is what a limit
  ledger wants.
- **Job-granularity research rows** (one per job, stamped at
  completion) — see §4.

## 9. What this defers

- **`run_oneshot` consumption** (schedule NL parsing, research
  reasoning leaves): oneshot returns extracted text only; capturing it
  means parsing usage out of both backends' oneshot output — the Codex
  oneshot stream's usage shape is unverified, volumes are small, and
  research reasoning cost is *already* excluded from `research_jobs.cost`
  today. Wire it when limit-awareness needs it, with its own probe.
- **Historical backfill** — excluded by the task statement. Old turns
  have no token data anywhere (it was never persisted) and only partial
  cost on `messages.cost` with no timestamps; there is nothing sound to
  backfill from.
- **Charts, limit logic, failover** — the follow-up features this
  foundation exists for.
