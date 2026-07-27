import { beforeEach, describe, expect, it } from "vitest";

import { useSessionStore } from "../../stores/sessionStore";
import { applyBgSpecimenEvent, resetBgSpecimenStore } from "./bgState";
import { BG_SESSION_ID, BG_TASK_ID } from "./scripts";

describe("background task specimen state", () => {
  beforeEach(resetBgSpecimenStore);

  it("uses the production WS reducer for running and terminal state", () => {
    applyBgSpecimenEvent({ type: "bg_started", actor: "Manager" });
    expect(useSessionStore.getState().bgTasks[BG_SESSION_ID][0]).toMatchObject({ id: BG_TASK_ID, status: "running" });

    applyBgSpecimenEvent({ type: "bg_completed", actor: "WebSocket", status: "cancelled", exitCode: -15 });
    expect(useSessionStore.getState().bgTasks[BG_SESSION_ID][0]).toMatchObject({ status: "cancelled", exit_code: -15 });
  });

  it("hydrates bytes separately and injects a production-shaped result turn", () => {
    applyBgSpecimenEvent({ type: "bg_started", actor: "Manager" });
    applyBgSpecimenEvent({ type: "rest_hydrated", actor: "MCP", stdout: "76 passed" });
    applyBgSpecimenEvent({ type: "result_injected", actor: "Queue", status: "completed", exitCode: 0, stdout: "76 passed" });

    const store = useSessionStore.getState();
    expect(store.bgTasks[BG_SESSION_ID][0].stdout).toBe("76 passed");
    expect(store.messages[BG_SESSION_ID].at(-1)?.content).toMatch(/^\[bg-task-result\]/);
  });
});
