"""Real-uvicorn boot smoke test for the Feishu bridge in **ws** transport.

Why this file exists (regression guard for the ws-loop boot crash):

``lark_channel/ws/client.py`` binds a MODULE-LEVEL ``loop =
asyncio.get_event_loop()`` at import time, and ``WSClient.start()`` drives the
websocket with ``loop.run_until_complete(...)`` on THAT global (ignoring the
per-instance ``self._loop``). ``server/main.py`` imports the feishu bridge
*inside* the FastAPI lifespan — i.e. while uvicorn's loop is already running —
so that capture grabs the running server loop. ``start_background()`` then runs
the blocking ``WSClient.start()`` on a worker thread, which calls
``run_until_complete()`` on the server's own loop from that thread and STOPS it:
``RuntimeError: Event loop stopped before Future completed``, killing the whole
process at boot. It was the *production default* (``feishu_transport=ws``) yet
never exercised: webhook mode never builds a ``WSClient``, and every other
feishu test (unit + bridge e2e) is webhook, so the blind spot survived.

This test closes that blind spot with a **real uvicorn.Server** (not
``TestClient`` — the crash is a real-event-loop interaction that ``TestClient``'s
in-thread portal does not reproduce). It recreates the exact production
precondition — the SDK's module ``loop`` IS the running server loop — inside the
lifespan, then asserts the server still boots and serves. The fix
(``_isolate_lark_ws_loop``) rebinds that module global to an isolated loop, so
the ws client runs fully decoupled from the server loop.

Verified this test genuinely catches the regression: with the
``_isolate_lark_ws_loop`` call removed from ``FeishuBridge.start``, it fails —
the uvicorn thread dies with the RuntimeError above and ``server.started`` stays
False.

No real Feishu tenant is needed: fake creds + a dead loopback domain make the ws
connect fail fast and deterministically. The connect *failing* is irrelevant to
the bug — the crash is at ``run_until_complete`` entry, before the network — and
``BridgeManager.start_all`` swallows that failure exactly as in production, so a
healthy server is the expected post-fix outcome.
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
import urllib.request
from contextlib import asynccontextmanager

import lark_channel.ws.client as _lark_ws_client
import uvicorn
from fastapi import FastAPI

from server.bridges.feishu import FeishuBridge
from server.bridges.manager import BridgeManager


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _get_no_proxy(url: str, timeout: float) -> int:
    """GET bypassing any ambient http(s)_proxy (a Clash proxy on a dev Mac would
    otherwise swallow the loopback request — see CLAUDE.md)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as r:
        return r.status


def test_ws_transport_boots_under_real_uvicorn():
    dead_port = _free_port()  # nothing listens here → ws connect refused, fast
    serving_port = _free_port()
    state: dict = {"serving_loop": None, "reached_yield": False, "start_error": None}

    # Snapshot the SDK's module-level ws loop so we can restore it afterwards —
    # this test deliberately points it at the server loop to recreate the boot
    # hazard, and the fix rebinds it to a fresh loop; neither should leak into
    # the rest of the suite.
    original_ws_loop = getattr(_lark_ws_client, "loop", None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        serving_loop = asyncio.get_running_loop()
        state["serving_loop"] = serving_loop
        # Recreate main.py's import-inside-lifespan capture: make the SDK's
        # module ws loop BE this running server loop. Pre-fix, WSClient.start
        # would then stop this very loop from its worker thread and crash boot.
        _lark_ws_client.loop = serving_loop

        class _MgrStub(BridgeManager):
            def __init__(self):
                self._bridges = {}

        bridge = FeishuBridge(
            _MgrStub(),
            app_id="cli_fake",
            app_secret="fake_secret",
            transport="ws",
            domain=f"http://127.0.0.1:{dead_port}",
            allowed_open_ids=["ou_fake"],
        )
        # Skip the pre-connect identity probe (needs a real tenant) and fail on
        # the first connect instead of the SDK's 120s reconnect wait, so the
        # test is fast and deterministic. The loop-conflict path is unaffected.
        bridge._channel._fetch_bot_identity_sync = lambda: None
        bridge._channel._config.transport.auto_reconnect = False

        mgr = _MgrStub()
        mgr.register_bridge(bridge)
        try:
            # Swallows the ws connect failure, exactly like production boot.
            await mgr.start_all()
        except BaseException as e:  # noqa: BLE001 - capture anything for the assert
            state["start_error"] = f"{type(e).__name__}: {e}"
            raise
        state["reached_yield"] = True
        try:
            yield
        finally:
            try:
                await bridge.stop()
            except Exception:
                pass

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"ok": True}

    config = uvicorn.Config(
        app, host="127.0.0.1", port=serving_port, log_level="warning",
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="ws-smoke-uvicorn", daemon=True)
    thread.start()

    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            if server.started or not thread.is_alive():
                break
            time.sleep(0.05)

        # Pre-fix this is False: the uvicorn thread died with the loop RuntimeError.
        assert server.started, (
            f"server did not start (thread_alive={thread.is_alive()}, "
            f"start_error={state['start_error']})"
        )
        assert thread.is_alive(), "uvicorn thread died during ws-mode startup"
        assert state["reached_yield"], "lifespan never reached yield (startup aborted)"

        # The server loop must survive and serve requests.
        status = _get_no_proxy(f"http://127.0.0.1:{serving_port}/health", timeout=5)
        assert status == 200

        # Regression guard: the SDK's ws loop must have been rebound OFF the
        # server loop (the whole point of the fix).
        assert _lark_ws_client.loop is not state["serving_loop"]
        assert not _lark_ws_client.loop.is_closed()
    finally:
        server.should_exit = True
        thread.join(timeout=8)
        # Restore module state so the isolated loop the fix created doesn't leak
        # into other tests; close it if it isn't the one we started with.
        created = _lark_ws_client.loop
        _lark_ws_client.loop = original_ws_loop
        if created is not original_ws_loop and created is not None and not created.is_closed():
            try:
                created.close()
            except Exception:
                pass

    assert not thread.is_alive(), "uvicorn thread failed to shut down"
