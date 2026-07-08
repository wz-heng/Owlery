# E2E slim — shrink the @llm bucket, put CLAUDE.md on a diet

## 1. What & why

The e2e suite is structurally healthy (35 pure-UI tests, ~16 s, free) but
the `@llm` bucket is bloated: 32 tests each drive a full real `claude` /
`codex` turn. Their unique value — proving the UI → WS → server → CLI
wiring works against a real model — needs 2-3 tests, not 32. Feature
logic (delegation chains, /schedule parsing, …) is already covered by
pytest real-CLI tests one layer down.

Real-LLM tests are also environment-sensitive (proxy, keychain, region
blocks, CLI upgrades). Incident 2026-07-08: a transient macOS keychain
denial right after a `claude` CLI upgrade made an @llm-adjacent probe
report "Not logged in", and an agent burned an afternoon of tokens
misdiagnosing a healthy login. False alarms from env-sensitive tests
cost far more than the quota they burn.

## 2. Target shape

- **Keep 2-3 `@llm` smoke tests** (the only quota burners): one claude
  chat roundtrip, one codex chat roundtrip, one real delegation
  (`ask_agent` 2-hop). They run once per task per the tiered policy in
  CLAUDE.md.
- **Convert the remaining ~29 to fake-CLI e2e**: a fake `claude` /
  `codex` binary emitting canned stream-json, injected via the PATH
  override the server already supports — the real spawn path is still
  exercised, only the model is canned. The codebase has this pattern
  already: `codex_login` tests use a fake CLI; telegram bridge tests use
  a fake API server. Extend it, don't invent a new one.
- **Zero assertion loss**: every converted test keeps its assertions
  verbatim; only the model responses become deterministic fixtures.

Expected outcome: full e2e drops from ~3.5 min + quota + flaky to
~1 min, free, deterministic. The tiered run policy in CLAUDE.md gets its
numbers updated accordingly.

## 3. CLAUDE.md diet (same task)

The test-coverage table has grown paragraph-sized cells; every agent
session pays those tokens at startup. Compress each suite cell to
one-line-per-area bullets: counts + area names + pointers to the
relevant `docs/plans/*.md` for detail. Target: CLAUDE.md under ~150
lines with no information an agent actually needs removed — deep detail
lives in the plan docs it points to.

## 4. Out of scope

- Backend pytest real-CLI tests (they stay; they're the cheap layer that
  covers real-CLI behavior).
- Telegram bridge e2e (own config, already isolated).
- Any product code changes beyond what the fake-CLI fixture needs.

## 5. Sequencing

After `feat/usage-tracking` merges — both touch `web/e2e/` and
CLAUDE.md.
