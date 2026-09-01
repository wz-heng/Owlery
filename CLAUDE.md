# Owlery Development Rules

## Do It Right The First Time (no MVPs, no future-polish)

**If we choose to do something, we do it perfectly — right now, in
this session.** No "minimal fix", no "MVP for now", no "we'll polish
this later". No deferral of cleanup to a "follow-up" item.

This rule is non-negotiable. Specifically that means:

- Never ship a half-done implementation and document the rest as
  "future work". If the full thing isn't worth doing right now, then
  don't start it at all.
- Never park the cleaner version in a "deferred work" note
  *instead* of doing it. Genuine deferrals (work that needs a real
  second use case, an external dep, or a user decision) belong in
  the relevant plan doc's §10 "What this defers"; nothing else is a
  legitimate place to stash "felt too long".
- Never add a comment like `# TODO: handle X properly later` or `#
  HACK: works for now`. If `X` matters, handle it in this change.
  If it doesn't matter, delete the comment.
- When the user asks "fix this", interpret it as "fix it the way a
  careful engineer with infinite time would" — not "ship the
  smallest patch that no longer crashes".
- "MVP" is not a status the user has to accept. There is no future
  in which a later session will go back and polish; in the AI era
  we have the bandwidth to do it right *now*, here, in one go.

This rule exists because past sessions repeatedly took the shortcut
and then had to be told to go back and do the real thing. Skip the
shortcut. Do the real thing the first time.

## Retrospective Discipline (Experience Consolidation)

Owlery mechanizes "a retrospective must happen" for Task Board work
(`docs/plans/experience-consolidation.md`): a worker's `complete` on a
**non-clean-pass** run (a retry, a prior blocked/failed/interrupted attempt
on the same task, or `verdict="fail"`) is refused by the `tasks` MCP server
until it has called `reflect` — a self-authored triage into exactly one of
three channels: your own agent memory (a judgment call specific to you),
a CLAUDE.md nomination (a rule everyone working in this repo should know —
edit this file and land it through the normal branch+PR flow, same as any
other change), or a skill candidate (`skills` MCP server's `propose` — a
repeatable multi-step process, reviewed by a human via the pending-candidate
queue before it ever lands on disk). `reflect` also accepts a bare
`nothing_note` when reflection genuinely finds nothing new. A clean first
pass skips the gate entirely — do not add one.

**This mechanism has no terminal hook to attach to for a standing agent**
(there is no Task Board run wrapping an ordinary chat session). If you are
a standing agent (not a one-shot Task Board worker) and you just walked a
multi-step external workflow to completion for the first time — with any
real friction along the way — stop before ending your turn and ask
yourself: *will I walk this path again?* If yes, codify it on the spot,
right now, in this same turn: a skill candidate if it is a repeatable
procedure, or a note in your own memory if it is a judgment call specific
to you. Do not wait to be asked, and do not defer it to "later" — later is
a fresh session with no memory of the friction you just hit.

**Delegations do not have this gate** — only Task Board `complete` does.
`experience-consolidation.md` §3.2 permits narrowing to Task Board alone
when a delegation-side gate isn't proportionate, provided the narrowing is
written down; this is that note. A delegation's terminal turn is delivered
by `_inject_terminal` (`server/delegations.py`), which has no equivalent
"was this a clean pass" signal to gate on today. If delegation retries
start showing the same repeated-mistake pattern Task Board attempts did,
revisit this rather than bolting a gate on blind.

## After Every Code Change

Run all four. They total well under two minutes — there is no cheap
tier to fall back to, so don't skip one on the grounds that a change
"only touched the frontend".

1. **Backend unit**: `.venv/bin/pytest tests/ -v` (1426)
2. **Frontend unit**: `cd web && bun run test` (279)
3. **TypeScript**: `cd web && npx tsc --noEmit`
4. **E2E**: `cd web && bun run test:e2e` (76, ~2 min, Playwright auto-starts
   servers)

**Zero test failures are acceptable.** All tests must pass before
committing. If a test fails, investigate and fix it — never ignore, skip,
or dismiss a failure as "flaky" or "pre-existing".

**Keep test output out of your context**: redirect to a file (e.g.
`.venv/bin/pytest tests/ > /tmp/pytest.log 2>&1`) and read back only the
summary line and any failure sections — never load a full passing run into
the conversation.

### Which tests hit a real model

Almost none, by design (`docs/plans/e2e-slim.md`). A fake CLI on PATH emits
canned output through the real spawn → stream-json → MCP path; only the
model is canned.

