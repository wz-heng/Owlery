// Fake Feishu (Lark) open-platform server for the bridge e2e
// (docs/plans/feishu-bridge.md §6). It stands in for open.feishu.cn as the
// bridge's OUTBOUND target: the SDK fetches a tenant_access_token, then POSTs
// messages / PATCHes cards here. We answer with the minimal success envelopes
// the SDK needs and RECORD every outbound message so the spec can assert what
// the bridge actually sent. INBOUND is driven separately by the spec POSTing
// Feishu-shaped events straight at the backend's /feishu/webhook.
//
// No product code runs here — it is a dumb echo/record server. It also exposes
// two control routes the real Feishu never has: GET /__sent (recorded
// outbound) and POST /__reset (clear), used only by the spec.
import http from "node:http";

const PORT = Number(process.env.FAKE_FEISHU_PORT || 9101);

/** @type {{receive_id?: string, msg_type?: string, content?: string, path: string, method: string}[]} */
const sent = [];

function readBody(req) {
  return new Promise((resolve) => {
    let data = "";
    req.on("data", (c) => (data += c));
    req.on("end", () => resolve(data));
  });
}

function json(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { "Content-Type": "application/json" });
  res.end(body);
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`);
  const path = url.pathname;
  const body = await readBody(req);

  // --- control plane (spec-only) ---
  if (path === "/__sent" && req.method === "GET") {
    return json(res, 200, { sent });
  }
  if (path === "/__reset" && req.method === "POST") {
    sent.length = 0;
    return json(res, 200, { ok: true });
  }

  // --- token endpoints ---
  if (path.includes("/auth/v3/tenant_access_token") || path.includes("/auth/v3/app_access_token")) {
    return json(res, 200, {
      code: 0,
      msg: "ok",
      tenant_access_token: "t-fake",
      app_access_token: "a-fake",
      expire: 7200,
    });
  }

  // --- send a message (record it) ---
  if (path === "/open-apis/im/v1/messages" && req.method === "POST") {
    let parsed = {};
    try {
      parsed = JSON.parse(body || "{}");
    } catch {
      /* ignore */
    }
    const id = `om_${sent.length + 1}`;
    sent.push({
      path,
      method: req.method,
      receive_id: parsed.receive_id,
      msg_type: parsed.msg_type,
      content: parsed.content,
    });
    return json(res, 200, { code: 0, msg: "ok", data: { message_id: id } });
  }

  // --- patch a card (record it) ---
  if (path.startsWith("/open-apis/im/v1/messages/") && req.method === "PATCH") {
    let parsed = {};
    try {
      parsed = JSON.parse(body || "{}");
    } catch {
      /* ignore */
    }
    sent.push({ path, method: req.method, content: parsed.content });
    return json(res, 200, { code: 0, msg: "ok", data: {} });
  }

  // --- everything else under open-apis (bot info, etc.): benign success ---
  return json(res, 200, { code: 0, msg: "ok", data: {} });
});

server.listen(PORT, "127.0.0.1", () => {
  // eslint-disable-next-line no-console
  console.log(`fake-feishu-server listening on http://127.0.0.1:${PORT}`);
});
