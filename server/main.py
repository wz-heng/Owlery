import asyncio
import logging
import os
import signal
from contextlib import asynccontextmanager
from pathlib import Path

# Clear this so the `claude` CLI subprocess doesn't think it's nested
# inside another Claude Code session (which would change its behavior).
os.environ.pop("CLAUDECODE", None)

import uvicorn
from fastapi import Depends, FastAPI, Request, Response
from fastapi.routing import APIRoute
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
from .deploy import DeployLayout
from .legacy_rename import migrate_legacy_state, rewrite_legacy_paths
from .notifiers import notifier_manager
from .agent_manager import AgentManager
from .connector_manager import ConnectorManager
from .routers import agents, attachments, bg_tasks as bg_tasks_router, budgets as budgets_router, connectors, credentials, delegations as delegations_router, files, memory as memory_router, notifiers, questions, research as research_router, schedules, sessions, task_boards as task_boards_router, usage as usage_router, ws
from .parked_turns import ParkedTurnRunner
from .scheduler import ScheduleRunner
from .session_manager import session_manager
from .task_board import task_repository
from .task_board import workspaces as task_workspaces
from .task_board.manager import task_board_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _begin_deploy_probation(
    *,
    admission_gate,
    probation,
    run_probation,
    on_release,
) -> asyncio.Task:
    """Close admission before starting the boot-probation monitor.

    The helper makes the ordering explicit and testable: the monitor always
    observes a closed gate, while the first producer release observes it open.
    It returns the monitor task so lifespan can intentionally leave the bounded
    local poll off the startup critical path.
    """
    await admission_gate.close()

    async def _release() -> None:
        await admission_gate.open()
        await on_release()

    return asyncio.create_task(run_probation(probation, on_release=_release))


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
        admission_gate=session_manager.deploy_admission_gate,
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

    from .bridges.feishu import build_feishu_bridge

    # Fail-loud on half-config (one credential set), fail-closed on webhook
    # without a verification token — both raise here and abort boot (§4.1/4.2).
    feishu = build_feishu_bridge(bridge_manager, settings)
    if feishu is not None:
        bridge_manager.register_bridge(feishu)
        if feishu.transport == "webhook":
            # Only mounted in webhook mode WITH a verification token (guaranteed
            # by build_feishu_bridge). In ws mode, or unconfigured, this route
            # is never added — a bare POST falls through to the SPA catch-all
            # and is rejected without ever reaching bridge code (§4.2).
            async def feishu_webhook(request: Request) -> Response:
                body = await request.body()
                status, content = await feishu.handle_webhook(
                    dict(request.headers), body
                )
                return Response(
                    content=content,
                    status_code=status,
                    media_type="application/json",
                )

            # INSERT at the front of the router, not append: the SPA
            # `StaticFiles` catch-all is mounted at "/" at import time, and it
            # matches every path (returning 405 for a POST). Appending would
            # let that mount shadow this route; inserting ahead of it makes
            # POST /feishu/webhook resolve here first.
            app.router.routes.insert(
                0,
                APIRoute("/feishu/webhook", feishu_webhook, methods=["POST"]),
            )
            logger.info("Feishu webhook route mounted at /feishu/webhook")

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
    budgets_router._db = db
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

    # Git delivery recovery (task-git-delivery.md §16): interrupt in-flight
    # delivery ops, reset stuck baselines, and reconstruct terminal-delivery
    # notifications. DB-only — no hosting-platform I/O in this barrier. Also
    # reconciles any interrupted `deploy_switch` op from the switcher journal
    # (local-deploy.md §8); a flipped-but-unconfirmed boot returns a probation.
    probation = await task_board_manager.recover_deliveries()

    # Wire the deploy-switch effects that only exist in a live server
    # (local-deploy.md §7.2): the quiesce census/drain primitives, the
    # `server_restarting` broadcast, and the in-process graceful-shutdown trigger
    # (send ourselves the same SIGTERM uvicorn already turns into this teardown).
    task_board_manager.bind_deploy_switch(
        broadcast_restarting=session_manager._broadcast,
        request_shutdown=lambda: os.kill(os.getpid(), signal.SIGTERM),
        bg_task_manager=bg_task_manager,
        research_manager=research_manager,
        bridge_manager_getter=lambda: getattr(app.state, "bridge_manager", None),
        scheduler_getter=lambda: getattr(
            getattr(app.state, "schedule_runner", None), "_scheduler", None
        ),
        parked_scheduler_getter=lambda: getattr(
            getattr(app.state, "parked_turn_runner", None), "_scheduler", None
        ),
    )

    async def _release_producers() -> None:
        # Domain recovery is complete and all broadcast listeners are live. Drain
        # only after that barrier; a replay can immediately start a model turn, so
        # moving this earlier reintroduces the boot race. A due parked turn,
        # schedule, or bridge message can call start_message directly (outside the
        # injection outbox), so producers start only after the barrier too.
        await session_manager.deploy_admission_gate.open()
        await session_manager.resume_session_injection_dispatch()
        await schedule_runner.initialize()
        await parked_turn_runner.initialize()
        await bridge_manager.start_all()
        await task_board_manager.start()
        # Off the boot critical path (task-git-delivery.md §16, S3): a bounded,
        # read-only reconcile of any interrupted PR op. Fire-and-forget so it
        # never blocks the dispatcher, the injection drain, or any session's turn.
        asyncio.create_task(task_board_manager.reconcile_interrupted_prs())

    if probation is None:
        await _release_producers()
    else:
        # Boot probation (local-deploy.md §7.5): this server flipped but the
        # switcher has not yet confirmed health. Hold every producer paused — the
        # health window performs no user-visible work a snapshot restore could
        # undo — until the switcher's verdict lands (or the window elapses), then
        # release. /health is already served, so the switcher can confirm us.
        # This boot is already serving /health for the switcher, but must not
        # admit any new work that a rollback could invalidate.  Every producer
        # claims the same gate before becoming census-visible; release happens
        # only from the probation monitor's `on_release` callback above.
        logger.info(
            "deploy probation: holding producers until switch op %s settles",
            probation.op_id,
        )
        await _begin_deploy_probation(
            admission_gate=session_manager.deploy_admission_gate,
            probation=probation,
            run_probation=task_board_manager.run_deploy_probation,
            on_release=_release_producers,
        )

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
app.include_router(memory_router.router)
app.include_router(usage_router.router)
app.include_router(budgets_router.router)
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
    sha, slot = await _running_deploy_sha_slot()
    return {"status": "ok", "bridges": bridges_health, "sha": sha, "slot": slot}


