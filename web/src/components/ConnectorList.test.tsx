/**
 * Renderer tests for ConnectorList's static-credential install flow
 * (mail-connector.md §4.1) — the non-OAuth path: preset dropdown autofill,
 * per-field form rendering driven entirely by the catalog entry's
 * `static_fields`/`static_presets` (no per-kind frontend code), inline
 * server-error surfacing, and the install POST body.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ConnectorList } from "./ConnectorList";
import { useSessionStore } from "../stores/sessionStore";
import type { ConnectorCatalogEntry, ConnectorInstallationInfo } from "../api";

function mailCatalogEntry(overrides: Partial<ConnectorCatalogEntry> = {}): ConnectorCatalogEntry {
  return {
    kind: "mail",
    display_name: "Mail (IMAP/SMTP)",
    category: "email",
    allows_multiple: true,
    available: true,
    scopes: [],
    custom: false,
    setup_url: null,
    setup_steps: [],
    auth_mode: "static",
    static_fields: [
      { key: "email", label: "Email address", secret: false, default: "", placeholder: "", help_text: "" },
      {
        key: "auth_code",
        label: "Authorization code",
        secret: true,
        default: "",
        placeholder: "",
        help_text: "Not your account password.",
      },
      { key: "imap_host", label: "IMAP host", secret: false, default: "", placeholder: "", help_text: "" },
      { key: "imap_port", label: "IMAP port", secret: false, default: "993", placeholder: "", help_text: "" },
      { key: "smtp_host", label: "SMTP host", secret: false, default: "", placeholder: "", help_text: "" },
      { key: "smtp_port", label: "SMTP port", secret: false, default: "465", placeholder: "", help_text: "" },
    ],
    static_presets: [
      {
        key: "qq",
        label: "QQ Mail",
        values: { imap_host: "imap.qq.com", imap_port: "993", smtp_host: "smtp.qq.com", smtp_port: "465" },
      },
      {
        key: "outlook",
        label: "Outlook",
        values: {
          imap_host: "outlook.office365.com",
          imap_port: "993",
          smtp_host: "smtp.office365.com",
          smtp_port: "587",
        },
      },
    ],
    ...overrides,
  } as ConnectorCatalogEntry;
}

let fetchMock: ReturnType<typeof vi.fn>;
let installations: ConnectorInstallationInfo[];

beforeEach(() => {
  installations = [];
  useSessionStore.setState({
    token: "tok",
    connectorCatalog: [mailCatalogEntry()],
    connectorInstallations: [],
  });
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const u = String(url);
    if (u.endsWith("/api/connectors/catalog")) {
      return new Response(JSON.stringify(useSessionStore.getState().connectorCatalog), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (u.endsWith("/api/connectors") && method === "GET") {
      return new Response(JSON.stringify(installations), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (u.endsWith("/install-static") && method === "POST") {
      const body = JSON.parse(init!.body as string);
      if (body.fields.auth_code === "wrong") {
        return new Response(JSON.stringify({ detail: "IMAP login failed: bad authorization code" }), {
          status: 400,
          headers: { "content-type": "application/json" },
        });
      }
      const inst: ConnectorInstallationInfo = {
        id: "inst-1",
        kind: "mail",
        label: body.fields.email,
        auth_type: "api_key",
        external_account_id: body.fields.email,
        scopes: [],
        enable_by_default: false,
        needs_reconnect: false,
        token_expires_at: null,
        last_refresh_error_code: null,
        created_at: "2026-08-26T00:00:00Z",
      };
      installations = [inst];
      return new Response(JSON.stringify(inst), {
        status: 201,
        headers: { "content-type": "application/json" },
      });
    }
    return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function openMailForm() {
  await act(async () => {
    render(<ConnectorList />);
  });
  fireEvent.click(screen.getByTitle("Add connector"));
  const connectBtn = await screen.findByRole("button", { name: "Connect" });
  await act(async () => {
    fireEvent.click(connectBtn);
  });
  await screen.findByText("Connect Mail (IMAP/SMTP)");
}

describe("ConnectorList static-credential install", () => {
  it("shows a plain Connect button (no OAuth Set-up step) for a static kind", async () => {
    await act(async () => {
      render(<ConnectorList />);
    });
    fireEvent.click(screen.getByTitle("Add connector"));
    await screen.findByText("Mail (IMAP/SMTP)");
    expect(screen.queryByText(/Needs an OAuth client/)).toBeNull();
    expect(screen.getByRole("button", { name: "Connect" })).toBeTruthy();
  });

  it("renders one input per declared static field, with secret fields masked", async () => {
    await openMailForm();
    expect(screen.getByLabelText("Email address")).toBeTruthy();
    const authInput = screen.getByLabelText("Authorization code") as HTMLInputElement;
    expect(authInput.type).toBe("password");
    expect(screen.getByText("Not your account password.")).toBeTruthy();
  });

  it("defaults to the first preset's values on open", async () => {
    await openMailForm();
    expect((screen.getByLabelText("IMAP host") as HTMLInputElement).value).toBe("imap.qq.com");
    expect((screen.getByLabelText("SMTP port") as HTMLInputElement).value).toBe("465");
  });

  it("selecting a different preset autofills its host/port values", async () => {
    await openMailForm();
    fireEvent.change(screen.getByLabelText("Preset"), { target: { value: "outlook" } });
    expect((screen.getByLabelText("IMAP host") as HTMLInputElement).value).toBe(
      "outlook.office365.com"
    );
    expect((screen.getByLabelText("SMTP port") as HTMLInputElement).value).toBe("587");
  });

  it("disables Connect until every field is filled", async () => {
    await openMailForm();
    const submit = screen.getByRole("button", { name: "Connect" });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "me@qq.com" } });
    fireEvent.change(screen.getByLabelText("Authorization code"), { target: { value: "abc123" } });
    expect(submit).not.toBeDisabled();
  });

  it("submits the fields to install-static and adds the new installation", async () => {
    await openMailForm();
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "me@qq.com" } });
    fireEvent.change(screen.getByLabelText("Authorization code"), { target: { value: "abc123" } });
    const submit = screen.getByRole("button", { name: "Connect" });
    await act(async () => {
      fireEvent.click(submit);
    });
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([u]) => String(u).endsWith("/install-static"));
      expect(call).toBeTruthy();
      const body = JSON.parse((call![1] as RequestInit).body as string);
      expect(body.fields.email).toBe("me@qq.com");
      expect(body.fields.auth_code).toBe("abc123");
      expect(body.fields.imap_host).toBe("imap.qq.com");
    });
    await screen.findByText("me@qq.com");
  });

  it("shows the server's verification error inline without closing the dialog", async () => {
    await openMailForm();
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "me@qq.com" } });
    fireEvent.change(screen.getByLabelText("Authorization code"), { target: { value: "wrong" } });
    const submit = screen.getByRole("button", { name: "Connect" });
    await act(async () => {
      fireEvent.click(submit);
    });
    expect(await screen.findByText(/IMAP login failed/)).toBeTruthy();
    // Dialog stayed open on the static form — the user can fix and retry.
    expect(screen.getByLabelText("Email address")).toBeTruthy();
  });
});
