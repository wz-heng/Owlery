/**
 * The five families of `messenger-form.md` §4, on one page, rendered with
 * the REAL product components — not replicas. This is the before/after
 * evidence surface for stage 2, and it is deliberately built so that the
 * exact same file renders against the pre-round-3 tree: it touches no API
 * that round 3 introduced, so checking out an older commit and dropping
 * this page in shows the old form with nothing else changed.
 *
 * Dev-server only, like the stage-1 sample room: `vite build` takes
 * index.html as its sole input, so none of this reaches web/dist/.
 *
 * `fetch` is stubbed below. The sidebar components fetch agents/sessions
 * on mount and would blow away the seeded store (or reject into an
 * unhandled promise) with no backend behind them. A design harness wants
 * fixed data, not a live one — so every call resolves to a 404 and the
 * components fall back to what the store already holds.
 */
import { useEffect, useState } from "react";

import { MessageBubble } from "../components/MessageBubble";
import { AgentDelegationEventCard, parseDelegationEvent } from "../components/AgentDelegationEventCard";
import { ToolApproval } from "../components/ToolApproval";
import { QuestionPrompt } from "../components/QuestionPrompt";
import { ResearchCard } from "../components/ResearchCard";
import { BgTaskChip } from "../components/BgTaskChip";
import { AgentDelegationRequestCard } from "../components/AgentDelegationRequestCard";
import { AgentList } from "../components/AgentList";
import { OwleryLogo } from "../components/OwleryLogo";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Tabs, TabsList, TabsTrigger } from "../components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "../components/ui/dialog";
import { useSessionStore, type Message } from "../stores/sessionStore";

const SESSION_ID = "sess-form-demo";

/* ── Fixed data ────────────────────────────────────────────────────── */

const USER_TEXT =
  "帮我把 e2e 里那三个真机 spec 拆出来单独跑,顺便看下 handoff-pull 为什么只在代理机上红。";

const ASSISTANT_MD = [
  "拆出来了 —— 三个 `@llm` spec 现在走 `test:e2e:llm`,其余 66 个跑 `test:e2e:fast`。",
  "",
  "`handoff-pull` 的红跟代码无关:它的 `--server` 指向一个临时 loopback 监听器,",
  "而这台机器的 `http_proxy` 没有豁免 `127.0.0.1`,请求被代理吞掉了。",
  "",
  "- 本机修法:`export no_proxy=127.0.0.1,localhost`",
  "- 产品侧不动 —— 这个 flag 本来就要支持远端 target",
].join("\n");

const msg = (m: Partial<Message>): Message =>
  ({ type: "text", role: "user", content: "", ...m }) as Message;

function seedStore() {
  useSessionStore.setState({
    token: "demo",
    agents: [
      { id: "a1", name: "Dobby", avatar: "🦉" },
      { id: "a2", name: "Snape", avatar: "🧪" },
    ] as never,
    activeAgentId: "a1",
    sessions: [
      { id: "s1", name: "messenger form", status: "running", agent_id: "a1" },
      { id: "s2", name: "waiting on approval", status: "waiting_approval", agent_id: "a1" },
      { id: "s3", name: "idle session", status: "idle", agent_id: "a1" },
    ] as never,
    activeSessionId: "s1",
    bgTasks: {
      [SESSION_ID]: [
        {
          id: "bg1",
          command: "cd web && bun run test:e2e",
          description: "e2e suite",
          status: "completed",
          exit_code: 0,
          stdout: "69 passed (1.9m)",
          stderr: "",
          truncated: false,
        },
      ] as never,
    },
    research: {
      [SESSION_ID]: [
        {
          id: "r1",
          question: "How do other apps encode component identity?",
          status: "completed",
          phase: "done",
          verified: 7,
          sources: ["a", "b", "c"],
        },
      ] as never,
    },
  });
}

function Family({
  n,
  title,
  note,
  children,
  wide = false,
}: {
  n: string;
  title: string;
  note: string;
  children: React.ReactNode;
  wide?: boolean;
}) {
  return (
    <section className={`space-y-3 ${wide ? "w-[860px]" : "w-[520px]"}`}>
      <header>
        <h2 className="font-brand text-lg font-semibold">
          {n} · {title}
        </h2>
        <p className="text-xs text-muted-foreground">{note}</p>
      </header>
      <div className="rounded-lg border border-ink-300/70 p-6">{children}</div>
    </section>
  );
}

