# AgentBeats purple-agent image for Owlery's Terminal Bench 2.0 adapter
# (docs/plans/agentbeats-a2a-adapter.md). NOT built or pushed by any CI —
# hand-build only, and only for local validation
# (docs/plans/agentbeats-phase1.md's "不做清单": no registration, no
# publishing, in this phase).
#
# This is the *whole* Owlery backend, not a special-purpose exam shell: the
# A2A endpoint is one more route on `server.main:app` (server/main.py mounts
# it at /a2a). No secret of any kind is baked in here — the model API key is
# supplied at container *runtime* via the ANTHROPIC_API_KEY env var (wired
# through amber-manifest.json5's config_schema, §4 of the plan doc), and
# Claude Code's own subprocess spawn inherits the ambient process env
# (server/harness/run.py: `os.environ.copy()`), so no Owlery DB credential
# row is involved at all.
#
# Build (never pushed):
#   docker build -f deploy/agentbeats/Dockerfile -t owlery-agentbeats:local .
# Run locally:
#   docker run --rm -p 9010:9010 -e ANTHROPIC_API_KEY=sk-... owlery-agentbeats:local

FROM python:3.12-slim

# Claude Code CLI (npm) — the harness spawns this binary per turn
# (server/harness/claude_code.py). Node is only needed to install it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y gnupg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY server ./server
# No `web/dist/`: the purple agent serves only the A2A endpoint, not the
# chat UI, so the "/" SPA StaticFiles mount in main.py never registers
# (it's conditional on `web/dist` existing) — no ordering concern.
RUN pip install --no-cache-dir -e .

# The exam agent's generated adapter never carries a credential_id; the
# model's API key must arrive as a plain runtime env var (never baked in).
ENV ANTHROPIC_API_KEY=""
ENV OWLERY_HOME_DIR=/data
ENV OWLERY_PORT=9010
ENV OWLERY_HOST=0.0.0.0
# Loopback shared secret between this process and its own spawned MCP
# tool subprocesses (tbexec/bg/ask/...) — not reachable from outside the
# container, so a fixed default is fine; override if paranoid.
ENV OWLERY_AUTH_TOKEN=agentbeats-local

VOLUME ["/data"]
EXPOSE 9010

ENTRYPOINT ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "9010"]
