"""Tests for the Feishu bridge (docs/plans/feishu-bridge.md).

Outbound is patched at the bridge's own `_send` boundary (which enqueues to the
worker thread), and the low-level HTTP (`_sync_request`) is mocked in the
REST-layer tests — no network is touched. We drive `_on_message` /
`_on_card_action` with constructed SDK events. Covers the §6.2 unit matrix:
fail-closed allowlist, card-action integrity (nonce bound to its kind + record
fields, replay/tamper rejected without consuming), one-time nonce, transport
config matrix (incl. half-cred boot failure), the REST layer (token fetch,
refresh-on-invalid, backoff-retry, hard-fail → None), and start/stop lifecycle.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from lark_channel.channel.types import (
    CardActionEvent,
    CardActionPayload,
    Conversation,
    EventOperator,
    Identity,
    InboundMessage,
)

from server.bridges.feishu import (
    FeishuBridge,
    FeishuConfigError,
    build_feishu_bridge,
)
from server.bridges.manager import BridgeManager, ChatBinding


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _settings(**over):
    base = dict(
        feishu_app_id="cli_x",
        feishu_app_secret="sec",
        feishu_transport="webhook",
        feishu_verification_token="vtok",
        feishu_encrypt_key=None,
        feishu_domain="http://127.0.0.1:9",
        feishu_allowed_open_ids=["ou_me"],
        feishu_allowed_chat_ids=[],
    )
    base.update(over)
    return SimpleNamespace(**base)


def _make_bridge(**over) -> FeishuBridge:
    """A bridge with a stub manager and patched outbound (no network)."""
    manager = SimpleNamespace(
        handle_incoming=AsyncMock(),
        handle_tool_decision=AsyncMock(return_value=True),
        switch_session=AsyncMock(return_value="Switched to session 's1'."),
    )
    bridge = build_feishu_bridge(manager, _settings(**over))
    assert bridge is not None
    # Outbound enqueues via _send; mock it so card-building and routing are
    # captured without touching the worker/network.
    bridge._send = AsyncMock(return_value=None)
    return bridge


def _inbound(
    *,
    text: str = "hello",
    chat_id: str = "oc_1",
    chat_type: str = "p2p",
    sender: str = "ou_me",
    mentioned_bot: bool = False,
    thread_id: str | None = None,
    raw_content_type: str = "text",
) -> InboundMessage:
    msg = InboundMessage(
        id="m1",
        create_time=0,
        conversation=Conversation(chat_id=chat_id, chat_type=chat_type, thread_id=thread_id),
        sender=Identity(open_id=sender),
    )
    msg.content_text = text
    msg.body_text = text
    msg.mentioned_bot = mentioned_bot
    msg.raw_content_type = raw_content_type
    return msg


def _card_event(value, *, operator: str = "ou_me", chat_id: str = "oc_1") -> CardActionEvent:
    return CardActionEvent(
        message_id="om_card",
        chat_id=chat_id,
        operator=EventOperator(open_id=operator),
        action=CardActionPayload(value=value, tag="button"),
    )


async def _poll(cond, timeout: float = 2.0) -> None:
    """Await until `cond()` is truthy, polling the running worker thread's
    progress. Raises if it never becomes true within `timeout`."""
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within timeout")


def _button_value(send_mock, index: int = 0, button: int = 0) -> dict:
    """Extract a button's `value` dict from a captured card send.

    `index` picks which send (call), `button` picks which action button within
    that card (0 = Allow / first, 1 = Deny / second)."""
    _, message = send_mock.await_args_list[index].args
    card = message["card"]
    action_el = next(e for e in card["elements"] if e["tag"] == "action")
    return action_el["actions"][button]["value"]


# --------------------------------------------------------------------------
# Transport / config matrix (§4.1, §4.2)
# --------------------------------------------------------------------------


class TestBuildMatrix:
    def test_neither_credential_returns_none(self):
        assert build_feishu_bridge(object(), _settings(feishu_app_id=None, feishu_app_secret=None)) is None

    def test_half_credential_is_boot_failure(self):
        with pytest.raises(FeishuConfigError):
            build_feishu_bridge(object(), _settings(feishu_app_secret=None))
        with pytest.raises(FeishuConfigError):
            build_feishu_bridge(object(), _settings(feishu_app_id=None))

    def test_webhook_without_token_is_boot_failure(self):
        with pytest.raises(FeishuConfigError):
            build_feishu_bridge(object(), _settings(feishu_verification_token=None))

    def test_ws_transport_without_token_is_allowed(self):
        b = build_feishu_bridge(
            object(), _settings(feishu_transport="ws", feishu_verification_token=None)
        )
        assert b is not None and b.transport == "ws"

    def test_bad_transport_value_rejected(self):
        with pytest.raises(FeishuConfigError):
            build_feishu_bridge(object(), _settings(feishu_transport="carrier-pigeon"))

    def test_strict_security_enabled(self):
        b = _make_bridge()
        assert b._channel.config.security.is_strict is True

    def test_loopback_domain_disables_env_proxy(self):
        b = _make_bridge(feishu_domain="http://127.0.0.1:9")
        assert b._channel.config.transport.trust_env_proxy is False

    def test_real_domain_honors_env_proxy(self):
        b = _make_bridge(feishu_domain="https://open.feishu.cn")
        assert b._channel.config.transport.trust_env_proxy is None

    def test_loopback_flag_set(self):
        # Drives the sync-request proxy exemption (§4.3).
        assert _make_bridge(feishu_domain="http://127.0.0.1:9")._loopback is True
        assert _make_bridge(feishu_domain="https://open.feishu.cn")._loopback is False


# --------------------------------------------------------------------------
# Inbound: fail-closed authorization + routing (§4.2, §4.5)
# --------------------------------------------------------------------------


class TestInbound:
    async def test_empty_allowlist_rejects_everyone(self):
        b = _make_bridge(feishu_allowed_open_ids=[])
        await b._on_message(_inbound(sender="ou_anyone"))
        b.manager.handle_incoming.assert_not_awaited()
        b._send.assert_not_awaited()  # silent drop, no presence leak

    async def test_unauthorized_sender_rejected(self):
        b = _make_bridge()
        await b._on_message(_inbound(sender="ou_stranger"))
        b.manager.handle_incoming.assert_not_awaited()

    async def test_authorized_p2p_text_routes(self):
        b = _make_bridge()
        await b._on_message(_inbound(text="do a thing", chat_id="oc_p2p"))
        b.manager.handle_incoming.assert_awaited_once_with(
            "feishu", "oc_p2p", "do a thing", b
        )

    async def test_group_without_mention_ignored(self):
        b = _make_bridge()
        await b._on_message(_inbound(chat_type="group", mentioned_bot=False))
        b.manager.handle_incoming.assert_not_awaited()

    async def test_group_with_mention_routes(self):
        b = _make_bridge()
        await b._on_message(
            _inbound(text="summarize", chat_type="group", mentioned_bot=True, chat_id="oc_grp")
        )
        b.manager.handle_incoming.assert_awaited_once_with(
            "feishu", "oc_grp", "summarize", b
        )

    async def test_group_chat_allowlist_enforced(self):
        b = _make_bridge(feishu_allowed_chat_ids=["oc_ok"])
        await b._on_message(
            _inbound(chat_type="group", mentioned_bot=True, chat_id="oc_blocked")
        )
        b.manager.handle_incoming.assert_not_awaited()
        # allowed chat passes
        await b._on_message(
            _inbound(text="hi", chat_type="group", mentioned_bot=True, chat_id="oc_ok")
        )
        b.manager.handle_incoming.assert_awaited_once()

    async def test_thread_message_rejected_with_notice(self):
        b = _make_bridge()
        await b._on_message(_inbound(thread_id="omt_123"))
        b.manager.handle_incoming.assert_not_awaited()
        b._send.assert_awaited_once()  # unsupported notice

    async def test_non_text_rejected_with_notice(self):
        b = _make_bridge()
        await b._on_message(_inbound(raw_content_type="image", text=""))
        b.manager.handle_incoming.assert_not_awaited()
        b._send.assert_awaited_once()

    async def test_bare_mention_empty_text_nudges(self):
        b = _make_bridge()
        await b._on_message(
            _inbound(text="", chat_type="group", mentioned_bot=True)
        )
        b.manager.handle_incoming.assert_not_awaited()
        b._send.assert_awaited_once()


# --------------------------------------------------------------------------
# Card actions: nonce one-time use + operator authorization (§4.2, §4.4)
# --------------------------------------------------------------------------


class TestCardActions:
    async def test_approval_button_carries_full_identity(self):
        b = _make_bridge()
        await b.send_tool_approval_request(
            "oc_1", "sess-A", "tu-1", "Bash", {"command": "ls"}
        )
        value = _button_value(b._send)
        assert value["session_id"] == "sess-A"
        assert value["tool_use_id"] == "tu-1"
        assert value["action"] == "approve"
        assert value["nonce"]

    async def test_approve_click_routes_then_nonce_consumed(self):
        b = _make_bridge()
        await b.send_tool_approval_request(
            "oc_1", "sess-A", "tu-1", "Bash", {"command": "ls"}
        )
        value = _button_value(b._send)

        await b._on_card_action(_card_event(value))
        b.manager.handle_tool_decision.assert_awaited_once_with(
            "feishu", "oc_1", "sess-A", "tu-1", True
        )

        # Second identical click is inert — the nonce is one-time (§4.2).
        b.manager.handle_tool_decision.reset_mock()
        await b._on_card_action(_card_event(value))
        b.manager.handle_tool_decision.assert_not_awaited()

    async def test_allow_and_deny_have_independent_nonces(self):
        # Each button carries its OWN nonce, bound to its own action.
        b = _make_bridge()
        await b.send_tool_approval_request("oc_1", "sess-A", "tu-1", "Bash", {})
        allow = _button_value(b._send, button=0)
        deny = _button_value(b._send, button=1)
        assert allow["action"] == "approve" and deny["action"] == "deny"
        assert allow["nonce"] and deny["nonce"] and allow["nonce"] != deny["nonce"]
        assert b._nonces[allow["nonce"]]["action"] == "approve"
        assert b._nonces[deny["nonce"]]["action"] == "deny"

    async def test_deny_click_routes(self):
        b = _make_bridge()
        await b.send_tool_approval_request("oc_1", "sess-A", "tu-1", "Bash", {})
        value = _button_value(b._send, button=1)  # tap the Deny button (own nonce)
        assert value["action"] == "deny"
        await b._on_card_action(_card_event(value))
        b.manager.handle_tool_decision.assert_awaited_once_with(
            "feishu", "oc_1", "sess-A", "tu-1", False
        )
        # One-time: a second identical deny click is inert (§4.2).
        b.manager.handle_tool_decision.reset_mock()
        await b._on_card_action(_card_event(value))
        b.manager.handle_tool_decision.assert_not_awaited()

    async def test_unauthorized_operator_rejected(self):
        b = _make_bridge()
        await b.send_tool_approval_request("oc_1", "sess-A", "tu-1", "Bash", {})
        value = _button_value(b._send)
        await b._on_card_action(_card_event(value, operator="ou_intruder"))
        b.manager.handle_tool_decision.assert_not_awaited()

    async def test_unknown_nonce_rejected(self):
        b = _make_bridge()
        await b._on_card_action(
            _card_event({"action": "approve", "session_id": "s", "tool_use_id": "t", "nonce": "forged"})
        )
        b.manager.handle_tool_decision.assert_not_awaited()

    async def test_switch_button_carries_nonce_and_routes_once(self):
        b = _make_bridge()
        await b.send_session_list(
            "oc_1",
            [{"id": "s1", "name": "One", "status": "idle", "current": False}],
        )
        value = _button_value(b._send)
        assert value["action"] == "switch" and value["session_id"] == "s1" and value["nonce"]

        await b._on_card_action(_card_event(value))
        b.manager.switch_session.assert_awaited_once_with("feishu", "oc_1", "s1")

        b.manager.switch_session.reset_mock()
        await b._on_card_action(_card_event(value))  # consumed
        b.manager.switch_session.assert_not_awaited()


# --------------------------------------------------------------------------
# BridgeManager.handle_tool_decision — §4.4 session-routing validation
# --------------------------------------------------------------------------


class _StubSM:
    def __init__(self, sessions: dict[str, str], pending: bool = True):
        self._sessions = sessions  # session_id -> agent_id
        self._pending = pending
        self.approved: tuple | None = None
        self.denied: tuple | None = None

    def get_session(self, sid):
        aid = self._sessions.get(sid)
        return SimpleNamespace(id=sid, agent_id=aid) if aid else None

    async def approve_tool(self, sid, tid):
        self.approved = (sid, tid)
        return self._pending

    async def deny_tool(self, sid, tid, reason=""):
        self.denied = (sid, tid, reason)
        return self._pending


def _manager(sm) -> BridgeManager:
    bm = BridgeManager(sm, db=None)
    bm._mappings["feishu:oc_1"] = ChatBinding(agent_id="agent-A", session_id="sess-A")
    return bm


class TestDecisionRouting:
    async def test_approve_on_bound_agent_pending(self):
        sm = _StubSM({"sess-A": "agent-A"})
        bm = _manager(sm)
        assert await bm.handle_tool_decision("feishu", "oc_1", "sess-A", "tu-1", True) is True
        assert sm.approved == ("sess-A", "tu-1")

    async def test_session_on_different_agent_is_rejected(self):
        # The card names a session that belongs to ANOTHER agent — must not
        # settle it, even though the chat is bound (§4.4 stale-card guard).
        sm = _StubSM({"sess-X": "agent-OTHER"})
        bm = _manager(sm)
        assert await bm.handle_tool_decision("feishu", "oc_1", "sess-X", "tu-1", True) is False
        assert sm.approved is None

    async def test_unknown_session_rejected(self):
        sm = _StubSM({})
        bm = _manager(sm)
        assert await bm.handle_tool_decision("feishu", "oc_1", "gone", "tu-1", True) is False

    async def test_unbound_chat_rejected(self):
        sm = _StubSM({"sess-A": "agent-A"})
        bm = _manager(sm)
        assert await bm.handle_tool_decision("feishu", "oc_other", "sess-A", "tu-1", True) is False
        assert sm.approved is None

    async def test_not_pending_returns_false(self):
        # SessionManager reports the tool_use isn't awaiting approval.
        sm = _StubSM({"sess-A": "agent-A"}, pending=False)
        bm = _manager(sm)
        assert await bm.handle_tool_decision("feishu", "oc_1", "sess-A", "tu-1", True) is False


# --------------------------------------------------------------------------
# Outbound surface (§4.3): failures surfaced, chunking, plain vs card
# --------------------------------------------------------------------------


class TestOutbound:
    async def test_long_text_chunks_into_multiple_cards(self):
        b = _make_bridge()
        await b.send_text("oc_1", "x" * 8000)
        assert b._send.await_count >= 2

    async def test_result_and_error_use_plain_text(self):
        b = _make_bridge()
        await b.send_result("oc_1", 0.01, is_error=False)
        _, message = b._send.await_args.args
        assert "text" in message and "card" not in message
        b._send.reset_mock()
        await b.send_error("oc_1", "nope")
        _, message = b._send.await_args.args
        assert "text" in message

    async def test_agent_text_uses_card(self):
        b = _make_bridge()
        await b.send_text("oc_1", "**bold**")
        _, message = b._send.await_args.args
        assert "card" in message

    async def test_status_noise_suppressed(self):
        b = _make_bridge()
        await b.send_status("oc_1", "running")
        b._send.assert_not_awaited()
        await b.send_status("oc_1", "Usage limit reset — resuming.")
        b._send.assert_awaited_once()


# --------------------------------------------------------------------------
# REST layer (§4.3): the worker-thread `_api_sync` — token fetch,
# refresh-on-invalid, backoff-retry on transient codes, hard-fail → None. The
# blocking HTTP (`_sync_request`) is mocked; everything above it runs for real.
# `_api_sync` is synchronous (it runs on the outbound worker), so these tests
# call it directly. `_send` only enqueues (fire-and-forget), verified separately.
# --------------------------------------------------------------------------

_TOKEN_OK = {"code": 0, "tenant_access_token": "t-1", "expire": 7200}
_MSG_OK = {"code": 0, "data": {"message_id": "om_1"}}


def _http_bridge(responses: list) -> FeishuBridge:
    manager = SimpleNamespace()
    bridge = build_feishu_bridge(manager, _settings())
    assert bridge is not None
    bridge._sync_request = Mock(side_effect=responses)
    return bridge


class TestApiLayer:
    def test_fetches_token_then_posts(self):
        b = _http_bridge([_TOKEN_OK, _MSG_OK])
        assert b._api_sync("POST", "/open-apis/im/v1/messages", {"x": 1}) == _MSG_OK
        urls = [c.args[1] for c in b._sync_request.call_args_list]
        assert "tenant_access_token" in urls[0]
        assert "/im/v1/messages" in urls[1]

    def test_invalid_token_refreshes_and_retries(self):
        b = _http_bridge([_TOKEN_OK, {"code": 99991663, "msg": "invalid"}, _TOKEN_OK, _MSG_OK])
        assert b._api_sync("POST", "/x", {}) == _MSG_OK
        assert b._sync_request.call_count == 4

    def test_transient_code_backs_off_and_retries(self):
        b = _http_bridge([_TOKEN_OK, {"code": 230020, "msg": "rate"}, _MSG_OK])
        assert b._api_sync("POST", "/x", {}) == _MSG_OK

    def test_hard_error_returns_none(self):
        b = _http_bridge([_TOKEN_OK, {"code": 99999, "msg": "nope"}])
        assert b._api_sync("POST", "/x", {}) is None

    def test_sync_request_exception_returns_none(self):
        b = _http_bridge([_TOKEN_OK, RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")])
        assert b._api_sync("POST", "/x", {}) is None

    def test_token_is_cached_across_calls(self):
        b = _http_bridge([_TOKEN_OK, _MSG_OK, _MSG_OK])
        b._api_sync("POST", "/x", {})
        b._api_sync("POST", "/y", {})
        urls = [c.args[1] for c in b._sync_request.call_args_list]
        assert sum("tenant_access_token" in u for u in urls) == 1

    def test_token_fetch_failure_aborts(self):
        b = _http_bridge([{"code": 99991400, "msg": "bad app"}])
        assert b._api_sync("POST", "/open-apis/im/v1/messages", {}) is None
        assert all("/im/v1/messages" not in c.args[1] for c in b._sync_request.call_args_list)

    async def test_send_enqueues_text_not_blocking(self):
        b = _http_bridge([])  # _sync_request never called — send only enqueues
        await b._send("oc_1", {"text": "hi"})
        method, path, body = b._out_q.get_nowait()
        assert method == "POST" and "/im/v1/messages" in path
        assert body["receive_id"] == "oc_1" and body["msg_type"] == "text"
        b._sync_request.assert_not_called()

    async def test_send_card_enqueues_interactive(self):
        b = _http_bridge([])
        await b._send("oc_1", {"card": {"elements": []}})
        _, _, body = b._out_q.get_nowait()
        assert body["msg_type"] == "interactive"

    def test_loopback_sync_request_disables_env_proxy(self, monkeypatch):
        # should-1: loopback must use a trust_env=False session (defeats ALL_PROXY).
        import server.bridges.feishu as feishu_mod
        captured = {}

        class _FakeResp:
            def json(self): return _MSG_OK

        class _FakeSession:
            def __init__(self): self.trust_env = True
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def request(self, *a, **k):
                captured["trust_env"] = self.trust_env
                return _FakeResp()

        monkeypatch.setattr(feishu_mod, "_is_loopback", lambda d: True)
        b = build_feishu_bridge(SimpleNamespace(), _settings())
        monkeypatch.setattr("requests.Session", _FakeSession)
        b._sync_request("POST", "http://127.0.0.1:9/x", {}, {})
        assert captured["trust_env"] is False


# --------------------------------------------------------------------------
# Card-action integrity (§4.4, Snape blocker-2): the nonce record is trusted,
# the card `value` is not. EVERY identity field of the click (action, session,
# tool) must match the nonce record; execution uses the RECORD's fields. Any
# mismatch is rejected WITHOUT consuming the nonce (so a legit later click still
# works).
# --------------------------------------------------------------------------


class TestCardActionSecurity:
    async def _mint_switch(self, b) -> dict:
        await b.send_session_list(
            "oc_1", [{"id": "s1", "name": "One", "status": "idle", "current": False}]
        )
        return _button_value(b._send)

    async def _mint_approval(self, b) -> dict:
        await b.send_tool_approval_request("oc_1", "REAL-sess", "REAL-tool", "Bash", {})
        return _button_value(b._send)

    async def test_switch_nonce_cannot_be_replayed_as_approval(self):
        # Snape PoC: take a /sessions switch nonce, forge an approve payload.
        b = _make_bridge()
        switch_value = await self._mint_switch(b)
        forged = {"action": "approve", "session_id": "victim-sess",
                  "tool_use_id": "victim-tool", "nonce": switch_value["nonce"]}
        await b._on_card_action(_card_event(forged))
        b.manager.handle_tool_decision.assert_not_awaited()  # attack blocked
        # nonce NOT consumed → the legitimate switch click still works
        await b._on_card_action(_card_event(switch_value))
        b.manager.switch_session.assert_awaited_once_with("feishu", "oc_1", "s1")

    async def test_switch_nonce_action_tamper_rejected(self):
        # Snape blocker: the switch branch must check value.action=="switch" too,
        # not just session_id. Keep the session_id CORRECT (so it can't be a
        # session mismatch) and only flip the action — the click must still be
        # rejected without consuming the nonce, and the real switch still works.
        b = _make_bridge()
        switch_value = await self._mint_switch(b)  # {action:switch, session_id:s1}
        for bad_action in ("approve", "deny"):
            forged = {**switch_value, "action": bad_action}  # session_id kept = s1
            await b._on_card_action(_card_event(forged))
        b.manager.switch_session.assert_not_awaited()
        b.manager.handle_tool_decision.assert_not_awaited()
        assert switch_value["nonce"] in b._nonces  # never consumed
        # The genuine switch click still routes.
        await b._on_card_action(_card_event(switch_value))
        b.manager.switch_session.assert_awaited_once_with("feishu", "oc_1", "s1")

    async def test_approval_missing_action_rejected(self):
        # Symmetric to the switch action check: an approval value with no action
        # key (None) mismatches record.action ("approve") — rejected, unconsumed.
        b = _make_bridge()
        appr = await self._mint_approval(b)
        forged = {k: v for k, v in appr.items() if k != "action"}  # drop action
        await b._on_card_action(_card_event(forged))
        b.manager.handle_tool_decision.assert_not_awaited()
        assert appr["nonce"] in b._nonces  # not consumed
        await b._on_card_action(_card_event(appr))
        b.manager.handle_tool_decision.assert_awaited_once()

    async def test_approval_nonce_cannot_be_replayed_as_switch(self):
        b = _make_bridge()
        appr = await self._mint_approval(b)
        forged = {"action": "switch", "session_id": "other", "nonce": appr["nonce"]}
        await b._on_card_action(_card_event(forged))
        b.manager.switch_session.assert_not_awaited()
        # legit approval still works (nonce not consumed)
        await b._on_card_action(_card_event(appr))
        b.manager.handle_tool_decision.assert_awaited_once_with(
            "feishu", "oc_1", "REAL-sess", "REAL-tool", True
        )

    async def test_tampered_identity_rejected_without_consuming(self):
        # A value whose session/tool differ from the nonce record is a forgery:
        # rejected, nonce preserved, handler NOT called. (This test previously —
        # wrongly — asserted the tampered click STILL approved the real record,
        # baking the vulnerability into the suite; reversed here.)
        b = _make_bridge()
        appr = await self._mint_approval(b)
        tampered = {**appr, "session_id": "ATTACK", "tool_use_id": "ATTACK"}
        await b._on_card_action(_card_event(tampered))
        b.manager.handle_tool_decision.assert_not_awaited()
        assert appr["nonce"] in b._nonces  # not consumed
        # The untampered click still routes to the REAL session/tool.
        await b._on_card_action(_card_event(appr))
        b.manager.handle_tool_decision.assert_awaited_once_with(
            "feishu", "oc_1", "REAL-sess", "REAL-tool", True
        )

    async def test_approve_nonce_cannot_be_used_to_deny(self):
        # The Allow button's nonce is bound to action="approve"; a value that
        # flips it to "deny" (keeping that nonce) is an action mismatch —
        # rejected without consuming, so the real Allow click still works.
        b = _make_bridge()
        appr = await self._mint_approval(b)  # the Allow button's value
        forged = {**appr, "action": "deny"}
        await b._on_card_action(_card_event(forged))
        b.manager.handle_tool_decision.assert_not_awaited()
        assert appr["nonce"] in b._nonces  # not consumed
        await b._on_card_action(_card_event(appr))
        b.manager.handle_tool_decision.assert_awaited_once_with(
            "feishu", "oc_1", "REAL-sess", "REAL-tool", True
        )

    async def test_unknown_action_rejected_without_consuming(self):
        b = _make_bridge()
        appr = await self._mint_approval(b)
        await b._on_card_action(_card_event({**appr, "action": "rm -rf"}))
        b.manager.handle_tool_decision.assert_not_awaited()
        # nonce survived → real approve still works
        await b._on_card_action(_card_event(appr))
        b.manager.handle_tool_decision.assert_awaited_once()

    async def test_switch_on_approval_nonce_does_not_consume(self):
        # A switch action against an approval nonce is a kind mismatch: rejected,
        # nonce preserved for the real approval click.
        b = _make_bridge()
        appr = await self._mint_approval(b)
        await b._on_card_action(_card_event({"action": "switch", "nonce": appr["nonce"]}))
        b.manager.switch_session.assert_not_awaited()
        assert appr["nonce"] in b._nonces  # not consumed


# --------------------------------------------------------------------------
# Lifecycle (§ should-3): keepalive tick + outbound worker start/stop.
# --------------------------------------------------------------------------


class TestLifecycle:
    def _stubbed(self):
        b = _make_bridge()
        b._channel.start_background = AsyncMock()
        b._channel.stop_background = AsyncMock()
        fake_future = Mock()
        fake_future.done.return_value = False
        # Close the tick coroutine we're handed (we don't run it here) so no
        # "coroutine was never awaited" warning; return the fake future.
        b._channel.schedule = Mock(side_effect=lambda coro: (coro.close(), fake_future)[1])
        return b, fake_future

    async def test_start_starts_worker_and_saves_tick(self):
        b, fake_future = self._stubbed()
        await b.start()
        try:
            assert b._tick_future is fake_future
            assert b._worker is not None and b._worker.is_alive()
        finally:
            await b.stop()

    async def test_restart_does_not_stack_tick_or_worker(self):
        b, fake_future = self._stubbed()
        await b.start()
        w1 = b._worker
        b._channel.schedule.reset_mock()
        await b.start()  # re-start
        try:
            assert b._tick_future is fake_future  # not re-scheduled
            b._channel.schedule.assert_not_called()
            assert b._worker is w1  # same worker, not stacked
        finally:
            await b.stop()

    async def test_stop_cancels_tick_and_joins_worker(self):
        b, fake_future = self._stubbed()
        await b.start()
        w = b._worker
        await b.stop()
        fake_future.cancel.assert_called_once()
        assert b._worker is None
        w.join(timeout=2)
        assert not w.is_alive()

    async def test_slow_send_stop_timeout_then_start_yields_working_worker(self):
        # Snape blocker (real in-flight repro, not a manual flag set): a worker
        # stuck in a slow _api_sync makes stop()'s join time out and leaves a
        # stale sentinel in the queue. The prior implementation let the fresh
        # worker read that stale None and die immediately. A correct start()
        # drains the queue first, so the new worker is genuinely live and
        # processes new messages.
        b, _ = self._stubbed()
        b._worker_join_timeout = 0.3  # make the join time out fast
        gate = threading.Event()
        in_flight = threading.Event()

        def slow_api(method, path, body):
            in_flight.set()
            gate.wait(3.0)  # hold the worker in-flight past the join timeout
            return None

        b._api_sync = slow_api
        await b.start()
        old = b._worker
        b._enqueue("POST", "/slow", {})       # worker dequeues and blocks in slow_api
        await _poll(in_flight.is_set)          # ensure it's really in-flight before stop
        await b.stop()                         # join(0.3) times out; worker still alive
        assert old.is_alive()                  # confirm the stop-timeout state
        assert b._worker is old                # reference kept (not a clean exit)
        gate.set()                             # let the slow send finish → worker exits

        # Immediate restart must reap the corpse, drain the stale sentinel, and
        # spawn a fresh, working worker.
        processed = threading.Event()

        def ok_api(method, path, body):
            processed.set()
            return None

        b._api_sync = ok_api
        await b.start()
        try:
            assert b._worker is not None and b._worker.is_alive()
            assert b._worker is not old          # genuinely new worker
            assert b._worker_stop.is_set() is False
            b._enqueue("POST", "/new", {})       # must be processed, not eaten by a stale None
            await _poll(processed.is_set)
            assert processed.is_set()
        finally:
            await b.stop()
        old.join(timeout=2)
        assert not old.is_alive()

    async def test_start_raises_and_leaves_channel_untouched_when_worker_stuck(self):
        # Snape should (real in-flight repro): if the lingering worker refuses to
        # exit, start() must fail loud BEFORE bringing up the channel/tick — no
        # half-started state to roll back.
        b, _ = self._stubbed()
        b._worker_join_timeout = 0.2
        gate = threading.Event()               # never set → worker stays stuck
        in_flight = threading.Event()

        def stuck_api(method, path, body):
            in_flight.set()
            gate.wait()                        # blocks indefinitely
            return None

        b._api_sync = stuck_api
        await b.start()
        b._enqueue("POST", "/stuck", {})
        await _poll(in_flight.is_set)
        await b.stop()                         # join times out; worker stays stuck-alive
        assert b._worker is not None and b._worker.is_alive()

        b._channel.start_background.reset_mock()
        b._channel.schedule.reset_mock()
        with pytest.raises(RuntimeError):
            await b.start()                    # join times out again → raise
        # The failure happened before channel/tick were (re)started.
        b._channel.start_background.assert_not_called()
        b._channel.schedule.assert_not_called()
        assert b._tick_future is None
        # cleanup: release the stuck worker so the thread can exit.
        gate.set()
        b._worker.join(timeout=2)

    async def test_channel_startup_failure_rolls_back_the_live_worker(self):
        # Snape should: start() is worker-first (_ensure_worker brings the
        # outbound worker up BEFORE the channel/tick). If the channel/tick then
        # fails, the already-live worker must be rolled back — otherwise a failed
        # start leaves a healthy outbound worker spinning against a dead channel,
        # and BridgeManager.start_all() swallows the start error so nobody cleans
        # it up. start() must re-raise the ORIGINAL error and leave nothing
        # half-started (no live worker, no residual tick).
        b, fake_future = self._stubbed()
        b._channel.start_background = AsyncMock(side_effect=RuntimeError("dial failed"))

        with pytest.raises(RuntimeError, match="dial failed"):
            await b.start()

        # The worker _ensure_worker() spawned must have been torn down: no live
        # feishu-outbound thread survives, and the reference is cleared.
        live_workers = [
            t for t in threading.enumerate()
            if t.name == "feishu-outbound" and t.is_alive()
        ]
        assert live_workers == []
        assert b._worker is None
        # No residual tick, and the channel/tick were never left half-started.
        assert b._tick_future is None
        b._channel.schedule.assert_not_called()  # start_background died first