export function FamilySheet() {
  const [seeded, setSeeded] = useState(false);
  const [dlgOpen, setDlgOpen] = useState(false);
  useEffect(() => {
    seedStore();
    setSeeded(true);
  }, []);
  if (!seeded) return null;

  const delegationEvent = parseDelegationEvent(
    "[agent-reply:Snape delegation=3ba1f2d9ba62]\n分支看过了,结论:通过。\n\ntrust_env=False 钉在 loopback 回调上是对的。\n两处 [nit]:注释还写着旧的拼法;测试里的 env 建议用 fixture 收口。"
  )!;

  return (
    <div className="min-h-screen px-8 py-10">
      <header className="mb-8 space-y-2">
        <h1 className="flex items-center gap-3 font-brand text-2xl font-semibold">
          <OwleryLogo size={28} className="text-primary-700" />
          Messenger Form — the five families
        </h1>
        <p className="max-w-3xl text-sm text-muted-foreground">
          Real components, fixed data. Round 3 stage 2 (§4). The bar: strip
          the colour and it still says Owlery.
        </p>
      </header>

      <div className="flex flex-wrap items-start gap-10">
        <Family
          n="1"
          title="Bubbles"
          note="user / assistant / system-injected — attribution is the letterhead"
        >
          <div className="space-y-4">
            <MessageBubble
              message={msg({ content: USER_TEXT, seq: 3 })}
              sessionId={SESSION_ID}
              onFork={() => {}}
            />
            <MessageBubble
              message={msg({ role: "assistant", content: ASSISTANT_MD })}
              sessionId={SESSION_ID}
              agentName="Dobby"
            />
            <MessageBubble
              message={msg({
                content:
                  "[bg-task-result] bg task `bg1` finished with status completed (exit 0)\n69 passed (1.9m)",
              })}
              sessionId={SESSION_ID}
            />
            <MessageBubble
              message={msg({ type: "result", role: "assistant", cost: 0.0241 })}
              sessionId={SESSION_ID}
            />
          </div>
        </Family>

        <Family
          n="2"
          title="Card family"
          note="all six, one skeleton — state colour untouched (plum stays plum)"
        >
          <div className="space-y-4">
            <AgentDelegationEventCard event={delegationEvent} />
            <ToolApproval
              message={msg({
                type: "tool_approval_request",
                tool_name: "mcp__ask_agent__ask",
                tool_use_id: "t1",
                tool_input: { command: "rm -rf ./dist && bun run build" },
              })}
              onApprove={() => {}}
              onDeny={() => {}}
            />
            <QuestionPrompt
              question={{
                question_id: "q1",
                questions: [
                  {
                    header: "Scope",
                    question: "Which grammar should stage 2 roll out?",
                    options: [
                      { label: "Seal & Letterhead", description: "the disc" },
                      { label: "Dog-ear", description: "the fold" },
                    ],
                  },
                ],
              } as never}
              onSubmit={() => {}}
            />
            <ResearchCard sessionId={SESSION_ID} />
            <BgTaskChip sessionId={SESSION_ID} taskId="bg1" />
            <AgentDelegationRequestCard
              sessionId={SESSION_ID}
              toolUseId="t2"
              agentName="Snape"
              request="复核 fix/mcp-callback-trust-env 分支"
              files={["server/mcp_servers/_shared.py"]}
            />
          </div>
        </Family>

        <Family n="3" title="Dialog family" note="the app's own voice — the owl, impressed">
          <div className="space-y-3">
            <p className="text-xs text-muted-foreground">
              Radix portals the sheet to <code>&lt;body&gt;</code> and it is a
              centred modal over a scrim — which is exactly how the user meets
              it, so the shot opens it for real rather than faking it into the
              page flow.
            </p>
            <Button
              size="sm"
              data-testid="open-dialog"
              onClick={() => setDlgOpen(true)}
            >
              Open the dialog
            </Button>
          </div>
          <Dialog open={dlgOpen} onOpenChange={setDlgOpen}>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Settings</DialogTitle>
                <DialogDescription>
                  The header is ruled like any other sheet; the seal is the app
                  introducing itself.
                </DialogDescription>
              </DialogHeader>
              <Input placeholder="Working directory…" />
              <div className="flex justify-end gap-2">
                <Button variant="outline" size="sm">
                  Cancel
                </Button>
                <Button size="sm">Save</Button>
              </div>
            </DialogContent>
          </Dialog>
        </Family>

        <Family n="4" title="Controls" note="restraint: only the active tab is sealed">
          <div className="space-y-4">
            <Tabs defaultValue="one">
              <TabsList>
                <TabsTrigger value="one">Sessions</TabsTrigger>
                <TabsTrigger value="two">Archived</TabsTrigger>
                <TabsTrigger value="three">Usage</TabsTrigger>
              </TabsList>
            </Tabs>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm">Allow</Button>
              <Button size="sm" variant="outline">
                Deny
              </Button>
              <Button size="sm" variant="secondary">
                Secondary
              </Button>
              <Button size="sm" variant="ghost">
                Ghost
              </Button>
              <Button size="sm" variant="destructive">
                Delete
              </Button>
            </div>
            <Input placeholder="Type a message…" />
          </div>
        </Family>

        <Family
          n="5"
          title="Sidebar items"
          note="the status dot was already in the accent position — it just wore a generic circle"
        >
          <div className="w-[260px] rounded-lg bg-sidebar p-2">
            <AgentList onCreateAgent={() => {}} />
          </div>
        </Family>

        <Family n="6" title="The mark" note="16 / 32 / 96 — the same rim the app stamps everywhere">
          <div className="flex items-end gap-6">
            {[16, 32, 96].map((s) => (
              <div key={s} className="flex flex-col items-center gap-2">
                <OwleryLogo size={s} className="text-primary-700" />
                <span className="font-mono text-[9px] text-muted-foreground">{s}</span>
              </div>
            ))}
          </div>
        </Family>
      </div>
    </div>
  );
}
