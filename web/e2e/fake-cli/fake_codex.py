#!/usr/bin/env python3
"""Tripwire `codex` shim for the e2e suite (docs/plans/e2e-slim.md §4).

This is deliberately NOT a fake codex: there is no canned model, no
`stream-json` emitter, no MCP client. It is a gate. A codex turn either

  * runs the REAL `codex` — when the session's working dir carries the
    `.owlery-real-cli` marker, i.e. it opted in and expects to burn quota; or
  * fails the turn loudly, and records the breach for `global-teardown` —
    when it did not.

Why this exists. The fake `claude` makes the claude side self-enforcing: an
unmarked dir gets canned output, so a test can't reach the real model by
accident. Codex has no fake, so without this shim an unmarked codex-backed
turn would resolve straight to the real binary and silently burn subscription
quota — the exact silent-misroute class that let `agent-collaboration.spec.ts`
sit broken (it ran against a fake with no `ask_agent` op, outside the fast
bucket, so nothing surfaced it). Silent is the problem; this makes it red.

When someone wants a codex-backed test that does NOT burn quota, this shim is
their signal that a real fake `codex` now has a consumer and is worth building
(the second-use-case bar). Until then it doesn't get built.

`is_available()` only checks that the binary resolves on PATH, so this shim
keeps `/api/backends` reporting codex as available — the create-session
harness selector still renders, as the codex smoke's assertions require.

That cuts both ways, and it is why `/api/backends` must NOT be used to decide
whether the codex smoke can run. This shim satisfies the PATH probe on every
host, including one with no real `codex` installed, so the backend reports
codex available there too; the smoke would then not skip, and would die at
`exec_real_codex()` (exit 127) instead. A spec deciding whether the real binary
exists has to look for the real binary — see `realCodexInstalled()` in
`web/e2e/fake-cli.ts`, which resolves it the way this shim does, skipping
its own dir.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys

# Shared with `fake_claude.py`: a working dir opts into the real binary.
_REAL_CLI_MARKER = ".owlery-real-cli"

# Breaches land here for `global-teardown` to read. Deliberately outside
# E2E_FAKE_STATE_DIR: teardown deletes that tree, which would eat the evidence
# and turn a breach into a silent pass (this bit us once while auditing).
_TRIPWIRE_LOG_ENV = "OWLERY_FAKE_TRIPWIRE_LOG"


def _real_cli_requested() -> bool:
    return (pathlib.Path.cwd() / _REAL_CLI_MARKER).exists()


def _record_breach() -> None:
    log = os.environ.get(_TRIPWIRE_LOG_ENV)
    if not log:
        return
    try:
        with open(log, "a") as fh:
            fh.write(f"{os.getcwd()}\n")
    except OSError:
        pass  # the stderr message below still fails the turn


def exec_real_codex() -> int:
    """Replace this process with the real `codex`, argv untouched. Our own dir
    is stripped from PATH first, or `which` would resolve back to this shim."""
    here = str(pathlib.Path(__file__).resolve().parent)
    path = os.pathsep.join(
        d
        for d in os.environ.get("PATH", "").split(os.pathsep)
        if d and pathlib.Path(d).resolve(strict=False) != pathlib.Path(here)
    )
    real = shutil.which("codex", path=path)
    if real is None:
        sys.stderr.write(
            f"fake-codex: {_REAL_CLI_MARKER} present but no real `codex` on PATH\n"
        )
        return 127
    os.execv(real, ["codex", *sys.argv[1:]])


def main() -> int:
    if _real_cli_requested():
        return exec_real_codex()

    _record_breach()
    sys.stderr.write(
        "fake-codex: refusing to spawn the REAL `codex` from an unmarked "
        f"working dir ({os.getcwd()}).\n"
        "\n"
        "A codex-backed e2e turn reaches the real CLI and burns real "
        "subscription quota. There is no fake `codex`, so the only thing "
        "standing between a new codex test and silent quota burn is this "
        "refusal.\n"
        "\n"
        "If this turn SHOULD burn quota, it belongs in the @llm bucket: mark "
        "its working dir with realCliDir() (see web/e2e/fake-cli.ts).\n"
        "If it should NOT, you need a fake `codex` — see docs/plans/"
        "e2e-slim.md §4; you are the second use case that justifies building "
        "it.\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
