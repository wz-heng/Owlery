#!/usr/bin/env python3
"""Fake `claude` CLI for the deterministic e2e suite (docs/plans/e2e-slim.md).

Injected by prepending `web/e2e/fake-cli/` to the e2e backend's PATH, so
`HarnessRun.prepare_spawn` resolves *this* as the `claude` binary. Everything
downstream of the model is real: the server spawns a real subprocess, renders
the real argv, parses real `stream-json` off stdout, and this process really
speaks MCP over stdio to the real `bg` / `ask` servers. Only the model is
canned.

Two invocation shapes, both rendered by `server/harness/claude_code.py`:

  turn     `claude --print --output-format=stream-json --verbose … -- <prompt>`
  one-shot `claude --print --output-format=json … -- <prompt>`

What the turn does is driven by a directive embedded in the prompt:

    <<fake:[{"t":"text","v":"hi"}, {"t":"bash","cmd":"sleep 4"}]>>

With no directive it emits a plain text reply — enough for every test that
only needs "a turn happened" (a result badge, a webhook, a JSONL export).

Ops:
  text        emit an assistant text block
  bash        emit a Bash tool_use, sleep, emit the tool_result
  ask         call `mcp__ask__user` over MCP and echo the answer
  bg          call `mcp__bg__run` over MCP and end the turn
  task_complete call `mcp__tasks__complete` over MCP for the owning run
  task_block    call `mcp__tasks__block` over MCP for the owning run
  task_reflect  call `mcp__tasks__reflect` over MCP (experience-consolidation.md §3.2/§3.3)
  skill_propose call `mcp__skills__propose` over MCP (experience-consolidation.md §3.4)
  invoke_skill  emit a native `Skill` tool_use (no MCP call) to exercise use_count tracking
  write_file    write a relative file inside the current worker workspace
  remember    persist a word into this session's transcript
  recall      read that word back out of the transcript
  on_bg       rule: how to answer the injected `[bg-task-result]` turn

`remember`/`recall` and the `on_bg` rules persist to a per-session state file
keyed by the session id, which the server round-trips through `--resume`. That
keeps the resume assertion honest: a `recall` turn that was never resumed has
no state file to read, so it answers "I don't remember" and the test fails —
exactly as it would if `--resume` regressed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib
import re
import shutil
import sys
import time
import uuid

# A session whose working dir holds this marker is served by the REAL `claude`
# binary: the fake execs it, argv untouched. That's how the handful of @llm
# smoke tests keep burning real quota while sharing one server with the fakes
# (PATH is per-process, so it can't discriminate). A delegation child inherits
# its parent's working dir, so a real 2-hop chain needs no model cooperation.
#
# Shared with the tripwire `codex` shim (fake_codex.py), which reads the same
# marker. Never drop it in a shared dir — see `realCliDir` in fake-cli.ts.
_REAL_CLI_MARKER = ".owlery-real-cli"

# A turn's directive. Embedded in the prompt so a Playwright spec can script
# one turn without a side channel (env is per-server, not per-test).
_DIRECTIVE_RE = re.compile(r"<<fake:(.*?)>>", re.DOTALL)

# The absolute path `large_prompts.spill_if_large` puts in its pointer prompt.
_SPILL_PATH_RE = re.compile(r"saved at (\S+\.txt)")

# Deliberately-invalid credentials the credential-override spec attaches to a
# session. Seeing one proves the env-var injection reached the CLI — which is
# the whole claim of that test — so we fail the turn the way the real CLI does.
_BOGUS_KEY_PREFIX = "sk-ant-bogus"

# The reasoning leaf of `/research` goes through the one-shot path. Real scope
# calls take seconds; returning instantly would let a job finish before the
# research spec clicks Cancel. Sleep long enough that "running" is observable.
_ONESHOT_DELAY_SECONDS = 6.0


def _state_dir() -> pathlib.Path:
    d = pathlib.Path(
        os.environ.get("OWLERY_FAKE_STATE_DIR") or "/tmp/owlery-fake-cli-state"
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


def _limit_flag_path(prompt: str) -> pathlib.Path:
    """Where the `limit` op records "this prompt has already been limited once".

    Keyed on the PROMPT, not the session id: the no-output resume mode re-runs
    the original prompt and deliberately discards the failed attempt's session
    id (harness-transient-retry.md §4), so the id is not stable across the park.
    The prompt is — and it's what distinguishes concurrent specs from each other.
    """
    digest = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    return _state_dir() / f"limit-{digest}.flag"


def _load_state(session_id: str) -> dict:
    path = _state_dir() / f"{session_id}.json"
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"remember": None, "rules": {}}


def _save_state(session_id: str, state: dict) -> None:
    (_state_dir() / f"{session_id}.json").write_text(json.dumps(state))


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _emit_text(text: str) -> None:
    _emit({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    })


def _emit_tool_use(tool_use_id: str, name: str, tool_input: dict) -> None:
    _emit({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}
            ],
        },
    })


def _emit_tool_result(tool_use_id: str, content: str, is_error: bool = False) -> None:
    _emit({
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                    "is_error": is_error,
                }
            ],
        },
    })


def _sleep(seconds: float) -> None:
    """Sleep in slices so a SIGTERM from `HarnessRun.stop()` lands promptly —
    the Esc-interrupt spec depends on the process dying inside its budget."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(min(0.1, deadline - time.monotonic()))


