"""Builds the A2A sub-application mounted into Owlery's main FastAPI app at
`/a2a` (wired in `server/main.py`). There is no separate standalone process:
the Docker image for AgentBeats submission (deploy/agentbeats/) runs the
*entire* Owlery backend (`uvicorn server.main:app`) — the A2A endpoint is
just one more route tree on the same app/port, so "the real stack" is
literally true rather than a slogan (agentbeats-a2a-adapter.md §1).
"""

from __future__ import annotations

from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from starlette.applications import Starlette

from .executor import OwleryExamExecutor
from .manager import AgentBeatsExamManager


def a2a_mount_path_and_card_url(*, dist_dir_exists: bool, port: int) -> tuple[str, str]:
    """Where to mount the A2A sub-app, and what URL to advertise in its
    agent card, as a function of whether `web/dist/` exists (server/main.py).

    Root "/" when there's no SPA to coexist with (the real AgentBeats
    submission image never copies `web/dist/` in) — confirmed against the
    real terminal-bench-green reference agent, not just assumed: its
    a2a-sdk client doesn't follow redirects, and Starlette's
    ``Mount(path="/a2a")`` 307-redirects a request to the bare "/a2a" (no
    trailing slash) to "/a2a/", which that client treats as a hard failure.
    Root-mounting needs no such trailing-slash normalization. Dev mode
    (web/dist present) keeps the "/a2a" sub-path to coexist with the SPA at
    "/" — the card URL there is `/a2a/`, *with* the trailing slash, so
    anyone who calls back using the card's own advertised address (as the
    real reference agent's messenger.py does) lands on it directly rather
    than depending on a redirect too; the mount path argument to
    `app.mount()` itself doesn't need the slash (Starlette strips it).
    """
    if dist_dir_exists:
        return "/a2a", f"http://127.0.0.1:{port}/a2a/"
    return "/", f"http://127.0.0.1:{port}"


def build_a2a_app(manager: AgentBeatsExamManager, *, card_url: str | None = None) -> Starlette:
    """`card_url` is the externally-reachable base URL to advertise in the
    agent card (e.g. ``http://127.0.0.1:8000/a2a``); omit for relative
    discovery when the caller resolves the card against the request URL."""
    skill = AgentSkill(
        id="terminal-bench-2-purple",
        name="Owlery Terminal Bench 2.0 Agent",
        description=(
            "Owlery's real session/harness engine, exposed as an A2A "
            "purple (competitor) agent for Terminal Bench 2.0 evaluations "
            "on AgentBeats. Speaks the terminal-bench-shell-v1 protocol: "
            "one shell-command decision per A2A round, relayed to a "
            "throwaway, no-memory exam agent."
        ),
        tags=["evaluation", "benchmark", "terminal"],
        examples=[],
    )
    agent_card = AgentCard(
        name="Owlery (exam agent)",
        description=(
            "General-purpose AI coding assistant, evaluated via Owlery's "
            "real harness with no task-specific tuning."
        ),
        url=card_url or "/",
        version="1.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=False),
        skills=[skill],
    )
    request_handler = DefaultRequestHandler(
        agent_executor=OwleryExamExecutor(manager),
        task_store=InMemoryTaskStore(),
    )
    a2a_app = A2AStarletteApplication(agent_card=agent_card, http_handler=request_handler)
    return a2a_app.build()