- **E2E**: 3 of the 76 burn real quota — claude chat, codex chat, 2-hop
  delegation. They opt in by marking their working dir via `realCliDir()`;
  everything else gets the fake. `bun run test:e2e:fast` (73, ~1.4 min) skips
  them via `--grep-invert @llm` — fine while iterating, but run the full
  suite before committing.
- **Backend**: `test_*_real.py` auto-skip unless their CLI is on PATH.
  `claude` for `test_backend_claude_code_real` / `test_schedule_ai_real` /
  `test_showme_ai_real` / most of `test_delegations_real`; `codex` for
  `test_backend_codex_real` / `test_codex_login_real`; **both** for
  `test_agent_memory_real` and the claude→codex delegation case. They
  *error* rather than skip if a binary is missing — see Conventions.
- Feishu bridge e2e has its own config: `bun run test:e2e:bridge` (6,
  `playwright.bridge.config.ts`). It carries the SAME isolation guardrails as
  the main suite — its own temp DB/home/agents dirs, the fake `claude` + codex
  tripwire on PATH, `reuseExistingServer: false`, a private port (8766) with
  its own `OWLERY_AUTH_TOKEN`, and `OWLERY_LEGACY_HOME_DIR=""`. Transport is
  webhook (§3.1): the spec POSTs Feishu-shaped events at `/feishu/webhook` and
  asserts outbound via the fake Feishu server (`fake-feishu-server.mjs`). The
  bridge sets `trust_env_proxy=False` for its loopback domain and the config
  also exports `no_proxy` — so an ambient Clash proxy can't swallow the
  outbound (feishu-bridge.md §4.3).

Two traps. The `.owlery-real-cli` marker is a property of a *directory* and
all specs share one backend, so never drop it in a shared dir (`/tmp`) —
you'd route much of the suite to the real CLIs. And there is no fake
`codex`, only a tripwire shim that fails the run if a codex turn spawns the
real binary from an unmarked dir; if you need a canned codex turn, read
`e2e-slim.md` §4 first.

A third trap is environmental, not code: if the shell has `http_proxy` /
`https_proxy` set (e.g. a system-wide Clash proxy) without a matching
`no_proxy` exemption for `127.0.0.1`/`localhost`, five e2e specs go red —
`handoff-pull.spec.ts` (its `--server` flag and the webhook notifier both
intentionally support a *remote* target, so Owlery can't just force
`trust_env=False` there) and the webhook-notifier idle test in
`new-features.spec.ts`, both of which stand up a throwaway loopback HTTP
listener that the proxy swallows. Export
`no_proxy=127.0.0.1,localhost NO_PROXY=127.0.0.1,localhost` before running
`test:e2e` on such a box — that's the fix, not touching the product code.
Owlery's own MCP-callback servers (`server/mcp_servers/*.py`,
`connectors/_shared.py`) are a different case: their target is always a
hardcoded `127.0.0.1:{port}` (`server/harness/assembly.py`), so those calls
always pass `trust_env=False` unconditionally rather than relying on the
environment.

## Test Coverage

What each suite covers; detail lives in the linked plan docs.

**Backend unit** (pytest, 1426) — config, models, session manager, REST API,
DB persistence (credential split, refresh-error codes), JSONL parser/writer,
CLI handoff/pull, import API, schedules CRUD + scheduler (interval + cron),
NL `/schedule` parsing, Feishu bridge (fail-closed allowlist, card-value
validation, one-time nonce, per-chat verbosity, `/sessions` picker), tunnel,
OAuth registry, agents • harness layer
(`harness-layer.md`) • Codex in-app login (`codex-backend.md`) • connectors
(`connectors.md`) • agent memory (`memory.md`) • delegation
(`agent-collaboration.md`) • usage tracking (`usage-tracking.md`) •
Octopus→Owlery migration (`rename-owlery.md` §3) • durable Task Board and
Git-worktree delivery state/CAS/recovery (`task-board.md`,
`task-git-delivery.md`).

**Frontend unit** (vitest, 279) — zustand store, `useWebSocket`, BgTaskChip,
FileViewerDialog, SlashCommandMenu, delegation cards, fork dialog +
deferred-fork helper, CredentialList, ResearchCard, UsageDialog, `readStored`
localStorage rename migration, Task Board state/event reconciliation and Git
delivery controls.