# --------------------------------------------------------------------------- #
# argv
# --------------------------------------------------------------------------- #


# Flags whose value is a separate argv element. Their values must be skipped
# rather than scanned: `--append-system-prompt`'s value is a multi-KB blob that
# could otherwise be mistaken for a flag.
_VALUE_FLAGS = frozenset({
    "--output-format", "--resume", "--mcp-config", "--append-system-prompt",
    "--model", "--allowedTools", "--disallowedTools",
})


def parse_argv(argv: list[str]) -> dict:
    """Pull out only what the fake needs. `--` ends the flags; the single
    element after it is the prompt."""
    out: dict = {
        "output_format": "stream-json",
        "resume": None,
        "mcp_servers": {},
        "prompt": "",
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            out["prompt"] = " ".join(argv[i + 1 :])
            break
        if arg.startswith("--output-format="):
            out["output_format"] = arg.split("=", 1)[1]
        elif arg in _VALUE_FLAGS:
            value = argv[i + 1] if i + 1 < len(argv) else ""
            if arg == "--output-format":
                out["output_format"] = value
            elif arg == "--resume":
                out["resume"] = value
            elif arg == "--mcp-config":
                try:
                    out["mcp_servers"] = json.loads(value).get("mcpServers", {})
                except json.JSONDecodeError:
                    out["mcp_servers"] = {}
            i += 1  # skip the value
        i += 1
    return out


def parse_directive(prompt: str) -> list[dict]:
    match = _DIRECTIVE_RE.search(prompt)
    if not match:
        return []
    try:
        ops = json.loads(match.group(1))
    except json.JSONDecodeError:
        sys.stderr.write(f"fake-claude: unparseable directive: {match.group(1)[:200]}\n")
        return []
    return ops if isinstance(ops, list) else [ops]


# --------------------------------------------------------------------------- #
# MCP client — talks to the real bg / ask stdio servers the harness assembled
# --------------------------------------------------------------------------- #


async def _call_mcp_tool(spec: dict, tool: str, arguments: dict) -> str:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=spec["command"],
        args=spec.get("args", []),
        env={**os.environ, **spec.get("env", {})},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            parts = [
                block.text
                for block in result.content
                if getattr(block, "type", None) == "text"
            ]
            return "\n".join(parts)


def call_mcp_tool(mcp_servers: dict, key: str, tool: str, arguments: dict) -> str:
    spec = mcp_servers.get(key)
    if spec is None:
        return f"Error: MCP server {key!r} was not configured for this turn."
    return asyncio.run(_call_mcp_tool(spec, tool, arguments))


# --------------------------------------------------------------------------- #
# turn
# --------------------------------------------------------------------------- #


def run_bg_result_turn(prompt: str, state: dict) -> None:
    """Answer the `[bg-task-result]` turn the server injects when a bg task
    reaches a terminal state, per the `on_bg` rule the first turn stored."""
    rule = state.get("rules", {}).get("on_bg")
    if not rule:
        _emit_text("Acknowledged the background task result.")
        return

    spill = _SPILL_PATH_RE.search(prompt)
    if spill:
        # The pointer prompt replaced the payload. Follow it the way a real
        # model does: emit a `Read` tool_use naming the file, then the matching
        # tool_result carrying what came back, and answer only from that.
        #
        # Deliberately NOT a bare `read_text()` short-circuit. The claim under
        # test is the whole pointer round-trip — the model is handed a pointer,
        # ISSUES A READ, receives the content, answers from it. Reading the file
        # privately inside this shim and jumping straight to the answer would
        # still surface the sentinel while skipping the tool-call leg, so a
        # backend that spilled the file but never surfaced a usable pointer
        # would sail through. Emitting the real tool_use/tool_result pair keeps
        # the stream-json parser, the transcript and the event stream on exactly
        # the path a real turn takes.
        path = spill.group(1)
        tool_use_id = "toolu_fake_spill_read"
        _emit_tool_use(tool_use_id, "Read", {"file_path": path})
        try:
            body = pathlib.Path(path).read_text()
        except OSError as e:
            _emit_tool_result(tool_use_id, f"Error reading {path}: {e}", is_error=True)
            _emit_text(f"Could not read the spilled prompt: {e}")
            return
        _emit_tool_result(tool_use_id, body)
        haystack = body
    else:
        haystack = prompt

    required = rule.get("require")
    if required and required not in haystack:
        _emit_text(
            f"The bg-task result did not contain the expected marker {required!r}."
        )
        return
    _emit_text(rule["v"])


class _UsageLimited(Exception):
    """The `limit` op fired: abandon the turn and fail it the way a real
    usage-limit rejection does (limit-auto-resume.md §4)."""

    def __init__(self, reset_at: int) -> None:
        super().__init__("usage limit")
        self.reset_at = reset_at


def run_ops(ops: list[dict], parsed: dict, state: dict) -> None:
    mcp_servers = parsed["mcp_servers"]

    for index, op in enumerate(ops):
        kind = op.get("t")

        if kind == "text":
            _emit_text(op["v"])

        elif kind == "bash":
            tool_use_id = f"toolu_fake_{index}"
            command = op["cmd"]
            _emit_tool_use(tool_use_id, "Bash", {"command": command})
            match = re.match(r"^sleep\s+([\d.]+)$", command.strip())
            _sleep(float(match.group(1)) if match else 0.0)
            _emit_tool_result(tool_use_id, "")

        elif kind == "ask":
            tool_use_id = f"toolu_fake_ask_{index}"
            questions = op["questions"]
            _emit_tool_use(tool_use_id, "mcp__ask__user", {"questions": questions})
            answer = call_mcp_tool(mcp_servers, "ask", "user", {"questions": questions})
            _emit_tool_result(tool_use_id, answer)
            _emit_text(f"You chose: {answer}")

        elif kind == "bg":
            tool_use_id = f"toolu_fake_bg_{index}"
            arguments = {"command": op["command"], "description": op.get("description")}
            _emit_tool_use(tool_use_id, "mcp__bg__run", arguments)
            started = call_mcp_tool(mcp_servers, "bg", "run", arguments)
            _emit_tool_result(tool_use_id, started)
            _emit_text("started")

        elif kind == "task_complete":
            tool_use_id = f"toolu_fake_task_complete_{index}"
            arguments = {"summary": op["summary"], "metadata": {"e2e": True}}
            _emit_tool_use(tool_use_id, "mcp__tasks__complete", arguments)
            completed = call_mcp_tool(mcp_servers, "tasks", "complete", arguments)
            _emit_tool_result(tool_use_id, completed)
            _emit_text("completed")

        elif kind == "task_block":
            tool_use_id = f"toolu_fake_task_block_{index}"
            arguments = {"reason": op["reason"], "kind": op.get("kind", "input")}
            _emit_tool_use(tool_use_id, "mcp__tasks__block", arguments)
            blocked = call_mcp_tool(mcp_servers, "tasks", "block", arguments)
            _emit_tool_result(tool_use_id, blocked)
            _emit_text("blocked")

        elif kind == "task_reflect":
            tool_use_id = f"toolu_fake_task_reflect_{index}"
            arguments = {
                key: op[key]
                for key in (
                    "memory_note", "claude_md_note", "skill_candidate_ids",
                    "nothing_note",
                )
                if key in op
            }
            _emit_tool_use(tool_use_id, "mcp__tasks__reflect", arguments)
            reflected = call_mcp_tool(mcp_servers, "tasks", "reflect", arguments)
            _emit_tool_result(tool_use_id, reflected)
            _emit_text("reflected")

        elif kind == "skill_propose":
            tool_use_id = f"toolu_fake_skill_propose_{index}"
            arguments = {
                "slug": op["slug"],
                "title": op["title"],
                "description": op["description"],
                "body_markdown": op["body_markdown"],
                "rationale": op["rationale"],
            }
            _emit_tool_use(tool_use_id, "mcp__skills__propose", arguments)
            proposed = call_mcp_tool(mcp_servers, "skills", "propose", arguments)
            _emit_tool_result(tool_use_id, proposed)
            _emit_text("proposed")

        elif kind == "invoke_skill":
            # Simulate the CLI's own native `Skill` tool_use — this is NOT an
            # MCP call (skills are a CLI-native mechanism), just the same
            # tool_use/tool_result shape a real skill invocation would stream,
            # so the server's use_count hook (session_manager.py) fires on it.
            tool_use_id = f"toolu_fake_invoke_skill_{index}"
            _emit_tool_use(tool_use_id, "Skill", {"skill": op["slug"]})
            _emit_tool_result(tool_use_id, f"Loaded skill {op['slug']}")

        elif kind == "write_file":
            tool_use_id = f"toolu_fake_write_{index}"
            relative = pathlib.Path(str(op["path"]))
            _emit_tool_use(
                tool_use_id,
                "Write",
                {"file_path": str(relative), "content": str(op["v"])},
            )
            root = pathlib.Path.cwd().resolve()
            target = (root / relative).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                _emit_tool_result(
                    tool_use_id,
                    "Error: fake write path escaped the worker workspace",
                    is_error=True,
                )
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(op["v"]))
            _emit_tool_result(tool_use_id, f"Wrote {target.relative_to(root)}")

        elif kind == "remember":
            state["remember"] = op["v"]
            _emit_text("OK")

        elif kind == "recall":
            remembered = state.get("remember")
            _emit_text(remembered if remembered else "I don't remember.")

        elif kind == "on_bg":
            state.setdefault("rules", {})["on_bg"] = {
                "v": op["v"],
                "require": op.get("require"),
            }

        elif kind == "limit":
            # Fail the turn on the USER'S OWN usage limit, exactly as the real
            # CLI does (limit-auto-resume.md §4). The shape here is copied from
            # a live capture (tests/fixtures/limit_claude_user_5h.jsonl): the
            # `rate_limit_event` carrying the window claim + epoch is the ONLY
            # thing separating this from a server-side 429, and it lands BEFORE
            # the terminal result. `reset_in` seconds from now keeps the epoch
            # live so the wake-up is schedulable in test time.
            #
            # Limits ONCE per working dir, then lets the turn through — the
            # window really does reopen, so an auto-resumed turn must be able to
            # succeed. Without this the resumed turn (which re-runs the original
            # prompt, directive and all) would re-park forever. The flag lives
            # on disk, not in the session state, because the no-output resume
            # mode deliberately discards the failed attempt's session id.
            already = _limit_flag_path(parsed["prompt"])
            if already.exists():
                already.unlink()
                _emit_text("Resumed after the limit reset.")
                continue
            already.parent.mkdir(parents=True, exist_ok=True)
            already.write_text("1")
            reset_at = int(time.time()) + int(op.get("reset_in", 3600))
            _emit({
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "rejected",
                    "resetsAt": reset_at,
                    "rateLimitType": op.get("kind", "five_hour"),
                    "isUsingOverage": False,
                },
            })
            raise _UsageLimited(reset_at)

        else:
            sys.stderr.write(f"fake-claude: unknown op {kind!r}\n")


