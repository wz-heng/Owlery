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
  already: `codex_login` tests use a fake CLI; Feishu bridge tests use
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
- Feishu bridge e2e (own config, already isolated).
- Any product code changes beyond what the fake-CLI fixture needs.

### A fake `codex` — deferred until it has a consumer

There is a fake `claude` but **no fake `codex`**, and that asymmetry is
deliberate. Exactly one e2e test drives a codex turn (the codex chat
smoke, §2), and it is *supposed* to burn quota. A fake codex today
would be speculative infrastructure with zero consumers.

**The failure mode this leaves open.** On the claude side the fake makes
routing self-enforcing: an unmarked working dir gets canned output, so a
test cannot reach the real model by accident. Codex has no such gate, so
routing there is not controlled by the `.owlery-real-cli` marker at all —
a codex-backed turn resolves straight to the real binary. Any future
codex-backed test outside the `@llm` bucket would therefore burn real
subscription quota on every run, quietly. This is the same class of
silent misroute that let `agent-collaboration.spec.ts` sit broken: it
ran against a fake with no `ask_agent` op, and because `@llm` tests are
excluded from `test:e2e:fast`, nothing surfaced it.

**What guards it in the meantime.** A tripwire `codex` shim
(`web/e2e/fake-cli/codex`) sits on the same PATH. It execs the real
binary from a marked dir and otherwise refuses the turn and records the
breach; `global-teardown` fails the whole run if any breach was
recorded. That converts "silently burns quota" into a red run. It is a
tripwire, not a fake — it cannot serve a test that needs a *working*
canned codex turn.

**Trigger to build the real thing.** The first person who wants a
codex-backed test that must not burn quota. At that point the fake has
its second use case, and `fake_claude.py` is the template: same
directive protocol, same marker passthrough, emitting codex's own event
schema instead of claude's `stream-json`.

## 5. Sequencing

After `feat/usage-tracking` merges — both touch `web/e2e/` and
CLAUDE.md.
