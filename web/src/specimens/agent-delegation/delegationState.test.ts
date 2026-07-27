import { beforeEach, describe, expect, it } from "vitest";

import { parseDelegationEvent } from "../../components/AgentDelegationEventCard";
import { useSessionStore } from "../../stores/sessionStore";
import {
  applyDelegationSpecimenEvent,
  resetDelegationSpecimenStore,
} from "./delegationState";
import {
  DOBBY_SESSION_ID,
  PARENT_SESSION_ID,
  RESEARCH_SESSION_ID,
} from "./scripts";

describe("delegation specimen state", () => {
  beforeEach(resetDelegationSpecimenStore);

  it("uses the child session id as the delegation identity", () => {
    applyDelegationSpecimenEvent({
      type: "delegation_started",
      actor: "Aberforth",
      delegationId: DOBBY_SESSION_ID,
      target: "Dobby",
      content: "Review the stream.",
    });

    const store = useSessionStore.getState();
    const delegation = store.delegations[PARENT_SESSION_ID][0];
    const child = store.sessions.find((item) => item.id === DOBBY_SESSION_ID);

    expect(delegation.delegation_id).toBe(child?.id);
    expect(delegation.sub_session_id).toBe(child?.id);
    expect(child?.parent_session_id).toBe(PARENT_SESSION_ID);
  });

  it("injects a production-shaped reply that the real card parser accepts", () => {
    applyDelegationSpecimenEvent({
      type: "delegation_started",
      actor: "Aberforth",
      delegationId: DOBBY_SESSION_ID,
      target: "Dobby",
      content: "Review the stream.",
    });
    applyDelegationSpecimenEvent({
      type: "child_reply",
      actor: "Owlery",
      delegationId: DOBBY_SESSION_ID,
      target: "Dobby",
      content: "Review complete.",
    });

    const message = useSessionStore.getState().messages[PARENT_SESSION_ID].at(-1);
    expect(parseDelegationEvent(message?.content)).toMatchObject({
      kind: "reply",
      agentName: "Dobby",
      delegationId: DOBBY_SESSION_ID,
      body: "Review complete.",
    });
  });

  it("keeps a nested delegation attached to its immediate caller", () => {
    applyDelegationSpecimenEvent({
      type: "nested_started",
      actor: "Dobby",
      delegationId: RESEARCH_SESSION_ID,
      target: "Researcher",
      content: "Verify the evidence.",
    });

    const nested = useSessionStore.getState().delegations[DOBBY_SESSION_ID][0];
    expect(nested.parent_session_id).toBe(DOBBY_SESSION_ID);
    expect(nested.delegation_id).toBe(RESEARCH_SESSION_ID);
  });
});
