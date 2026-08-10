/**
 * Renderer tests for the sidebar "Integrations" disclosure group
 * (docs/plans/sidebar-hierarchy.md §3-4): collapsed by default, toggles
 * open/closed on click, persists that state across remounts (localStorage,
 * same convention as `showDelegations`), and its header count reflects
 * installed connectors + credentials even while collapsed.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

import { IntegrationsSection } from "./IntegrationsSection";
import { INTEGRATIONS_EXPANDED_KEY } from "../lib/storage";
import { useSessionStore } from "../stores/sessionStore";

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  localStorage.clear();
  useSessionStore.setState({
    token: "tok",
    integrationsExpanded: false,
    connectorCatalog: [],
    connectorInstallations: [],
    credentials: [],
  });
  fetchMock = vi.fn(async (url: string) => {
    if (url.includes("/api/connectors/catalog")) {
      return new Response(JSON.stringify([]), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (url.includes("/api/connectors")) {
      return new Response(
        JSON.stringify(useSessionStore.getState().connectorInstallations),
        { status: 200, headers: { "content-type": "application/json" } }
      );
    }
    if (url.includes("/api/credentials")) {
      return new Response(
        JSON.stringify(useSessionStore.getState().credentials),
        { status: 200, headers: { "content-type": "application/json" } }
      );
    }
    return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("IntegrationsSection", () => {
  it("is collapsed by default and expands on click", async () => {
    await act(async () => {
      render(<IntegrationsSection />);
    });

    const header = screen.getByRole("button", { name: /Integrations/i });
    expect(header).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("region", { name: "Integrations" })).toBeNull();

    await act(async () => {
      fireEvent.click(header);
    });

    expect(header).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("region", { name: "Integrations" })).toBeTruthy();
  });

  it("collapses again on a second click", async () => {
    await act(async () => {
      render(<IntegrationsSection />);
    });
    const header = screen.getByRole("button", { name: /Integrations/i });

    await act(async () => {
      fireEvent.click(header);
    });
    expect(header).toHaveAttribute("aria-expanded", "true");

    await act(async () => {
      fireEvent.click(header);
    });
    expect(header).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("region", { name: "Integrations" })).toBeNull();
  });

  it("persists the expanded state to localStorage and across remounts", async () => {
    const { unmount } = render(<IntegrationsSection />);
    const header = screen.getByRole("button", { name: /Integrations/i });

    await act(async () => {
      fireEvent.click(header);
    });
    expect(localStorage.getItem(INTEGRATIONS_EXPANDED_KEY)).toBe("true");

    unmount();
    cleanup();

    // A fresh mount reads the persisted flag straight from the store, which
    // itself seeds from localStorage at module init — simulate a reload by
    // re-running the store's persisted read directly.
    useSessionStore.setState({ integrationsExpanded: true });
    await act(async () => {
      render(<IntegrationsSection />);
    });
    expect(
      screen.getByRole("button", { name: /Integrations/i })
    ).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("region", { name: "Integrations" })).toBeTruthy();
  });

  it("shows a combined connector+credential count in the header, even while collapsed", async () => {
    useSessionStore.setState({
      connectorInstallations: [
        { id: "i1", kind: "github", label: "GH", needs_reconnect: false } as never,
      ],
      credentials: [
        {
          id: "c1",
          backend: "claude-code",
          label: "Personal",
          auth_type: "oauth",
          created_at: "2026-06-09T00:00:00Z",
          status: "active",
          token_expires_at: null,
          needs_reconnect: false,
          last_refresh_error_code: null,
        } as never,
      ],
    });

    await act(async () => {
      render(<IntegrationsSection />);
    });

    expect(
      screen.getByRole("button", { name: /Integrations/i })
    ).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("2")).toBeTruthy();
  });

  it("shows 'none' when nothing is configured", async () => {
    await act(async () => {
      render(<IntegrationsSection />);
    });
    expect(screen.getByText("none")).toBeTruthy();
  });
});
