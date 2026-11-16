# AgentBeats A2A Adapter — Tech Plan

2026-11-16 · implements docs/plans/agentbeats-phase1.md's "A2A 适配器" design
point. Owlery as a purple (competitor) agent on the AgentBeats platform's
Terminal Bench 2.0 green agent, over the real A2A protocol.

## 0. Why this exists

Phase 1's red line (carried over from the Terminal-Bench reconnaissance,
`agentbeats-recon.md`): the adapter must drive Owlery's **real** session/
harness engine end to end — no exam-only shell standing in for the product.
This doc records the protocol reconnaissance that makes that concrete, and
the resulting design.

## 1. Protocol reconnaissance (source-read, not README-read)

RDI-Foundation's own README for `terminal-bench-green` describes a
`POST /exec/{session_token}` HTTP API for purple agents. **That's stale.**
Reading the actual source (`src/agent.py`, pinned at
`harbor-framework`'s mirror, commit referenced in
`docs/plans/agentbeats-ten-tasks.json`) shows the real protocol is a pure
A2A message round-trip, `terminal-bench-shell-v1`:

```
green → purple (A2A message, text part is this JSON):
  {"kind": "task", "protocol": "terminal-bench-shell-v1", "instruction": "<task prompt>"}

purple → green (A2A response, same call):
  {"kind": "exec_request", "command": "...", "timeout": 30}
  — or —
  {"kind": "final", "output": "<summary>"}

green → purple (next A2A message, SAME context_id):
  {"kind": "exec_result", "exit_code": 0, "stdout": "...", "stderr": "..."}
  (repeats until purple sends "final")
```

