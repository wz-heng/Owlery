import { applyWsEvent } from "../../hooks/useWebSocket";
import { useSessionStore, type BgTask, type SessionInfo } from "../../stores/sessionStore";
import { BG_COMMAND, BG_SESSION_ID, BG_TASK_ID, type BgSpecimenEvent } from "./scripts";

const SESSION: SessionInfo = {
  id: BG_SESSION_ID,
  name: "Background task pipeline",
  working_dir: "/owlery/specimens/background",
  status: "idle",
  created_at: "2026-07-22T00:00:00.000Z",
  message_count: 0,
  claude_session_id: null,
  credential_id: null,
  agent_id: "agent-aberforth",
  origin: "specimen",
  backend: "claude-code",
  can_fork: false,
  fork_is_full_copy: false,
  archived: false,
};

export function resetBgSpecimenStore(): void {
  useSessionStore.setState({
    sessions: [{ ...SESSION }],
    archivedSessions: [],
    activeSessionId: BG_SESSION_ID,
    messages: { [BG_SESSION_ID]: [] },
    bgTasks: { [BG_SESSION_ID]: [] },
    pendingQueue: {},
    pendingQuestions: {},
  });
}

function currentTask(): BgTask | undefined {
  return useSessionStore.getState().bgTasks[BG_SESSION_ID]?.find((task) => task.id === BG_TASK_ID);
}

export function applyBgSpecimenEvent(event: BgSpecimenEvent): void {
  const store = useSessionStore.getState();
  switch (event.type) {
    case "user_prompt":
      store.addMessage(BG_SESSION_ID, { role: "user", type: "text", content: event.content });
      break;
    case "tool_use":
      store.addMessage(BG_SESSION_ID, {
        role: "assistant",
        type: "tool_use",
        tool_name: "mcp__bg__run",
        tool_use_id: "tool-bg-specimen",
        tool_input: { command: BG_COMMAND, description: "full deterministic E2E" },
      });
      break;
    case "bg_started":
      applyWsEvent({
        type: "bg_started",
        session_id: BG_SESSION_ID,
        task_id: BG_TASK_ID,
        command: BG_COMMAND,
        description: "full deterministic E2E",
        started_at: "2026-07-22T00:00:01.000Z",
      });
      break;
    case "turn_closed":
      store.addMessage(BG_SESSION_ID, { role: "assistant", type: "text", content: "测试已转入后台。我先结束这一轮，完成时会自动回来。" });
      break;
    case "bg_completed":
      applyWsEvent({
        type: "bg_completed",
        session_id: BG_SESSION_ID,
        task_id: BG_TASK_ID,
        status: event.status,
        exit_code: event.exitCode,
        truncated: !!event.truncated,
        completed_at: "2026-07-22T00:01:38.000Z",
      });
      break;
    case "rest_hydrated": {
      const task = currentTask();
      if (task) store.upsertBgTask(BG_SESSION_ID, { ...task, stdout: event.stdout ?? "", stderr: event.stderr ?? "", truncated: !!event.truncated });
      break;
    }
    case "result_injected": {
      const status = event.status ?? currentTask()?.status ?? "completed";
      const exitCode = event.exitCode ?? currentTask()?.exit_code ?? 0;
      const output = event.stdout ? `\n\nstdout:\n\`\`\`\n${event.stdout}\n\`\`\`` : "\n\n(no output)";
      store.addMessage(BG_SESSION_ID, {
        role: "user",
        type: "text",
        content: `[bg-task-result] Background task \`${BG_TASK_ID}\` finished with status \`${status}\` (exit code ${exitCode}).${output}`,
      });
      break;
    }
    case "followup_reply":
      store.addMessage(BG_SESSION_ID, { role: "assistant", type: "text", content: event.content });
      break;
    default:
      break;
  }
}
