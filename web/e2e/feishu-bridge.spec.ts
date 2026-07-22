import { expect, test, type APIRequestContext } from "@playwright/test";

// Feishu bridge e2e (docs/plans/feishu-bridge.md §6). Inbound is driven by
// POSTing Feishu-shaped events at the backend's /feishu/webhook; outbound is
// observed via the fake Feishu server's /__sent record. Transport is webhook
// (§3.1). All flows are p2p — group @-mention gating needs a resolved bot
// identity and is covered in the backend unit tests (test_bridge_feishu.py);
// tool-approval cards likewise are unit-tested because the CLI-direct backend
// never emits an approval request to click, so the identical card-action
// machinery is exercised end-to-end here through the /sessions SWITCH flow.

const BACKEND = "http://127.0.0.1:8766";
const FAKE = "http://127.0.0.1:9101";
const WEBHOOK = `${BACKEND}/feishu/webhook`;
const TOKEN = "vtok-e2e";
const CHAT = "oc_e2e";
const OPEN_ID = "ou_me";

let evSeq = 0;
function eventId(): string {
  evSeq += 1;
  return `ev-${Date.now()}-${evSeq}`;
}

function messageEvent(opts: {
  text: string;
  openId?: string;
  chatId?: string;
  token?: string;
  eventId?: string;
}) {
  return {
    schema: "2.0",
    header: {
      event_id: opts.eventId ?? eventId(),
      token: opts.token ?? TOKEN,
      create_time: String(Date.now()),
      event_type: "im.message.receive_v1",
      app_id: "cli_e2e",
      tenant_key: "tk",
    },
    event: {
      sender: {
        sender_id: { open_id: opts.openId ?? OPEN_ID },
        sender_type: "user",
        tenant_key: "tk",
      },
      message: {
        message_id: `om_${eventId()}`,
        // Fresh create_time (ms): the SDK drops messages older than its 30-min
        // stale window, so a stale timestamp would silently no-op the turn.
        create_time: String(Date.now()),
        chat_id: opts.chatId ?? CHAT,
        chat_type: "p2p",
        message_type: "text",
        content: JSON.stringify({ text: opts.text }),
      },
    },
  };
}

function cardEvent(opts: {
  value: unknown;
  operator?: string;
  chatId?: string;
  token?: string;
}) {
  return {
    schema: "2.0",
    header: {
      event_id: eventId(),
      token: opts.token ?? TOKEN,
      create_time: String(Date.now()),
      event_type: "card.action.trigger",
      app_id: "cli_e2e",
      tenant_key: "tk",
    },
    event: {
      operator: { open_id: opts.operator ?? OPEN_ID, tenant_key: "tk" },
      token: "cbtok",
      action: { tag: "button", value: opts.value },
      host: "im_message",
      context: { open_message_id: "om_card", open_chat_id: opts.chatId ?? CHAT },
    },
  };
}

async function postWebhook(request: APIRequestContext, event: unknown) {
  return request.post(WEBHOOK, {
    data: event,
    headers: { "Content-Type": "application/json" },
  });
}

async function getSent(request: APIRequestContext): Promise<
  { receive_id?: string; msg_type?: string; content?: string; method: string; path: string }[]
> {
  const resp = await request.get(`${FAKE}/__sent`);
  return (await resp.json()).sent;
}

async function resetSent(request: APIRequestContext) {
  await request.post(`${FAKE}/__reset`);
}

/** Count outbound messages whose serialized content contains `needle`. */
async function countContaining(request: APIRequestContext, needle: string): Promise<number> {
  const sent = await getSent(request);
  return sent.filter((m) => (m.content ?? "").includes(needle)).length;
}

// Agent-turn replies must clear a real `claude` (fake) spawn plus a fresh set
// of stdio MCP subprocesses (bg / ask / ask_agent / research) that cold-start
// per turn — legitimately tens of seconds on first use. Command replies (/new,
// /sessions) are in-process and resolve near-instantly; both share this
// ceiling, which only bites on genuine failure.
async function waitForContent(request: APIRequestContext, needle: string, timeoutMs = 45000) {
  await expect
    .poll(async () => countContaining(request, needle), { timeout: timeoutMs })
    .toBeGreaterThan(0);
}

