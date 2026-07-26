import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

# Clear this so the `claude` CLI subprocess doesn't think it's nested
# inside another Claude Code session (which would change its behavior).
os.environ.pop("CLAUDECODE", None)

import uvicorn
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .auth import verify_token

from .bg_tasks import bg_task_manager
from .delegations import delegation_manager
from .research import research_manager
from .bridges.manager import BridgeManager
from .config import settings
from .tunnel import CloudflareTunnel
from .database import Database
from .legacy_rename import migrate_legacy_state, rewrite_legacy_paths
from .notifiers import notifier_manager
from .agent_manager import AgentManager
from .connector_manager import ConnectorManager
from .routers import agents, attachments, bg_tasks as bg_tasks_router, connectors, credentials, delegations as delegations_router, files, notifiers, questions, research as research_router, schedules, sessions, task_boards as task_boards_router, usage as usage_router, ws
from .parked_turns import ParkedTurnRunner
from .scheduler import ScheduleRunner
from .session_manager import session_manager
from .task_board import task_repository
from .task_board.manager import task_board_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Octopus → Owlery (rename-owlery.md §3). Moves ~/.octopus and octopus.db
    # into place BEFORE anything opens the DB or provisions agent dirs; the
    # stored-path rewrite then needs the open connection. Both are no-ops once
    # the migration has run.
    migrate_legacy_state(settings)

    db = Database(settings.db_path)
    await db.initialize()
    task_repository.bind(settings.db_path)
    await task_repository.initialize()
    await rewrite_legacy_paths(db, settings)
    await session_manager.initialize(db)
    # Boot is a two-phase recovery. Domain managers may create/repair durable
    # outbox intents below, but no consuming model turn may start until every
    # listener is bound and every prior-process execution state is reconciled.
    session_manager.pause_session_injection_dispatch()
    bg_task_manager.bind(
        db=db,
        deliver_cb=session_manager.deliver_bg_result,
        broadcast_cb=session_manager._broadcast,
    )
    delegation_manager.bind(session_mgr=session_manager, db=db)
    research_manager.bind(session_mgr=session_manager, db=db)
    task_board_manager.bind(session_mgr=session_manager, db=db)
    task_boards_router.set_manager(task_board_manager)
    app.state.task_board_manager = task_board_manager

    # Initialize bridge manager
    bridge_manager = BridgeManager(session_manager, db)
    await bridge_manager.initialize()
    await bridge_manager.register_broadcast()

    if settings.telegram_bot_token:
        from .bridges.telegram import TelegramBridge

        telegram = TelegramBridge(
            bridge_manager,
            token=settings.telegram_bot_token,
            allowed_chat_ids=settings.telegram_allowed_chat_ids or None,
            api_base_url=settings.telegram_api_base_url,
        )
        bridge_manager.register_bridge(telegram)

    app.state.bridge_manager = bridge_manager

    # Initialize scheduler
    schedule_runner = ScheduleRunner(session_manager, db)
    app.state.schedule_runner = schedule_runner
    session_manager.set_schedule_runner(schedule_runner)
    schedules._db = db
    schedules._runner = schedule_runner

    # Usage-limit parks (limit-auto-resume.md §4). Rebuilds the wake-up jobs a
    # restart destroyed — a park is a multi-hour wait, so the DB records are the
    # source of truth and the scheduler jobs are derived state.
    parked_turn_runner = ParkedTurnRunner(session_manager, db)
    app.state.parked_turn_runner = parked_turn_runner
    session_manager.set_parked_turn_runner(parked_turn_runner)
    sessions._parked_turns = parked_turn_runner
    usage_router._db = db
    agents.set_manager(AgentManager(db))
    connectors.set_manager(ConnectorManager(db))
    credentials.set_db(db)
    notifiers.set_db(db)
    notifier_manager.set_db(db)
    session_manager.set_notifier_manager(notifier_manager)
    await notifier_manager.load()

    # Bg task worker — lives in this FastAPI process so spawned
    # subprocesses survive any per-turn `claude --print` lifetime. The
    # deliver callback synthesizes a user message into the session; the
    # broadcast callback pushes status events to all WS clients.
    await bg_task_manager.start()

    # Native deep research (native-deep-research.md). Tracks research jobs as
    # async tasks; injects the final report back into the session.
    await research_manager.recover_interrupted()

    # Task workers are multi-turn transient sessions. Make their durable runs
    # truthful before nested delegation recovery, but keep the worker sessions
    # live until descendant events have been transcript-materialized.
    await task_board_manager.recover_phase1()

    # Delegation recovery runs last among domain recoveries because it
    # transcript-materializes every pending bg/research/child event aimed at a
    # delegation parent before archiving the interrupted delegation tree.
    await delegation_manager.recover_interrupted()

    # Repair cross-connection terminal/outbox gaps, materialize any remaining
    # worker-directed events without restarting models, then archive workers.
    await task_board_manager.recover_phase2()

    # Domain recovery is now complete and all broadcast listeners are live.
    # Drain only after that barrier; a replay can immediately start a model
    # turn, so moving this earlier reintroduces the boot race.
    await session_manager.resume_session_injection_dispatch()

    # Only now start autonomous producers. A due parked turn, schedule, or
    # bridge message can call start_message directly (outside the injection
    # outbox), so starting any of them before the recovery barrier would bypass
    # paused injection dispatch.
    await schedule_runner.initialize()
    await parked_turn_runner.initialize()
    await bridge_manager.start_all()
    await task_board_manager.start()

    # Start Cloudflare Tunnel if enabled
    tunnel: CloudflareTunnel | None = None
    if settings.enable_tunnel:
        tunnel = CloudflareTunnel(port=settings.port)
        url = await tunnel.start()
        if url:
            print("\n" + "=" * 60)
            print(f"  Tunnel URL: {url}")
            print("=" * 60 + "\n")
            logger.info("Cloudflare Tunnel active: %s", url)

    yield

    # No system-produced result may start a fresh model turn once teardown
    # begins. Producers below may still persist terminal state/outbox rows;
    # those pending intents are deliberately replayed on the next boot.
    session_manager.pause_session_injection_dispatch()
    await task_board_manager.shutdown()
    if tunnel:
        await tunnel.stop()

    # Clean up any in-flight OAuth login subprocesses before we tear down DB.
    from .oauth_login import oauth_login_manager
    await oauth_login_manager.shutdown()
    from .codex_login import codex_login_manager
    await codex_login_manager.shutdown()

    await schedule_runner.shutdown()
    await parked_turn_runner.shutdown()
    await bridge_manager.stop_all()
    await bridge_manager.unregister_broadcast()
    await research_manager.shutdown()
    await bg_task_manager.shutdown()
    delegation_manager.shutdown()
    await session_manager.shutdown_session_injections()
    await task_repository.close()
    await db.close()