**E2E** (Playwright, 76) — login, session CRUD, chat (send / Enter /
disabled-while-running / AskUserQuestion / resume), WS reconnect, mobile
layout, CLI handoff/pull + roundtrip, schedules, archived sessions, message
queue + Esc interrupt, virtualized chat, OAuth + Codex device-code sign-in,
credential override, agents rail/settings, connectors, `/research`,
`/rewind` + deferred fork, usage page, cross-turn `mcp__bg__run` + spill
pointer, `/showme`, Task Board worker completion, and Git-worktree
accept/commit/push/teardown. Plus 6 Feishu-bridge tests under their own config.

## Project Structure

- `server/` — FastAPI backend: `cli.py` (`serve`, `handoff`, `pull`),
  `database.py` (SQLite), `scheduler.py` (APScheduler), `jsonl_parser.py` /
  `jsonl_writer.py` (Claude Code JSONL codec), `fork_helpers.py` (`/rewind`
  tree-rewind — `session-rewind.md`, `session-fork.md`)
- `server/routers/` — REST + WS routers (`sessions`, `schedules`, `agents`,
  `credentials`, `connectors`, `delegations`, `research`, `usage`, `ws`)
- `server/harness/` — the single boundary for all model/runtime interaction:
  one `Harness` + one `HarnessRun` engine driven by a per-backend
  `RuntimeProfile` value, no per-framework subclasses (`harness-layer.md`)
- `server/delegations.py` + `server/mcp_servers/ask_agent.py` — agent-to-agent
  delegation; the delegation id IS the child session id
  (`agent-collaboration.md`)
- `server/agent_manager.py` / `server/agent_memory.py` — agent CRUD; per-agent
  native memory dir shared by both harnesses, decoupled from their config/auth
  dirs (`memory.md`)
- `server/research/` + `server/mcp_servers/research.py` — deep research
  (`native-deep-research.md`)
- `server/connectors/` + `connector_manager.py` + `mcp_servers/connectors/` —
  connector framework, business logic, per-kind stdio MCP servers
  (`connectors.md`)
- `server/bridges/` — messaging platforms (Feishu, `feishu-bridge.md`); a chat
  binds to an agent with a sticky session and a per-chat `verbose` flag
- `server/legacy_rename.py` — one-shot Octopus→Owlery first-boot migration;
  idempotent, marker-gated, disabled by `OWLERY_LEGACY_HOME_DIR=""`, which
  tests and e2e set since they boot the real lifespan against the real `$HOME`
  (`rename-owlery.md` §3)
- `web/` — React frontend (Vite + TS); `tests/` — pytest;
  `web/src/**/*.test.ts` — vitest, colocated; `web/e2e/` — Playwright

## Commands

> **Frontend gotcha**: the backend serves `web/dist/` (the built SPA),
> not `web/src/`. Any source change needs `cd web && bun run build`
> before `owlery serve` / `uvicorn server.main:app` users will see it.
> For live HMR, run `cd web && bun dev` and hit the dev server's port
> (5173) instead of the backend.

```bash
# Backend
.venv/bin/pytest tests/ -v              # run backend tests
.venv/bin/uvicorn server.main:app       # start server (serves web/dist/)

# Frontend
cd web && bun run test                  # run frontend unit tests
cd web && bun run build                 # typecheck + build (refreshes web/dist/)
cd web && bun dev                       # live dev server on :5173

# E2E (Playwright)
cd web && bun run test:e2e              # full suite (headless)
cd web && bun run test:e2e:fast         # skip the 3 real-model smokes
cd web && bun run test:e2e:ui           # run e2e tests with Playwright UI
cd web && npx playwright test --reporter=list  # verbose output
```

## Conventions

- Backend uses Python 3.12+, type hints, async/await
- Frontend uses React 19, TypeScript strict mode, zustand for state
- Use `useSessionStore.getState()` (not hook selectors) inside callbacks/effects that mutate store to avoid re-render loops
- The JS toolchain (`bun`, `node`, `npm`, `npx`) and `codex` install locations vary by machine (nvm under `~/.nvm/versions/node/*/bin`, or homebrew under `/opt/homebrew/bin` which is already on the default PATH). Confirm with `which bun` / `which codex`; only if a tool is missing from PATH do you need to manually prepend its bin dir (e.g. `export PATH="$HOME/.nvm/versions/node/<ver>/bin:$PATH"`). The real-CLI tests (`test_backend_codex_real.py` etc.) need `codex` resolvable the same way (otherwise they error rather than skip)
