#!/usr/bin/env python3
"""Stand-in for the AgentBeats gateway: sends an EvalRequest to a running
`terminal-bench-green` A2A server, pointing it at an Owlery A2A adapter
instance as the purple (competitor) agent.

Reproduces the AgentBeats Phase 1 ten-task local validation
(docs/plans/agentbeats-phase1.md, docs/plans/agentbeats-ten-tasks.json):

  1. Start colima/docker if not already running.
  2. Clone RDI-Foundation/terminal-bench-green @ dcaba23f and, in the
     clone's Dockerfile, swap the `get.docker.com` install step for the
     Debian `docker.io` package plus a separately-fetched `docker compose`
     v2 CLI plugin binary (neither ships with `docker.io` on bookworm, and
     Harbor's `DockerEnvironment` hardcodes v2 compose syntax,
     e.g. `--project-name`) — see docs/plans/agentbeats-a2a-adapter.md §5
     for the exact fix and why `get.docker.com` itself doesn't reach
     github.com without an explicit proxy build-arg from this network.
     Then: `docker build -t terminal-bench-green:latest .`
  3. Run it with DOOD wiring:
       docker run -d --name tbgreen -p 9009:9009 \
         --add-host=host.docker.internal:host-gateway --group-add <docker-gid> \
         -e WORKSPACE=/workspace -e DOCKER_HOST=unix:///var/run/docker.sock \
         -v /var/run/docker.sock:/var/run/docker.sock -v /workspace:/workspace \
         terminal-bench-green:latest --host 0.0.0.0 --port 9009
     (`<docker-gid>` = the *host* docker.sock's group id, so the
     container's own `agent` user — created with a different gid at image
     build time — can still open it; `colima ssh -- getent group docker`.)
  4. Start Owlery itself from a checkout with no `web/dist/` (so the A2A
     adapter mounts at root, matching the real submission image —
     docs/plans/agentbeats-a2a-adapter.md §2.6/§3):
       OWLERY_HOME_DIR=/tmp/agentbeats-work/owlery-home \
       OWLERY_LEGACY_HOME_DIR= OWLERY_PORT=9010 \
       .venv/bin/uvicorn server.main:app --host 127.0.0.1 --port 9010
  5. Run this script against both:
       .venv/bin/python scripts/agentbeats_drive_green.py \
         http://127.0.0.1:9009 http://host.docker.internal:9010 \
         build-cython-ext,configure-git-webserver,... 7200

Mirrors green's own `src/messenger.py` `send_message()` 1:1 (same
a2a-sdk version pin, 0.3.20) so the request/response is exactly what a
real green agent would produce — including the `agent_card.url` override
below, which green's own messenger.py also does: its agent card
advertises whatever `--host` it was started with (`0.0.0.0` in step 3),
which isn't itself a connectable address.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from uuid import uuid4

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import DataPart, Message, Part, Role, TextPart


def create_message(text: str) -> Message:
    return Message(
        kind="message",
        role=Role.user,
        parts=[Part(TextPart(kind="text", text=text))],
        message_id=uuid4().hex,
    )


def merge_parts(parts) -> str:
    chunks = []
    for part in parts:
        if isinstance(part.root, TextPart):
            chunks.append(part.root.text)
        elif isinstance(part.root, DataPart):
            chunks.append(json.dumps(part.root.data, indent=2))
    return "\n".join(chunks)


async def main() -> None:
    green_url = sys.argv[1]
    purple_url = sys.argv[2]
    task_names = sys.argv[3].split(",")
    timeout = int(sys.argv[4]) if len(sys.argv) > 4 else 600

    config = {"tasks": task_names} if len(task_names) > 1 else {"task": task_names[0]}
    eval_request = json.dumps({
        "participants": {"agent": purple_url},
        "config": config,
    })

    def ts() -> str:
        return time.strftime("%H:%M:%S")

    async with httpx.AsyncClient(timeout=timeout) as httpx_client:
        resolver = A2ACardResolver(httpx_client=httpx_client, base_url=green_url)
        agent_card = await resolver.get_agent_card()
        agent_card.url = green_url  # card may advertise an unroutable bind address
        client_config = ClientConfig(httpx_client=httpx_client, streaming=True)
        factory = ClientFactory(client_config)
        client = factory.create(agent_card)

        outbound_msg = create_message(eval_request)
        async for event in client.send_message(outbound_msg):
            match event:
                case Message() as msg:
                    print(f"[{ts()}][message]", merge_parts(msg.parts), flush=True)
                case (task, _update):
                    status = task.status
                    print(
                        f"[{ts()}][status={status.state.value}]",
                        merge_parts(status.message.parts) if status.message else "",
                        flush=True,
                    )
                    if task.artifacts:
                        for artifact in task.artifacts:
                            print(f"[{ts()}][artifact]", merge_parts(artifact.parts), flush=True)
                case _:
                    print(f"[{ts()}][other]", repr(event), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
