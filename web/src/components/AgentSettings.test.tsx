/**
 * AgentSettings built-in tools (agent-collaboration.md §5.1,
 * native-deep-research.md §7). A new agent's form must default to — and
 * offer a toggle for — the FULL built-in MCP set, including `ask_agent`
 * (delegation) and `research`. A stale `["ask", "bg"]` list once left those
 * two off by default AND unrenderable, so an agent could be born without a
 * delegation channel and no one could turn it back on from the UI.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { AgentSettings } from "./AgentSettings";
import { useSessionStore } from "../stores/sessionStore";

beforeEach(() => {
  useSessionStore.setState({
    token: "tok",
    agents: [],
    credentials: [],
    connectorInstallations: [],
    agentConnectorIds: {},
    availableBackends: ["claude-code"],
  });
});

afterEach(() => {
  cleanup();
});

describe("AgentSettings built-in tools", () => {
  it("offers all four built-in MCP servers, checked by default for a new agent", () => {
    render(
      <AgentSettings open initialAgentId={null} onOpenChange={() => {}} />
    );

    for (const id of ["ask", "bg", "ask_agent", "research"]) {
      const box = screen.getByRole("checkbox", { name: id }) as HTMLInputElement;
      expect(box).toBeInTheDocument();
      expect(box.checked).toBe(true);
    }
  });
});