async def _running_deploy_sha_slot() -> tuple[str | None, str | None]:
    """The build sha and slot this process is actually serving (local-deploy.md
    §6/§7.3 step 4): the switcher's `_fetch_health_sha` compares this `sha`
    against the handoff's `new_sha`/`old_sha` to confirm a flip (or a rollback
    flip-back) actually took effect, so it must reflect `current` at request
    time — not a DB row, which during probation still names the OLD live
    deployment until the switcher's terminal journal line reconciles it.

    `current_slot()` resolves the live symlink fresh on every call, and the
    sha is that slot's own git HEAD — exactly the commit `deploy_stage`
    detached-checked-out there (§5 step 2), so it equals the sha the deploy
    pipeline staged/switched to. None/None when local deploy is disabled or
    `current` does not resolve to a real slot (fail-closed, not a 500)."""
    root = settings.resolved_deploy_root
    if not root:
        return None, None
    layout = DeployLayout.at(root)
    slot = layout.current_slot()
    if slot is None:
        return None, None
    try:
        rc, out, _ = await task_workspaces._git(
            "rev-parse", "HEAD", cwd=str(layout.slot_path(slot))
        )
    except task_workspaces.WorkspaceError:
        return None, slot
    sha = out.strip() if rc == 0 and out else None
    return sha, slot


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