Green owns the Docker sandbox directly (Harbor's `DockerEnvironment`) and
runs the command on its own side between A2A calls — purple never gets
filesystem/exec access to the sandbox except by round-tripping a decision.
This is why a lightweight mock can't substitute for a real Docker daemon on
the green side: there's no "exec API on purple" to fake around, the
sandbox simply isn't reachable from here except through a real green agent
instance. (Confirmed separately with Albus from the recon card's A2A-cost
estimate, delegation `ac0e25fcc024`.)

A2A plumbing itself: `a2a-sdk` (Python), exact version pinned to `0.3.20`
in `terminal-bench-green`'s own `uv.lock` — not a floor, the SDK's 1.x line
removed `a2a.server.apps` (the Starlette app builder this adapter uses), so
`pyproject.toml` pins it exact rather than `>=`.

## 2. Design

### 2.1 One real turn per A2A round

Each `execute()` call the a2a-sdk server dispatches is **one Owlery turn**:
the incoming protocol JSON becomes the user-message prompt into a real
`SessionManager.start_message`, and the manager waits for that turn to
fully conclude (the `result`/`error` broadcast event — the same
authoritative "turn is over" signal `server/delegations.py` already relies
on, which conveniently also carries the turn's real dollar cost) before
answering green. This guarantees no two turns are ever in flight on the
same session, and gives a free, accurate per-round cost figure for the
budget ledger.

### 2.2 The `tbexec` MCP tool — the only thing the model can act through

The model has no local shell for this task (the sandbox is on green's
side), so it needs a tool whose *calling* IS the decision, not a tool that
executes anything itself:

- `mcp__tbexec__run(command, timeout?)` — records `{"kind":"exec", ...}`
  as this turn's decision. Posts to
  `POST /api/sessions/{sid}/tbexec/decision` (new router,
  `server/routers/tbexec.py`), which calls
  `AgentBeatsExamManager.record_decision` — 409 if a decision was already
  recorded this turn (exactly one of `run`/`done` per turn).
- `mcp__tbexec__done(output)` — records `{"kind":"finish", ...}`.

Shaped exactly like `bg.py`/`ask.py`: a stdio FastMCP server, spawned as a
child of the `claude`/`codex` CLI, talking back to FastAPI over the
`OWLERY_API_BASE`/`OWLERY_AUTH_TOKEN`/`OWLERY_SESSION_ID` env contract
(`server/mcp_servers/tbexec.py`). Registered in
`server/harness/assembly.py`'s builtin module map but **not** added to
`DEFAULT_MCP_SERVERS` — only the dedicated exam agent enables it.

If the model never calls either tool before the turn concludes (ignores
the protocol), the manager falls back to the turn's plain assistant text
as an implicit `done` — better than silently dropping the task.

### 2.3 Tool isolation — denylist, not allowlist

Claude Code's *native* tools (Bash, Read, Write, Edit, ...) are separate
from Owlery's MCP add-ons and are not controlled by `mcp_servers` at all.
Left alone, the exam agent could call its native `Bash` and execute on
*this* machine instead of the remote sandbox — both a correctness bug and
a real safety hole (arbitrary code execution from a task description).
The exam agent's `tool_deny` is set to the full native toolset (Bash,
Read, Write, Edit, MultiEdit, NotebookEdit, Glob, Grep, LS, WebSearch,
WebFetch, NotebookRead, TodoWrite, Task, BashOutput, KillBash). A denylist,
not an allowlist: `server/harness/claude_code.py`'s own comment on its
research-leaf precedent is explicit that `--allowedTools` semantics are
ambiguous under `--dangerously-skip-permissions` (which this harness
always passes) — a denylist is unambiguous.

### 2.4 Exam agent isolation — fresh, no memory, no credential, hard-deleted

One A2A `context_id` (= one Terminal Bench 2.0 task trial) == one
throwaway Owlery agent + session, created lazily on the first message:

- Generic persona (`_EXAM_AGENT_PERSONA` in `server/agentbeats/manager.py`)
  — no production agent's identity, memory, or credential ever appears.
- `mcp_servers=["tbexec"]` only.
- No `credential_id` — Claude Code's own `os.environ.copy()` subprocess
  spawn (`server/harness/run.py`) picks up an ambient `ANTHROPIC_API_KEY`
  if the process env has one, with zero Owlery DB credential involved.
  This is also how the eventual AgentBeats Docker image stays
  secret-free: the key is injected as a runtime env var by amber's
  `config_schema` (§4), never baked into the image.
- A fresh `agent_id` means a fresh memory dir every time
  (`agent-collaboration.md` §5.10) — memory genuinely cannot leak between
  tasks, because there's no second session to leak *into*.
- Hard-deleted (`delete_session` + `delete_agent`, which also removes the
  memory dir) immediately after the terminal decision — not merely
  archived. These are one-shot, never reused.

### 2.5 Budget enforcement

Per `agentbeats-phase1.md`: $3 or 50 rounds per task, whichever comes
first (`TASK_COST_BUDGET_USD`, `TASK_ROUND_BUDGET` in `manager.py`). A
"round" is one A2A message exchange (one exec decision), not an internal
CLI tool-call loop. Checked *before* spawning a turn — once exceeded, the
manager returns `{"kind":"final","output":"(aborted: ...)"}` without
touching `SessionManager` again. A per-round wall-clock timeout
(`ROUND_TIMEOUT_SEC`, comfortably under the green agent's own 300s A2A
client timeout) force-interrupts a wedged turn rather than hanging the
whole evaluation.

### 2.6 Mounting — no separate process

The A2A endpoint is mounted at `/a2a` directly on Owlery's main FastAPI
app (`server/main.py`), not a separate standalone server. This is also why
the eventual AgentBeats Docker image needs no special-purpose shell: the
entrypoint is literally `uvicorn server.main:app`, and "the real stack" is
the whole product, not a slogan. **Must be registered before the "/" SPA
`StaticFiles` mount** (same ordering constraint the Feishu webhook route
already documents in `main.py`): Starlette commits to the first `Mount`
whose prefix matches a request path and never falls through to a later
one, so a Mount appended after a catch-all `Mount("/")` is permanently
unreachable.

## 3. What this explicitly defers (§10 discipline)

- **In-memory-only task state, no DB table.** Unlike `delegations.py`'s
  `delegation_runs`, an in-flight A2A exam task does not survive an Owlery
  restart. Acceptable for this phase (short-lived local validation runs,
  explicitly not a 24/7 externally-reachable submission target yet); would
  need a durable table before Phase 2's actual public submission.
- **`card_url` is `http://127.0.0.1:{port}` (+ `/a2a` in dev)**, computed by
  `a2a_mount_path_and_card_url` (`server/agentbeats/app.py`) — fine for
  local-only validation; a real AgentBeats submission needs this to reflect
  the deployed image's actual externally-reachable URL (an amber
  `config_schema`/env concern, not touched here since Phase 1 does zero
  public submission).
