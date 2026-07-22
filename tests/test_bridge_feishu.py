"""Tests for the Feishu bridge (docs/plans/feishu-bridge.md).

Outbound is patched at the bridge's own `_send` / `_api` boundary (the
thread-pool REST layer), and the low-level HTTP (`_sync_request`) is mocked in
the REST-layer tests — no network is touched. We drive `_on_message` /
`_on_card_action` with constructed SDK events. Covers the §6.2 unit matrix:
fail-closed allowlist, card-value validation (session not on agent / tool not
pending / consumed nonce), one-time nonce, transport config matrix (incl.
half-cred boot failure), and the REST layer (token fetch, refresh-on-invalid,
backoff-retry on transient codes, hard-fail → None).
"""

from __future__ import annotations

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
    # Outbound goes through _send / _api (thread-pool REST). Mock at that
    # boundary so card-building and routing are captured without real HTTP.
    bridge._send = AsyncMock(return_value={"code": 0, "data": {"message_id": "om_x"}})
    bridge._api = AsyncMock(return_value={"code": 0, "data": {"message_id": "om_x"}})
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


def _button_value(send_mock, index: int = 0) -> dict:
    """Extract the first button's `value` dict from a captured card send."""
    _, message = send_mock.await_args_list[index].args
    card = message["card"]
    action_el = next(e for e in card["elements"] if e["tag"] == "action")
    return action_el["actions"][0]["value"]


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

    async def test_deny_click_routes(self):
        b = _make_bridge()
        await b.send_tool_approval_request("oc_1", "sess-A", "tu-1", "Bash", {})
        value = _button_value(b._send)
        value = {**value, "action": "deny"}  # tap the Deny button (same nonce)
        await b._on_card_action(_card_event(value))
        b.manager.handle_tool_decision.assert_awaited_once_with(
            "feishu", "oc_1", "sess-A", "tu-1", False
        )

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
# REST layer (§4.3): thread-pool _api — token fetch, refresh-on-invalid,
# backoff-retry on transient codes, hard-fail → None. _sync_request (the
# actual blocking HTTP) is mocked; everything above it runs for real.
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
    async def test_send_fetches_token_then_posts(self):
        b = _http_bridge([_TOKEN_OK, _MSG_OK])
        assert await b._send("oc_1", {"text": "hi"}) == _MSG_OK
        urls = [c.args[1] for c in b._sync_request.call_args_list]
        assert "tenant_access_token" in urls[0]
        assert "/im/v1/messages" in urls[1]

    async def test_card_send_uses_interactive_msg_type(self):
        b = _http_bridge([_TOKEN_OK, _MSG_OK])
        await b._send("oc_1", {"card": {"elements": []}})
        body = b._sync_request.call_args_list[1].args[2]
        assert body["msg_type"] == "interactive"

    async def test_invalid_token_refreshes_and_retries(self):
        b = _http_bridge([_TOKEN_OK, {"code": 99991663, "msg": "invalid"}, _TOKEN_OK, _MSG_OK])
        assert await b._send("oc_1", {"text": "hi"}) == _MSG_OK
        assert b._sync_request.call_count == 4

    async def test_transient_code_backs_off_and_retries(self):
        b = _http_bridge([_TOKEN_OK, {"code": 230020, "msg": "rate"}, _MSG_OK])
        assert await b._send("oc_1", {"text": "hi"}) == _MSG_OK

    async def test_hard_error_returns_none(self):
        b = _http_bridge([_TOKEN_OK, {"code": 99999, "msg": "nope"}])
        assert await b._send("oc_1", {"text": "hi"}) is None

    async def test_sync_request_exception_returns_none(self):
        b = _http_bridge([_TOKEN_OK, RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")])
        assert await b._send("oc_1", {"text": "hi"}) is None

    async def test_token_is_cached_across_sends(self):
        b = _http_bridge([_TOKEN_OK, _MSG_OK, _MSG_OK])
        await b._send("oc_1", {"text": "a"})
        await b._send("oc_1", {"text": "b"})
        urls = [c.args[1] for c in b._sync_request.call_args_list]
        assert sum("tenant_access_token" in u for u in urls) == 1

    async def test_token_fetch_failure_aborts_send(self):
        b = _http_bridge([{"code": 99991400, "msg": "bad app"}])
        assert await b._send("oc_1", {"text": "hi"}) is None
        # No message POST attempted when no token.
        assert all("/im/v1/messages" not in c.args[1] for c in b._sync_request.call_args_list)
