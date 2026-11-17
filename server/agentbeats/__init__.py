"""AgentBeats A2A adapter — Owlery as a purple (competitor) agent on the
AgentBeats platform's Terminal Bench 2.0 green agent.

See docs/plans/agentbeats-a2a-adapter.md for the full design. In short:

  A2A request → `executor.py` (a2a-sdk `AgentExecutor`) → `manager.py`
  (`AgentBeatsExamManager`, drives a real `SessionManager`/`HarnessRun`
  turn per A2A round through the `tbexec` MCP tool) → `app.py` (mounts the
  A2A server into the main FastAPI app / standalone entrypoint for the
  Docker image).
"""