- **No streaming.** `AgentCapabilities(streaming=False)` — each round is
  a single request/response, matching the protocol's own round-trip
  shape; there is no intermediate progress to stream within a round.
- **Docker packaging (`deploy/agentbeats/`) is build-ready, not
  submission-ready.** It builds and runs locally; it is never pushed to a
  registry or registered with agentbeats.dev in this phase (hard red line,
  `agentbeats-phase1.md`'s "不做清单"). Not even locally verified to
  *build* — no Docker on this dev machine during Phase 1 (§5).
- **Mount path — resolved, not merely assumed.** §2.6 originally mounted
  unconditionally at `/a2a` to coexist with the rest of Owlery's product on
  one port, flagging as an open risk that `agent-template`'s reference
  purple agent mounts at root instead. Local ten-task validation (§5)
  turned that "unconfirmed" into a confirmed bug: the real
  `terminal-bench-green` reference agent's own A2A client (`a2a-sdk`
  httpx, no redirect-following) hard-fails on a bare `/a2a` request because
  Starlette's `Mount(path="/a2a")` 307-redirects it to `/a2a/`. Fix:
  `a2a_mount_path_and_card_url` (`server/agentbeats/app.py`) mounts at root
  `/` whenever `web/dist/` is absent — true for the real submission image,
  which never copies it in, so there's no SPA catch-all to coexist with.
  Dev checkouts (web/dist present) keep the `/a2a` sub-path; no real
  AgentBeats client ever talks to a dev checkout, so the redirect caveat
  there is moot. What's still genuinely unconfirmed (would need registering
  a real agent, still forbidden this phase): whether the platform's own
  slot-wiring always hands purple's `participants.agent` URL with a
  trailing slash, independent of what this fix does — root-mounting makes
  that moot too, since root never needs trailing-slash normalization.

## 4. Tests

- `tests/test_tbexec_mcp.py` — the MCP tool functions, httpx mocked
  (mirrors `test_ask_mcp.py`).
- `tests/test_tbexec_router.py` — the decision route, real app mounted,
  ASGI transport, `record_decision` monkeypatched (mirrors
  `test_bg_tasks.py`'s `api_client` fixture).
- `tests/test_agentbeats_manager.py` — `AgentBeatsExamManager` against a
  *real* `SessionManager`/`AgentManager`/in-memory DB, with
  `SessionManager._make_run` scripted to a canned `HarnessEvent` stream
  (mirrors `test_session_manager.py`'s own `ScriptedBackend` convention —
  only the CLI subprocess is faked, same split CLAUDE.md's fake-CLI
  philosophy draws one layer up). Covers the full two-round lifecycle +
  teardown + agent isolation + both budget caps + the no-decision fallback
  + a harness-error abort + decision-conflict handling.
- `tests/test_agentbeats_a2a_app.py` — the one true conformance test: the
  real a2a-sdk **client** (`A2ACardResolver`/`ClientFactory`, the exact
  classes `terminal-bench-green`'s own `messenger.py` uses) driving the
  mounted Starlette app over `httpx.ASGITransport`, no real network, no
  real green agent, no Docker. Proves the actual JSON-RPC envelope and
  agent card a real green agent would see, which the other three files
  don't reach.

None of these spend any model budget or touch Docker — they're what let
this module ship through the normal four-gate flow before the ten-task
local run (which does spend real budget) is attempted separately.

## 5. Local validation status

This dev machine had no Docker installed at the start of Phase 1
(`which docker` empty). Running the real `terminal-bench-green` locally —
and therefore the ten-task validation in `agentbeats-phase1.md` — requires
a real Docker daemon (confirmed by reading `agent.py`: green spins up
Harbor's `DockerEnvironment` directly, not optionally). See the task
card's comment thread for the resulting decision on whether/how that
environment setup proceeded, and the ten-task results themselves.
