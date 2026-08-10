/**
 * Renderer tests for the sidebar Schedules rail entry
 * (docs/plans/sidebar-hierarchy.md §3-4): it renders at rail level like
 * Task Board and shows a glanceable count badge that reflects the fetched
 * schedule list, hiding when there are none.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

import { ScheduleList } from "./ScheduleList";
import { useSessionStore, type Schedule } from "../stores/sessionStore";

function schedule(overrides: Partial<Schedule> = {}): Schedule {
  return {
    id: "s1",
    agent_id: "a1",
    prompt: "do the thing",
    interval_seconds: 3600,
    cron: null,
    enabled: true,
    created_at: "2026-06-09T00:00:00Z",
    last_run_at: null,
    next_run_at: null,
    ...overrides,
  } as Schedule;
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  useSessionStore.setState({ token: "tok", schedules: [] });
  fetchMock = vi.fn(async () => {
    return new Response(
      JSON.stringify(useSessionStore.getState().schedules),
      { status: 200, headers: { "content-type": "application/json" } }
    );
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ScheduleList count badge", () => {
  it("renders no count badge when there are no schedules", async () => {
    await act(async () => {
      render(<ScheduleList onOpen={vi.fn()} />);
    });
    await screen.findByText("Schedules");
    expect(screen.queryByText(/^\d+$/)).toBeNull();
  });

  it("renders the live schedule count in the header", async () => {
    useSessionStore.setState({ schedules: [schedule({ id: "s1" }), schedule({ id: "s2" })] });
    await act(async () => {
      render(<ScheduleList onOpen={vi.fn()} />);
    });
    expect(await screen.findByText("2")).toBeTruthy();
  });

  it("calls onOpen when clicked", async () => {
    const onOpen = vi.fn();
    await act(async () => {
      render(<ScheduleList onOpen={onOpen} />);
    });
    const header = await screen.findByTitle("View all schedules");
    fireEvent.click(header);
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});