def run_turn(parsed: dict) -> int:
    prompt = parsed["prompt"]

    # A bogus credential must fail the turn the way the real CLI does: the
    # session-level env override is the thing under test.
    if os.environ.get("ANTHROPIC_API_KEY", "").startswith(_BOGUS_KEY_PREFIX):
        session_id = parsed["resume"] or str(uuid.uuid4())
        _emit({"type": "system", "subtype": "init", "session_id": session_id,
               "cwd": os.getcwd(), "tools": [], "model": "fake"})
        _emit({
            "type": "result", "subtype": "error_during_execution", "is_error": True,
            "session_id": session_id, "duration_ms": 5, "num_turns": 1,
            "result": "API Error: 401 invalid x-api-key",
        })
        return 0

    session_id = parsed["resume"] or str(uuid.uuid4())
    state = _load_state(session_id) if parsed["resume"] else {"remember": None, "rules": {}}

    _emit({
        "type": "system", "subtype": "init", "session_id": session_id,
        "cwd": os.getcwd(), "tools": ["Bash", "Read", "Write"], "model": "fake-claude",
        "permissionMode": "bypassPermissions",
    })

    ops = parse_directive(prompt)
    try:
        if prompt.lstrip().startswith("[bg-task-result]"):
            run_bg_result_turn(prompt, state)
        elif ops:
            run_ops(ops, parsed, state)
        else:
            _emit_text("Acknowledged.")
    except _UsageLimited:
        # The real CLI's user-limit failure: an is_error result whose prose is
        # the rendered reset ("You've hit your session limit · resets 10:22pm"),
        # captured live. The prose is deliberately UNPARSEABLE for a machine —
        # the server must take the epoch from the rate_limit_event above.
        _save_state(session_id, state)
        _emit({
            "type": "result", "subtype": "success", "is_error": True,
            "api_error_status": 429, "session_id": session_id,
            "duration_ms": 12, "num_turns": 1,
            "result": "You've hit your session limit · resets 10:22pm (Asia/Shanghai)",
        })
        return 0

    _save_state(session_id, state)

    _emit({
        "type": "result", "subtype": "success", "is_error": False,
        "duration_ms": 42, "num_turns": 1, "session_id": session_id,
        "total_cost_usd": 0.0001,
        "usage": {
            "input_tokens": 11, "output_tokens": 7,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        },
    })
    return 0


