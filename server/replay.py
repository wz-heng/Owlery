"""Attempt-replay assembly layer (docs/plans/attempt-replay.md).

A read-only, session-keyed reconstruction of everything that happened during
a session's lifetime — turns, tool calls, delegations, cost, and (critically)
how each turn ended — merged into one time-ordered list. This is "组装层":
no new event-sourcing table, just a join across tables that already exist
(`messages`, `turn_usage`, `harness_exits`, `bg_tasks`, `delegation_runs`,
`task_events`), each of which already carries what it needs after the §3.1
write-side additions (`messages.created_at`, `turn_usage.message_seq`, the
`harness_exits` terminal-record invariant, downsampled heartbeat events).

Session-keyed, not task-run-keyed: a task-run entry point (see
`server/routers/task_boards.py`) just resolves run → session_id and calls
`assemble_session_replay` here — the same assembly works for an interactive
chat session, a task worker session, or a delegation child, uniformly.

Works for archived/dead sessions too (the whole point is post-mortem
replay): every source table is read straight from the DB, nothing depends on
in-memory `Session` state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .database import Database

# Any gap longer than this between two consecutive timestamped events is
# surfaced as an explicit "black hole" in the timeline (attempt-replay.md
# §3.2 — gap detection is a first-class citizen, not something the frontend
# has to notice on its own).
DEFAULT_GAP_THRESHOLD_SECONDS = 300


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _excerpt(text: str | None, limit: int = 160) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _message_event(row: dict[str, Any]) -> dict[str, Any]:
    role = row.get("role")
    mtype = row.get("type")
    seq = row.get("seq")
    ts = row.get("created_at")
    if mtype == "tool_use":
        return {
            "kind": "tool_call",
            "ts": ts,
            "seq": seq,
            "summary": f"tool call: {row.get('tool_name') or '?'}",
            "detail": {
                "tool_name": row.get("tool_name"),
                "tool_input": row.get("tool_input"),
                "tool_use_id": row.get("tool_use_id"),
            },
        }
    if mtype == "tool_result":
        ok = row.get("is_error")
        return {
            "kind": "tool_result",
            "ts": ts,
            "seq": seq,
            "summary": ("tool error: " if ok else "tool result: ")
            + (_excerpt(_content_as_text(row.get("content"))) or ""),
            "detail": {
                "tool_use_id": row.get("tool_use_id"),
                "is_error": ok,
                "content": row.get("content"),
            },
        }
    if mtype == "result":
        return {
            "kind": "message",
            "ts": ts,
            "seq": seq,
            "summary": "turn result" + (f" (cost ${row['cost']:.4f})" if row.get("cost") else ""),
            "detail": {"role": role, "type": mtype, "cost": row.get("cost")},
        }
    summary_text = _content_as_text(row.get("content"))
    is_error = row.get("is_error")
    return {
        "kind": "message",
        "ts": ts,
        "seq": seq,
        "summary": f"{role}{' error' if is_error else ''}: {_excerpt(summary_text) or mtype}",
        "detail": {
            "role": role,
            "type": mtype,
            "content": row.get("content"),
            "is_error": is_error,
        },
    }


def _content_as_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if content is None:
        return None
    return str(content)


def _turn_usage_event(row: dict[str, Any]) -> dict[str, Any]:
    parts = [row.get("model") or row.get("origin") or "usage"]
    if row.get("cost") is not None:
        parts.append(f"${row['cost']:.4f}")
    if row.get("total_tokens"):
        parts.append(f"{row['total_tokens']} tok")
    if row.get("duration_ms") is not None:
        parts.append(f"{row['duration_ms']}ms")
    if row.get("is_error"):
        parts.append("(error)")
    return {
        "kind": "turn_usage",
        "ts": row.get("created_at"),
        "seq": row.get("message_seq"),
        "summary": "usage: " + " · ".join(parts),
        "detail": row,
    }


def _harness_exit_event(row: dict[str, Any]) -> dict[str, Any]:
    reason = row.get("reason")
    bits = [reason]
    if row.get("exit_code") is not None:
        bits.append(f"exit_code={row['exit_code']}")
    if row.get("signal") is not None:
        bits.append(f"signal={row['signal']}")
    if row.get("escalation"):
        bits.append(f"escalated={row['escalation']}")
    return {
        "kind": "turn_terminal",
        "ts": row.get("created_at"),
        "seq": row.get("message_seq"),
        "summary": "turn ended: " + " ".join(bits),
        "detail": row,
    }


def _bg_task_events(row: dict[str, Any]) -> list[dict[str, Any]]:
    events = [
        {
            "kind": "bg_task_started",
            "ts": row.get("started_at"),
            "seq": None,
            "summary": f"bg task started: {_excerpt(row.get('description') or row.get('command'), 80)}",
            "detail": row,
        }
    ]
    if row.get("completed_at"):
        events.append(
            {
                "kind": "bg_task_finished",
                "ts": row.get("completed_at"),
                "seq": None,
                "summary": (
                    f"bg task {row.get('status')}: "
                    f"{_excerpt(row.get('description') or row.get('command'), 80)}"
                    + (f" (exit {row['exit_code']})" if row.get("exit_code") is not None else "")
                ),
                "detail": row,
            }
        )
    return events


def _task_event_event(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "task_event",
        "ts": row.get("created_at"),
        "seq": None,
        "summary": f"task event: {row.get('kind')}",
        "detail": {
            "event_kind": row.get("kind"),
            "actor_kind": row.get("actor_kind"),
            "actor_agent_id": row.get("actor_agent_id"),
            "payload": row.get("payload"),
        },
    }


def _delegation_event(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "delegation",
        "ts": row.get("created_at"),
        "seq": row.get("start_seq"),
        "summary": (
            f"delegated to {row.get('target_agent_name')}: "
            f"{_excerpt(row.get('request'), 100)}"
        ),
        "detail": {
            "delegation_id": row.get("delegation_id"),
            "run_id": row.get("run_id"),
            "round_no": row.get("round_no"),
            "request": row.get("request"),
            "state": row.get("state"),
            "error": row.get("error"),
            "finished_at": row.get("finished_at"),
            "target_agent_id": row.get("target_agent_id"),
            "target_agent_name": row.get("target_agent_name"),
        },
    }


def _insert_gaps(
    events: list[dict[str, Any]], *, gap_threshold_seconds: float
) -> list[dict[str, Any]]:
    """Interleave explicit gap markers between consecutive timed events
    whose delta exceeds the threshold (attempt-replay.md §3.2 — "空档检测
    是一等公民": the frontend must not have to compute this itself)."""
    out: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    prev_ts: datetime | None = None
    for event in events:
        ts = _parse_ts(event.get("ts"))
        if prev is not None and prev_ts is not None and ts is not None:
            delta = (ts - prev_ts).total_seconds()
            if delta > gap_threshold_seconds:
                out.append(
                    {
                        "kind": "gap",
                        "ts": None,
                        "seq": None,
                        "summary": f"{int(delta)}s of silence",
                        "detail": {
                            "duration_seconds": delta,
                            "before": prev,
                            "after": event,
                        },
                    }
                )
        out.append(event)
        if ts is not None:
            prev, prev_ts = event, ts
    return out


async def assemble_session_replay(
    db: Database,
    session_id: str,
    *,
    gap_threshold_seconds: float = DEFAULT_GAP_THRESHOLD_SECONDS,
) -> dict[str, Any] | None:
    """Merge every source table touching `session_id` into one time-ordered
    replay timeline. Returns None if the session doesn't exist at all (the
    caller turns that into a 404); an empty-but-present session (no turns
    yet) returns a timeline with zero events, not None.
    """
    if not await db.session_exists(session_id):
        return None

    messages = await db.load_messages(session_id)
    turn_usage = await db.list_turn_usage_for_session(session_id)
    harness_exits = await db.list_harness_exits_for_session(session_id)
    bg_tasks = await db.list_bg_tasks_for_session(session_id, limit=10_000)
    delegation_runs = await db.list_all_delegation_runs_for_parent(session_id)

    task_events: list[dict[str, Any]] = []
    task_run_ref: dict[str, Any] | None = None
    from .task_board.repository import task_repository

    # Most sessions (interactive chat, delegation children) have no Task
    # Board involvement at all, and a plain-chat deployment may never
    # initialize task_repository's own DB connection — this is a normal,
    # not-Task-Board-at-all replay in that case, not an error.
    if task_repository.is_initialized:
        run = await task_repository.get_run_by_session(session_id)
        if run is not None:
            task_run_ref = {"task_id": run.task_id, "run_id": run.id}
            records = await task_repository.list_task_events_for_run(run.id)
            task_events = [r.to_dict() for r in records]

    # Messages written before `messages.created_at` existed have no
    # timestamp. In the ordinary case that's a strict-by-seq PREFIX (the
    # column is stamped unconditionally on every write going forward, so
    # once a timestamped row exists nothing after it should ever be untimed
    # again) — surfaced as an explicit "recording starts here" marker rather
    # than silently guessing a time (attempt-replay.md §3.2 / §4.5).
    #
    # `messages` is ordered by seq (load_messages), so a single forward scan
    # separates the leading untimed run from everything after it. Anything
    # untimed found AFTER the first timestamped row is NOT part of that
    # innocuous prefix — it's an anomaly (manual insert, partial migration,
    # a future bug) that must stay visible on its own, not get silently
    # absorbed into "history predating tracking" and hide a real mid-session
    # observation gap (Snape review).
    prefix_untimed_messages: list[dict[str, Any]] = []
    anomalous_untimed_messages: list[dict[str, Any]] = []
    timed_messages: list[dict[str, Any]] = []
    in_prefix = True
    for m in messages:
        if m.get("created_at"):
            in_prefix = False
            timed_messages.append(m)
        elif in_prefix:
            prefix_untimed_messages.append(m)
        else:
            anomalous_untimed_messages.append(m)

    timed_events: list[dict[str, Any]] = []
    timed_events.extend(_message_event(m) for m in timed_messages)
    timed_events.extend(_turn_usage_event(r) for r in turn_usage)
    timed_events.extend(_harness_exit_event(r) for r in harness_exits)
    timed_events.extend(_task_event_event(r) for r in task_events)
    timed_events.extend(_delegation_event(r) for r in delegation_runs)
    for bt in bg_tasks:
        timed_events.extend(_bg_task_events(bt))

    parsed = [(e, ts) for e in timed_events if (ts := _parse_ts(e.get("ts"))) is not None]
    parsed.sort(key=lambda pair: (pair[1], pair[0].get("seq") if pair[0].get("seq") is not None else -1))
    timed_events = [e for e, _ts in parsed]

    timeline = _insert_gaps(timed_events, gap_threshold_seconds=gap_threshold_seconds)

    unobserved_prefix = None
    if prefix_untimed_messages:
        unobserved_prefix = {
            "summary": (
                f"{len(prefix_untimed_messages)} message(s) recorded before "
                "timestamps were tracked — chronology within this span is "
                "unknown, ordered by sequence only"
            ),
            "events": [_message_event(m) for m in prefix_untimed_messages],
        }

    untimed_anomalies = None
    if anomalous_untimed_messages:
        # Should not happen in normal operation — every write after the
        # first timestamped row is expected to carry a timestamp. Surfaced
        # distinctly (never merged into `unobserved_prefix` or silently
        # dropped) precisely because it's unexpected.
        untimed_anomalies = {
            "summary": (
                f"{len(anomalous_untimed_messages)} message(s) with no "
                "timestamp appeared AFTER timestamped rows — this shouldn't "
                "happen; their position in the timeline is unknown"
            ),
            "events": [_message_event(m) for m in anomalous_untimed_messages],
        }

    return {
        "session_id": session_id,
        "task_run": task_run_ref,
        "gap_threshold_seconds": gap_threshold_seconds,
        "unobserved_prefix": unobserved_prefix,
        "untimed_anomalies": untimed_anomalies,
        "timeline": timeline,
    }