app = FastAPI(title="Owlery", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(agents.router)
app.include_router(sessions.router)
app.include_router(attachments.router)
app.include_router(files.router)
app.include_router(bg_tasks_router.router)
app.include_router(delegations_router.router)
app.include_router(research_router.router)
app.include_router(task_boards_router.router)
app.include_router(questions.router)
app.include_router(schedules.router)
app.include_router(usage_router.router)
app.include_router(credentials.router)
app.include_router(connectors.router)
app.include_router(connectors.agent_router)
app.include_router(notifiers.router)
app.include_router(ws.router)


@app.get("/api/backends")
async def list_backends(_: str = Depends(verify_token)):
    """Which AI backends are usable on this host (codex-backend.md §6.1).
    A harness kind appears only when its CLI resolves on PATH. `claude-code`
    is always listed (the default) even if not yet installed, matching the
    historical contract."""
    from .harness import available_backends

    available = available_backends()
    if "claude-code" not in available:
        available = ["claude-code", *available]
    return {"available": available}


@app.get("/health")
async def health():
    bridges_health = {}
    if hasattr(app.state, "bridge_manager"):
        for name, bridge in app.state.bridge_manager._bridges.items():
            bridges_health[name] = {"healthy": bridge.healthy}
    return {"status": "ok", "bridges": bridges_health}


# Serve built frontend as static files (SPA catch-all).
# Mounted after API routes so /api/*, /ws, /health take priority.
_dist_dir = Path(__file__).resolve().parent.parent / "web" / "dist"
if _dist_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_dist_dir), html=True), name="spa")


def run():
    uvicorn.run(
        "server.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    run()