test.describe("Feishu bridge", () => {
  test.beforeEach(async ({ request }) => {
    await resetSent(request);
  });

  test("message round-trips: inbound webhook → agent → outbound card", async ({ request }) => {
    const resp = await postWebhook(
      request,
      messageEvent({ text: '<<fake:[{"t":"text","v":"ROUNDTRIP_OK"}]>>' })
    );
    expect(resp.status()).toBe(200);
    await waitForContent(request, "ROUNDTRIP_OK");
  });

  test("duplicate event_id is deduped — the turn runs once", async ({ request }) => {
    const ev = messageEvent({ text: '<<fake:[{"t":"text","v":"DEDUP_ONCE"}]>>' });
    // Re-send the exact same event (same header.event_id) twice.
    await postWebhook(request, ev);
    await postWebhook(request, ev);
    await waitForContent(request, "DEDUP_ONCE");
    // Give any (wrongly) un-deduped second turn time to also land, then assert
    // the reply appeared exactly once.
    await new Promise((r) => setTimeout(r, 1500));
    expect(await countContaining(request, "DEDUP_ONCE")).toBe(1);
  });

  test("wrong verification token is rejected", async ({ request }) => {
    const resp = await postWebhook(
      request,
      messageEvent({ text: '<<fake:[{"t":"text","v":"SHOULD_NOT_RUN"}]>>', token: "WRONG" })
    );
    expect(resp.status()).toBeGreaterThanOrEqual(400);
    await new Promise((r) => setTimeout(r, 1500));
    expect(await countContaining(request, "SHOULD_NOT_RUN")).toBe(0);
  });

  test("unauthorized sender is dropped (fail-closed)", async ({ request }) => {
    const resp = await postWebhook(
      request,
      messageEvent({ text: '<<fake:[{"t":"text","v":"STRANGER_MSG"}]>>', openId: "ou_stranger" })
    );
    expect(resp.status()).toBe(200); // accepted by the SDK; dropped by the bridge
    await new Promise((r) => setTimeout(r, 1500));
    expect(await countContaining(request, "STRANGER_MSG")).toBe(0);
  });

  test("session-switch card: real click switches, and the nonce is one-time", async ({
    request,
  }) => {
    // Two sessions on a fresh chat; the second becomes the sticky one.
    const chat = "oc_switch";
    await postWebhook(request, messageEvent({ text: "/new FirstSession", chatId: chat }));
    await waitForContent(request, "FirstSession");
    await postWebhook(request, messageEvent({ text: "/new SecondSession", chatId: chat }));
    await waitForContent(request, "SecondSession");

    // Ask for the picker and grab the non-current (FirstSession) button value.
    await resetSent(request);
    await postWebhook(request, messageEvent({ text: "/sessions", chatId: chat }));
    await waitForContent(request, "switch");

    const value = await pickSwitchButton(request, "FirstSession");
    expect(value).toBeTruthy();

    // Click it as an authorized operator → the switch confirmation names it.
    await resetSent(request);
    await postWebhook(request, cardEvent({ value, chatId: chat }));
    await waitForContent(request, "Switched to session 'FirstSession'");
    const afterFirstClick = await countContaining(request, "Switched to session 'FirstSession'");
    expect(afterFirstClick).toBe(1);

    // Click the SAME card again — the nonce is consumed, so nothing happens.
    await postWebhook(request, cardEvent({ value, chatId: chat }));
    await new Promise((r) => setTimeout(r, 1500));
    expect(await countContaining(request, "Switched to session 'FirstSession'")).toBe(1);
  });

  test("card action from an unauthorized operator is rejected", async ({ request }) => {
    const chat = "oc_intruder";
    await postWebhook(request, messageEvent({ text: "/new OnlySession", chatId: chat }));
    await waitForContent(request, "OnlySession");
    await postWebhook(request, messageEvent({ text: "/new OtherSession", chatId: chat }));
    await waitForContent(request, "OtherSession");

    await resetSent(request);
    await postWebhook(request, messageEvent({ text: "/sessions", chatId: chat }));
    await waitForContent(request, "switch");
    const value = await pickSwitchButton(request, "OnlySession");
    expect(value).toBeTruthy();

    await resetSent(request);
    await postWebhook(request, cardEvent({ value, operator: "ou_intruder", chatId: chat }));
    await new Promise((r) => setTimeout(r, 1500));
    expect(await countContaining(request, "Switched to session 'OnlySession'")).toBe(0);
  });
});

/** Find the switch-button `value` for the session whose label contains `name`. */
async function pickSwitchButton(request: APIRequestContext, name: string): Promise<unknown> {
  const sent = await getSent(request);
  for (const m of sent) {
    if (!m.content) continue;
    let card: any;
    try {
      card = JSON.parse(m.content);
    } catch {
      continue;
    }
    const actions = (card.elements ?? []).find((e: any) => e.tag === "action");
    if (!actions) continue;
    for (const btn of actions.actions ?? []) {
      const label = btn?.text?.content ?? "";
      if (btn?.value?.action === "switch" && label.includes(name)) {
        return btn.value;
      }
    }
  }
  return null;
}