def run_oneshot(parsed: dict) -> int:
    """`--output-format=json`: the tool-free call behind `/schedule` NL parsing,
    `/showme` resolution, and the research reasoning leaf."""
    _sleep(_ONESHOT_DELAY_SECONDS)
    _emit({"type": "result", "subtype": "success", "is_error": False,
           "result": "{}", "session_id": str(uuid.uuid4()), "total_cost_usd": 0.0})
    return 0


def _real_cli_requested() -> bool:
    """The turn's cwd (the session's working dir) opts into the real binary."""
    return (pathlib.Path.cwd() / _REAL_CLI_MARKER).exists()


def exec_real_cli() -> int:
    """Replace this process with the real `claude`, argv untouched. Our own dir
    is stripped from PATH first, or `which` would resolve back to this shim."""
    here = str(pathlib.Path(__file__).resolve().parent)
    path = os.pathsep.join(
        d for d in os.environ.get("PATH", "").split(os.pathsep)
        if d and pathlib.Path(d).resolve(strict=False) != pathlib.Path(here)
    )
    real = shutil.which("claude", path=path)
    if real is None:
        sys.stderr.write(
            f"fake-claude: {_REAL_CLI_MARKER} present but no real `claude` on PATH\n"
        )
        return 127
    os.execv(real, ["claude", *sys.argv[1:]])


def main() -> int:
    if _real_cli_requested():
        return exec_real_cli()
    parsed = parse_argv(sys.argv[1:])
    if parsed["output_format"] == "json":
        return run_oneshot(parsed)
    return run_turn(parsed)


if __name__ == "__main__":
    sys.exit(main())
