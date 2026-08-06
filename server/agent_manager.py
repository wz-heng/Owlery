"""AgentManager — stateless CRUD + business rules over the `agents` table.

Agents are the durable definition of an assistant (agent-refactor.md §5.1):
pure DB rows, no in-memory subprocess. This layer enforces name uniqueness
and the "has live sessions → archive, don't delete" guard for the routes.
No agent is protected: every agent can be archived, and an agent with no
sessions can be deleted (agent-identity.md).
SessionManager reads agent rows directly through the Database at spawn time
(so editing an agent affects its open sessions on their next turn); it does
not go through this manager.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from . import agent_memory
from .database import Database
from .model_routing import validate_model_for_backend


class AgentError(Exception):
    """Agent business-rule violation. Routes map this to a 400/409."""


class AgentManager:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def list_agents(
        self, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        return await self.db.load_agents(include_archived=include_archived)

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return await self.db.get_agent(agent_id)

    async def get_default_agent(self) -> dict[str, Any] | None:
        """Some agent to fall back on when none is supplied — the oldest live
        agent (agent-identity.md). None only if there are no agents at all."""
        return await self.db.get_default_agent()

    async def create_agent(
        self,
        *,
        name: str,
        description: str = "",
        system_prompt: str = "",
        model: str | None = None,
        credential_id: str | None = None,
        backend: str = "claude-code",
        mcp_servers: list[str] | None = None,
        tool_allow: str = "",
        tool_deny: str = "",
    ) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise AgentError("Agent name is required")
        if await self.db.get_agent_by_name(name) is not None:
            raise AgentError(f"An agent named {name!r} already exists")
        # Reject a model the backend can't run (budget-model-routing.md §4.3).
        validate_model_for_backend(backend, model)
        agent_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        await self.db.save_agent(
            agent_id=agent_id,
            name=name,
            created_at=now,
            updated_at=now,
            description=description,
            system_prompt=system_prompt,
            model=model,
            credential_id=credential_id,
            backend=backend,
            mcp_servers=mcp_servers,
            tool_allow=tool_allow,
            tool_deny=tool_deny,
        )
        agent = await self.db.get_agent(agent_id)
        assert agent is not None
        # Provision the agent's canonical memory/ dir up front; also ensured
        # lazily per turn.
        agent_memory.ensure_agent_dirs(agent_id)
        return agent

    async def update_agent(self, agent_id: str, **fields: Any) -> dict[str, Any]:
        agent = await self.db.get_agent(agent_id)
        if agent is None:
            raise AgentError("Agent not found")
        if "name" in fields and fields["name"] is not None:
            new_name = fields["name"].strip()
            if not new_name:
                raise AgentError("Agent name cannot be empty")
            clash = await self.db.get_agent_by_name(new_name)
            if clash is not None and clash["id"] != agent_id:
                raise AgentError(f"An agent named {new_name!r} already exists")
            fields["name"] = new_name
        # Validate the RESULTING (backend, model) pair — a PATCH that changes
        # only the backend must still be checked against the existing model,
        # and vice-versa (budget-model-routing.md §4.3).
        #
        # backend uses `or`, not "in fields": it's a non-null column, so the DB
        # ignores a null backend update (keeps the existing one). An explicit
        # `{"backend": null, "model": "gpt-5"}` would otherwise validate against
        # None (→ passes) while the model still persists onto the kept backend.
        # model uses "in fields" because it IS nullable — an explicit null
        # clears it, and we validate the new (possibly-cleared) value.
        eff_backend = fields.get("backend") or agent.get("backend")
        eff_model = fields.get("model") if "model" in fields else agent.get("model")
        validate_model_for_backend(eff_backend, eff_model)
        await self.db.update_agent(agent_id, **fields)
        updated = await self.db.get_agent(agent_id)
        assert updated is not None
        return updated

    async def archive_agent(self, agent_id: str) -> None:
        agent = await self.db.get_agent(agent_id)
        if agent is None:
            raise AgentError("Agent not found")
        await self.db.archive_agent(agent_id)

    async def delete_agent(self, agent_id: str) -> None:
        agent = await self.db.get_agent(agent_id)
        if agent is None:
            raise AgentError("Agent not found")
        if await self.db.count_sessions_for_agent(agent_id) > 0:
            raise AgentError(
                "Agent still has sessions; archive it instead of deleting"
            )
        await self.db.delete_agent(agent_id)
        # Hard delete also removes the agent's memory dir. Archiving keeps it,
        # mirroring archived-session history.
        agent_memory.remove_agent_dir(agent_id)
