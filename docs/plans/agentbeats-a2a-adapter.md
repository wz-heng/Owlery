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
- **Backstop for when there is no terminal decision**: the ten-task run
  (§5) confirmed, not just hypothesized, that green can abandon a task
  without ever sending purple another A2A message — its own Harbor-side
  exec call can hit *its* 300s cap and raise internally, moving on to the
  next task with nothing sent back here. `_reap_idle` (a periodic sweep,
  `IDLE_REAP_SEC`/`REAP_POLL_SEC`) tears down any unfinished record whose
  last incoming message is older than a threshold set well above the
  longest legitimate gap between two `handle_message` calls on the same
  context (`ROUND_TIMEOUT_SEC` while a round is in flight, or green's own
  exec-side wait once we've handed back a decision) — see its own comment
  for the margin, and `record.last_activity_at`'s refresh-before-any-await
  placement in `handle_message` for why it can't race a round that's
  merely slow.

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

### 2.6 Mounting — no separate process, and a path that depends on the SPA

The A2A endpoint is mounted directly on Owlery's main FastAPI app
(`server/main.py`), not a separate standalone server. This is also why the
eventual AgentBeats Docker image needs no special-purpose shell: the
entrypoint is literally `uvicorn server.main:app`, and "the real stack" is
the whole product, not a slogan.

The mount *path* is conditional, computed by
`a2a_mount_path_and_card_url` (`server/agentbeats/app.py`): root `/` when
`web/dist/` is absent (true for the real submission image, which never
copies it in), `/a2a` when present (dev checkouts, where `/` is already
claimed by the SPA). This isn't cosmetic — it's a confirmed bug fix, not a
preference: the real `terminal-bench-green` reference agent's own
a2a-sdk client doesn't follow redirects, and Starlette's
`Mount(path="/a2a")` 307-redirects a bare `/a2a` request (no trailing
slash) to `/a2a/`, which that client then hard-fails on. Root-mounting
needs no such trailing-slash normalization, so it sidesteps the problem
entirely rather than depending on every caller happening to include the
slash.

Registration **order** matters twice over, for the same underlying
reason — Starlette commits to the first route (`Route` or `Mount`) whose
prefix matches a request path and never falls through to a later one:
- Must be registered **after every explicit route** above it
  (`/health`, `/api/backends`, the various `include_router` calls): when
  root-mounted, its path regex matches *everything*, so if it were
  registered first it would swallow those routes too (404 inside the A2A
  sub-app, which has none of them) — caught by the full backend suite,
  not by hand.
- Must be registered **before the "/" SPA `StaticFiles` catch-all**
  (same ordering constraint the Feishu webhook route already documents in
  `main.py`): appending it after a catch-all `Mount("/")` would make it
  permanently unreachable.

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
- **Docker packaging (`deploy/agentbeats/`) is build-verified, not
  submission-ready.** §5 originally flagged this as not even locally
  verified to build (no Docker on the dev machine at the time). Now built
  successfully (`docker build -f deploy/agentbeats/Dockerfile`, image
  `owlery-agentbeats:local`, 1.27GB). Docker Scout's static secret scanner
  flagged exactly `ENV ANTHROPIC_API_KEY` and `ENV OWLERY_AUTH_TOKEN` by
  name (it flags any env var matching a known secret-name pattern,
  regardless of whether a real value is present — not an exhaustive
  secret audit, and not evidence that no *other* secret exists anywhere
  in the image). What it does support, read against the `Dockerfile`
  itself (`ENV ANTHROPIC_API_KEY=""`): the flagged key is explicitly
  empty, consistent with the design's claim that it's injected at
  container runtime, never baked in. The other flagged value,
  `OWLERY_AUTH_TOKEN=agentbeats-local`, is non-empty but is the documented
  loopback-only shared secret between this process and its own spawned
  MCP subprocesses (Dockerfile comment, §2.2) — not a real credential.
  The image is never pushed to a registry or registered with
  agentbeats.dev in this phase (hard red line, `agentbeats-phase1.md`'s
  "不做清单"), and the ten-task validation run itself used the native
  (non-containerized) server instead — this image's own Claude Code CLI
  has no OAuth session to inherit (the host's is in the macOS Keychain,
  not reachable from inside a Linux container), so running a real task
  through it would need a real `ANTHROPIC_API_KEY` (metered billing) that
  this phase doesn't have provisioned. Build-verified; task-run-verified
  only via the native path.
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

This dev machine had no Docker at the start of Phase 1 (`which docker`
empty); running the real `terminal-bench-green` locally — and therefore
the ten-task validation in `agentbeats-phase1.md` — needs one (confirmed
by reading `agent.py`: green spins up Harbor's `DockerEnvironment`
directly, not optionally). `brew install colima docker docker-compose`,
authorized and capped by Albus per the task card's comment thread
(≤4 CPU/≤6G memory/40G disk for the colima VM); stopped (`colima stop`)
and all images/build-cache pruned (`docker system prune -a -f --volumes`)
once the ten-task run below finished — not uninstalled, so `colima start`
picks back up without reinstalling anything.

Getting the real green agent running against this adapter needed two
patches to its own `Dockerfile` (RDI-Foundation/terminal-bench-green
@ `dcaba23f`, cloned disposably — not vendored into this repo):

1. **`docker.io` instead of `get.docker.com`.** The upstream Dockerfile
   installs Docker via `curl -fsSL https://get.docker.com | sh`, which
   TLS-resets from inside the build container's bridged network on this
   network path (reproducible 5/5, direct from the colima VM itself
   works fine over the same domain — the break is specific to the
   container's bridged path, not a generic outage). Swap for Debian
   bookworm's own `docker.io` package, which is sufficient for a pure
   DOOD client (no dockerd ever starts inside green's container; it only
   ever talks to the host daemon over the mounted socket):
   ```
   RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates docker.io git && \
       rm -rf /var/lib/apt/lists/*
   ```
2. **A separately-fetched `docker compose` v2 CLI plugin.** Debian's
   `docker.io` (20.10.24) predates the v2 compose plugin architecture, but
   Harbor's `DockerEnvironment._run_docker_compose_command` hardcodes v2
   syntax (`docker compose --project-name ...`) with no v1 fallback — so
   `docker compose version` fails outright without this:
   ```
   RUN mkdir -p /usr/local/lib/docker/cli-plugins && \
       curl -fSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
         https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-aarch64 && \
       chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
   ```
   (`-aarch64` because this host is Apple Silicon; use `-x86_64` on an
   Intel/cloud host.)

This is a **build-time-only** proxy need, not a runtime one. The patched
Dockerfile's own `git clone` (pre-baking the task repo) and `curl`
(fetching the compose plugin binary) steps need outbound internet from
*inside* the build container on this network, which only worked through
this host's local proxy, explicitly, as `--build-arg`s — plain
`docker build` with no proxy configuration got `Could not connect`/
timeout failures from inside the container even though the host itself
reaches the same URLs fine unproxied:
```
docker build --add-host=host.docker.internal:host-gateway \
  --build-arg http_proxy=http://host.docker.internal:7890 \
  --build-arg https_proxy=http://host.docker.internal:7890 \
  -t terminal-bench-green:latest .
```
At **runtime**, green needs no proxy and no general internet access at
all: its own task-repo fetch (`agent.py`'s `_ensure_task_repo`) is a
local `shutil.copytree` from the pre-baked path, not a network call, and
the only two things it does reach over the network are the DOOD-mounted
Docker socket and this adapter — both purely local, wired with
`--add-host`/volume mounts, no proxy involved:
```
docker run -d --name tbgreen -p 9009:9009 \
  --add-host=host.docker.internal:host-gateway \
  --group-add <host docker.sock gid, via `colima ssh -- getent group docker`> \
  -e WORKSPACE=/workspace -e DOCKER_HOST=unix:///var/run/docker.sock \
  -v /var/run/docker.sock:/var/run/docker.sock -v /workspace:/workspace \
  terminal-bench-green:latest --host 0.0.0.0 --port 9009
```
The `--group-add` is needed because the image's own `Dockerfile` creates
its `docker` group at a fixed gid (999) that won't generally match the
*host's* docker.sock gid once that socket is bind-mounted in (DOOD, not a
locally-created socket) — without it the non-root `agent` user inside the
container can't open the socket at all.

`scripts/agentbeats_drive_green.py` is the AgentBeats-gateway stand-in
used to actually drive a run once both sides are up — see its own
docstring for the full sequence including starting this adapter itself.
The resulting ten-task scores, cost ledger, and extrapolation are on the
task card's comment thread, not duplicated here (that is genuinely
run-specific, not design documentation).
