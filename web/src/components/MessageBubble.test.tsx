/**
 * MessageBubble's identity seal (agent-identity.md): an assistant turn is
 * sealed in its OWNING agent's wax — the same deterministic colour the rail
 * and headers use, so one agent looks identical everywhere. A session with no
 * owning agent falls back to the neutral ink wax.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { MessageBubble } from "./MessageBubble";
import { AgentSeal } from "./ui/seal";
import { waxColorForId } from "../lib/seal";
import type { Message } from "../stores/sessionStore";

afterEach(cleanup);

const assistantText = (content: string): Message =>
  ({ type: "text", role: "assistant", content } as Message);

const rimStyle = (c: HTMLElement) =>
  c.querySelector(".seal-rim")?.getAttribute("style") ?? "";

describe("MessageBubble identity seal", () => {
  it("seals an assistant turn in its agent's wax — identical to the rail seal", () => {
    const { container: bubble } = render(
      <MessageBubble
        message={assistantText("hello")}
        sessionId="s1"
        agentName="Dobby"
        agentId="agent-dobby"
      />
    );
    const wax = waxColorForId("agent-dobby");
    expect(rimStyle(bubble)).toContain(wax);

    // The rail/header seal for the SAME agent uses the very same wax.
    const { container: rail } = render(
      <AgentSeal agent={{ id: "agent-dobby", name: "Dobby" }} />
    );
    expect(rimStyle(rail)).toContain(wax);
  });

  it("falls back to the neutral ink wax when the session has no owning agent", () => {
    const { container } = render(
      <MessageBubble message={assistantText("hi")} sessionId="s1" />
    );
    // No agent id → the seal's default ink wax, never a --wax-* identity colour.
    expect(rimStyle(container)).not.toContain("--wax-");
  });
});
