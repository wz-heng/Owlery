#!/bin/sh
# Task preflight — run before starting any pipeline task (~30 s).
# Verifies the environment an agent task depends on, so environment
# problems surface at task start with evidence, not mid-task as
# misleading failures (e.g. the 2026-07-08 keychain/"Not logged in"
# incident). Exit 0 = all clear; nonzero = fix environment first.
set -u

fail=0
say() { printf '%s\n' "$*"; }
check() { # check <label> <ok|FAIL> [detail]
  if [ "$2" = ok ]; then say "  ok    $1${3:+ — $3}"; else say "  FAIL  $1${3:+ — $3}"; fail=1; fi
}

say "== preflight $(date '+%F %T') =="

# 1. CLI auth probes (headless, the same way the server spawns them).
for cli in claude codex; do
  bin=$(command -v "$cli" 2>/dev/null)
  if [ -z "$bin" ]; then check "$cli on PATH" FAIL "not found"; continue; fi
  case "$cli" in
    claude) out=$("$bin" --print --output-format json "Reply with exactly: OK" 2>&1 | tail -1);;
    codex)  out=$("$bin" exec --json "Reply with exactly: OK" 2>&1 | tail -1);;
  esac
  case "$out" in
    *'"is_error":false'*|*turn.completed*) check "$cli auth probe" ok "$bin";;
    *) check "$cli auth probe" FAIL "$(printf '%s' "$out" | cut -c1-120)";;
  esac
done

# 2. Proxy env — codex needs the proxy; MCP callbacks must bypass it.
[ -n "${https_proxy:-}${HTTPS_PROXY:-}" ] \
  && check "https_proxy set" ok \
  || check "https_proxy set" FAIL "codex needs it for OpenAI region check"
case "${no_proxy:-}${NO_PROXY:-}" in
  *127.0.0.1*|*localhost*) check "no_proxy covers localhost" ok;;
  *) check "no_proxy covers localhost" FAIL "MCP callbacks to 127.0.0.1 will be hijacked (502)";;
esac

# 3. Server reachable (skip silently if OCTOPUS_API_BASE unset and :8000 down).
base="${OCTOPUS_API_BASE:-http://127.0.0.1:8000}"
if curl -sf -o /dev/null --max-time 5 "$base/docs" 2>/dev/null; then
  check "server at $base" ok
else
  check "server at $base" FAIL "octopus serve not reachable"
fi

# 4. Git state — on a work branch with a clean-enough tree.
branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')
[ "$branch" != main ] && check "on work branch" ok "$branch" \
  || say "  warn  on main — create a feat/fix/docs branch before changing code"

say "== preflight done (fail=$fail) =="
exit "$fail"
