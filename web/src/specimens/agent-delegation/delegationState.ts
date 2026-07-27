import {
  useSessionStore,
  type Delegation,
  type SessionInfo,
} from "../../stores/sessionStore";
import {
  DOBBY_SESSION_ID,
  PARENT_SESSION_ID,
  RESEARCH_SESSION_ID,
  type DelegationSpecimenEvent,
} from "./scripts";

const session = (
  id: string,
  name: string,
  agentId: string | null,
  parentSessionId: string | null,
  request: string | null
): SessionInfo => ({
  id,
  name,
  working_dir: "/owlery/specimens/delegation",
  status: "idle",
  created_at: "2026-07-21T00:00:00.000Z",
  message_count: 0,
  claude_session_id: null,
  credential_id: null,
  agent_id: agentId,
  origin: parentSessionId ? "delegation" : "user",
  backend: "claude-code",
  parent_session_id: parentSessionId,
  delegation_request: request,
  can_fork: false,
  fork_is_full_copy: false,
  archived: false,
});

export function resetDelegationSpecimenStore(): void {
  useSessionStore.setState({
    sessions: [
      session(PARENT_SESSION_ID, "Delegation laboratory", "agent-aberforth", null, null),
      session(DOBBY_SESSION_ID, "Delegated to Dobby", "agent-dobby", PARENT_SESSION_ID, "Specimen task"),
      session(RESEARCH_SESSION_ID, "Delegated to Researcher", "agent-researcher", DOBBY_SESSION_ID, "Evidence request"),
    ],
    archivedSessions: [],
    activeSessionId: PARENT_SESSION_ID,
    messages: {
      [PARENT_SESSION_ID]: [],
      [DOBBY_SESSION_ID]: [],
      [RESEARCH_SESSION_ID]: [],
    },
    delegations: {},
    pendingQuestions: {},
    pendingQueue: {},
  });
}

function recordFor(
  id: string,
  parentId: string,
  target: string,
  request: string
): Delegation {
  return {
    delegation_id: id,
    sub_session_id: id,
    parent_session_id: parentId,
    target_agent_id: target === "Researcher" ? "agent-researcher" : "agent-dobby",
    target_agent_name: target,
    request,
    state: "running",
    created_at: "2026-07-21T00:00:00.000Z",
    finished_at: null,
    error: null,
  };
}

function existingDelegation(parentId: string, id: string): Delegation | undefined {
  return (useSessionStore.getState().delegations[parentId] || []).find(
    (item) => item.delegation_id === id
  );
}

export function applyDelegationSpecimenEvent(
  event: DelegationSpecimenEvent
): void {
  const store = useSessionStore.getState();
  switch (event.type) {
    case "parent_prompt":
      store.addMessage(PARENT_SESSION_ID, {
        role: "user",
        type: "text",
        content: event.content,
      });
      break;

    case "delegation_started": {
      const id = event.delegationId ?? DOBBY_SESSION_ID;
      store.upsertDelegation(
        PARENT_SESSION_ID,
        recordFor(id, PARENT_SESSION_ID, event.target ?? "Dobby", event.content ?? "")
      );
      break;
    }

    case "child_running":
      store.updateSessionStatus(event.delegationId ?? DOBBY_SESSION_ID, "running");
      break;

    case "child_text":
      store.addMessage(DOBBY_SESSION_ID, {
        role: "assistant",
        type: "text",
        content: event.content,
      });
      break;

    case "child_question": {
      store.addMessage(DOBBY_SESSION_ID, {
        role: "assistant",
        type: "text",
        content: event.content,
      });
      store.addMessage(PARENT_SESSION_ID, {
        role: "user",
        type: "text",
        content: `[agent-question:Dobby delegation=${event.delegationId} question_id=${event.questionId}]\n${event.content}`,
      });
      break;
    }

    case "parent_answer":
      store.addMessage(PARENT_SESSION_ID, {
        role: "assistant",
        type: "text",
        content: event.content,
      });
      store.addMessage(DOBBY_SESSION_ID, {
        role: "user",
        type: "text",
        content: `[principal-answer question_id=${event.questionId}] ${event.content}`,
      });
      break;

    case "nested_started": {
      const id = event.delegationId ?? RESEARCH_SESSION_ID;
      store.upsertDelegation(
        DOBBY_SESSION_ID,
        recordFor(id, DOBBY_SESSION_ID, event.target ?? "Researcher", event.content ?? "")
      );
      store.updateSessionStatus(id, "running");
      break;
    }

    case "nested_reply": {
      const id = event.delegationId ?? RESEARCH_SESSION_ID;
      const current = existingDelegation(DOBBY_SESSION_ID, id);
      if (current) {
        store.upsertDelegation(DOBBY_SESSION_ID, {
          ...current,
          state: "completed",
          finished_at: "2026-07-21T00:00:05.000Z",
        });
      }
      store.updateSessionStatus(id, "idle");
      store.addMessage(RESEARCH_SESSION_ID, {
        role: "assistant",
        type: "text",
        content: event.content,
      });
      store.addMessage(DOBBY_SESSION_ID, {
        role: "user",
        type: "text",
        content: `[agent-reply:Researcher delegation=${id}]\n${event.content}`,
      });
      break;
    }

    case "child_reply": {
      const id = event.delegationId ?? DOBBY_SESSION_ID;
      const current = existingDelegation(PARENT_SESSION_ID, id);
      if (current) {
        store.upsertDelegation(PARENT_SESSION_ID, {
          ...current,
          state: "completed",
          finished_at: "2026-07-21T00:00:08.000Z",
        });
      }
      store.updateSessionStatus(id, "idle");
      store.addMessage(PARENT_SESSION_ID, {
        role: "user",
        type: "text",
        content: `[agent-reply:${event.target ?? "Dobby"} delegation=${id}]\n${event.content}`,
      });
      break;
    }

    case "child_error":
    case "child_cancelled": {
      const id = event.delegationId ?? DOBBY_SESSION_ID;
      const current = existingDelegation(PARENT_SESSION_ID, id);
      const reason = event.reason ?? (event.type === "child_cancelled" ? "cancelled" : "child error");
      if (current) {
        store.upsertDelegation(PARENT_SESSION_ID, {
          ...current,
          state: event.type === "child_cancelled" ? "cancelled" : "failed",
          finished_at: "2026-07-21T00:00:06.000Z",
          error: reason,
        });
      }
      store.updateSessionStatus(id, "idle");
      store.addMessage(PARENT_SESSION_ID, {
        role: "user",
        type: "text",
        content: `[agent-error:${event.target ?? "Dobby"} delegation=${id} reason=${reason}]\n${event.content}`,
      });
      break;
    }
  }
}
