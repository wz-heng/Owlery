import {
  IconArrowRight,
  IconCheck,
  IconLoader2,
  IconPlayerPause,
  IconPlayerPlay,
  IconRefresh,
  IconRoute,
  IconStepInto,
  IconSubtask,
  IconUsers,
  IconX,
} from "@tabler/icons-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { MessageBubble } from "../../components/MessageBubble";
import { OwleryLogo } from "../../components/OwleryLogo";
import { Button } from "../../components/ui/button";
import { SealChip, type CardTone } from "../../components/ui/sheet-card";
import {
  useSessionStore,
  type Delegation,
  type Message,
  type SessionStatus,
} from "../../stores/sessionStore";
import { DelegationReplayEngine } from "./DelegationReplayEngine";
import {
  applyDelegationSpecimenEvent,
  resetDelegationSpecimenStore,
} from "./delegationState";
import {
  DELEGATION_SCRIPTS,
  DOBBY_SESSION_ID,
  PARENT_SESSION_ID,
  type DelegationScenarioId,
  type DelegationScript,
  type DelegationSpecimenEvent,
} from "./scripts";
const EMPTY_MESSAGES: Message[] = [];
const EMPTY_DELEGATIONS: Delegation[] = [];

interface DelegationSnapshot {
  parentMessages: number;
  childMessages: number;
  rootState: Delegation["state"] | "not_started";
  nestedState: Delegation["state"] | "not_started";
  activeRuns: number;
  depth: number;
}

interface DelegationLog {
  index: number;
  event: DelegationSpecimenEvent;
  before: DelegationSnapshot;
  after: DelegationSnapshot;
}

function snapshot(): DelegationSnapshot {
  const store = useSessionStore.getState();
  const root = (store.delegations[PARENT_SESSION_ID] || [])[0];
  const nested = (store.delegations[DOBBY_SESSION_ID] || [])[0];
  const all = Object.values(store.delegations).flat();
  return {
    parentMessages: store.messages[PARENT_SESSION_ID]?.length ?? 0,
    childMessages: store.messages[DOBBY_SESSION_ID]?.length ?? 0,
    rootState: root?.state ?? "not_started",
    nestedState: nested?.state ?? "not_started",
    activeRuns: all.filter((item) => item.state === "running").length,
    depth: nested ? 2 : root ? 1 : 0,
  };
}

const EVENT_LABELS: Record<DelegationSpecimenEvent["type"], string> = {
  parent_prompt: "用户提出任务",
  delegation_started: "父 Agent 发起委派",
  child_running: "子会话开始运行",
  child_text: "子 Agent 独立工作",
  child_question: "问题注入父会话",
  parent_answer: "父 Agent 回答子问题",
  nested_started: "子 Agent 再次委派",
  nested_reply: "孙会话结果回流",
  child_reply: "最终结果注入父会话",
  child_error: "子会话失败",
  child_cancelled: "取消沿调用链传播",
};

function DelegationLaunchCard({ delegation }: { delegation: Delegation }) {
  const tone: CardTone =
    delegation.state === "completed"
      ? "brand"
      : delegation.state === "failed"
        ? "destructive"
        : "neutral";
  const stateLabel = {
    running: "running",
    completed: "replied",
    failed: "failed",
    cancelled: "cancelled",
  }[delegation.state];

  return (
    <SealChip
      className="delegation-launch-card"
      tone={tone}
      glyph={<IconSubtask />}
      title={
        <div className="delegation-launch-title">
          <strong>Asked {delegation.target_agent_name}</strong>
          <span data-state={delegation.state}>
            {delegation.state === "running" ? <IconLoader2 className="animate-spin" /> : delegation.state === "completed" ? <IconCheck /> : <IconX />}
            {stateLabel}
          </span>
        </div>
      }
      meta={delegation.delegation_id.slice(0, 8)}
    >
      <p>“{delegation.request}”</p>
      <code>delegation_id = child_session.id</code>
    </SealChip>
  );
}

function SessionPane({
  title,
  subtitle,
  agentName,
  agentId,
  status,
  messages,
  delegations,
  sessionId,
}: {
  title: string;
  subtitle: string;
  agentName: string;
  agentId: string;
  status: SessionStatus;
  messages: Message[];
  delegations: Delegation[];
  sessionId: string;
}) {
  const first = messages.slice(0, 1);
  const rest = messages.slice(1);
  const renderMessage = (message: Message, index: number) => (
    <MessageBubble
      key={`${message.type}-${index}-${message.content?.slice(0, 12)}`}
      message={message}
      sessionId={sessionId}
      agentName={agentName}
      agentId={agentId}
    />
  );

  return (
    <section className="delegation-session-pane">
      <header>
        <span className="delegation-agent-seal" data-agent={agentName}>{agentName.slice(0, 1)}</span>
        <div><strong>{title}</strong><small>{subtitle}</small></div>
        <span className="delegation-status" data-status={status}><span />{status}</span>
      </header>
      <div className="delegation-transcript">
        {messages.length === 0 && delegations.length === 0 ? (
          <div className="delegation-empty"><IconUsers /><strong>等待事件进入</strong><span>单步执行可以看清每一跳。</span></div>
        ) : (
          <>
            {first.map(renderMessage)}
            {delegations.map((item) => <DelegationLaunchCard key={item.delegation_id} delegation={item} />)}
            {rest.map((message, index) => renderMessage(message, index + 1))}
          </>
        )}
      </div>
    </section>
  );
}

function CallChain({
  root,
  nested,
}: {
  root: Delegation | undefined;
  nested: Delegation | undefined;
}) {
  const nodes = [
    { name: "Aberforth", role: "parent", state: root ? "delegated" : "ready" },
    { name: "Dobby", role: "child", state: root?.state ?? "waiting" },
    ...(nested ? [{ name: "Researcher", role: "grandchild", state: nested.state }] : []),
  ];
  return (
    <div className="call-chain" aria-label="委派调用链">
      {nodes.map((node, index) => (
        <div className="chain-node-wrap" key={node.name}>
          {index > 0 && <div className="chain-edge"><span>ask</span><IconArrowRight /></div>}
          <div className="chain-node" data-state={node.state}>
            <span>{node.name.slice(0, 1)}</span>
            <div><strong>{node.name}</strong><small>{node.role} · {node.state}</small></div>
          </div>
        </div>
      ))}
    </div>
  );
}

function DelegationInspector({
  logs,
  selected,
  onSelect,
}: {
  logs: DelegationLog[];
  selected: number | null;
  onSelect: (index: number) => void;
}) {
  const current = selected === null ? logs.at(-1) : logs[selected];
  const fields: Array<[keyof DelegationSnapshot, string]> = [
    ["rootState", "root delegation"],
    ["nestedState", "nested delegation"],
    ["activeRuns", "active runs"],
    ["depth", "chain depth"],
    ["parentMessages", "parent turns"],
    ["childMessages", "child turns"],
  ];

  return (
    <aside className="delegation-inspector">
      <div className="panel-heading">
        <div><span className="eyebrow">CHAIN INSPECTOR</span><h2>每一跳都留下身份</h2></div>
        <span className="live-indicator"><span />追踪中</span>
      </div>
      <div className="delegation-log">
        {logs.length === 0 ? (
          <div className="inspector-empty"><IconRoute /><p>事件开始后，这里显示调用者、目标、delegation_id 和状态变化。</p></div>
        ) : logs.map((log, order) => (
          <button type="button" data-selected={order === selected} key={`${order}-${log.event.type}`} onClick={() => onSelect(order)}>
            <span>{String(order + 1).padStart(2, "0")}</span>
            <div><strong>{EVENT_LABELS[log.event.type]}</strong><small>{log.event.actor} · {log.event.delegationId?.slice(0, 8) ?? "parent"}</small></div>
            <IconArrowRight />
          </button>
        ))}
      </div>
      <div className="delegation-state-diff">
        <div className="diff-head"><span>STATE SLICE</span><span>BEFORE</span><span>AFTER</span></div>
        {fields.map(([key, label]) => (
          <div className="diff-row" data-changed={!!current && current.before[key] !== current.after[key]} key={key}>
            <span>{label}</span><code>{current ? String(current.before[key]) : "—"}</code><code>{current ? String(current.after[key]) : "—"}</code>
          </div>
        ))}
      </div>
      <pre className="delegation-event-json">{current ? JSON.stringify(current.event, null, 2) : "// 选择一个事件查看载荷"}</pre>
    </aside>
  );
}

const DELEGATION_ARTICLE_TOC = [
  {
    id: "principle-002-1",
    index: "01",
    title: "异步委派生命周期",
    sections: [
      ["delegation-lifecycle-trace", "先走完一次真实委派"],
      ["delegation-identity", "身份模型"],
      ["delegation-boundaries", "实际执行边界"],
      ["delegation-broadcast", "广播与父 turn"],
      ["delegation-failures", "失败出口"],
      ["delegation-terminal-race", "终态竞态"],
      ["delegation-lifecycle-source", "源码阅读顺序"],
    ],
  },
  {
    id: "principle-002-2",
    index: "02",
    title: "问题沿调用链返回",
    sections: [
      ["delegation-question-trace", "一次问题往返"],
      ["delegation-question-source", "唯一事实源"],
      ["delegation-question-decisions", "父 Agent 的分支"],
      ["delegation-question-race", "并发回答"],
      ["delegation-question-source-code", "源码阅读顺序"],
    ],
  },
  {
    id: "principle-002-3",
    index: "03",
    title: "同一子会话上的后续轮次",
    sections: [
      ["delegation-followup-trace", "从首轮到第二轮"],
      ["delegation-followup-fields", "稳定与重置字段"],
      ["delegation-followup-modes", "继续、重建与并行"],
      ["delegation-followup-errors", "拒绝路径"],
      ["delegation-followup-source", "源码阅读顺序"],
    ],
  },
  {
    id: "principle-002-4",
    index: "04",
    title: "会话树的深度、环与取消",
    sections: [
      ["delegation-guard-tree", "三跳会话树"],
      ["delegation-guard-archives", "归档祖先"],
      ["delegation-guard-types", "四类护栏"],
      ["delegation-cascade-cancel", "级联取消"],
      ["delegation-cancel-race", "取消竞态"],
      ["delegation-guard-source", "源码阅读顺序"],
    ],
  },
] as const;

function DelegationArticleToc() {
  const [open, setOpen] = useState(false);
  const [activeId, setActiveId] = useState<string>(DELEGATION_ARTICLE_TOC[0].id);

  useEffect(() => {
    const media = window.matchMedia("(min-width: 1101px)");
    const update = () => setOpen(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    const ids = DELEGATION_ARTICLE_TOC.flatMap((chapter) => [
      chapter.id,
      ...chapter.sections.map(([id]) => id),
    ]);
    let frame = 0;
    const update = () => {
      frame = 0;
      let current = ids[0];
      for (const id of ids) {
        const element = document.getElementById(id);
        if (element && element.getBoundingClientRect().top <= 148) current = id;
      }
      setActiveId(current);
    };
    const schedule = () => {
      if (!frame) frame = window.requestAnimationFrame(update);
    };
    update();
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("hashchange", schedule);
    return () => {
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("hashchange", schedule);
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, []);

  const activeChapter = DELEGATION_ARTICLE_TOC.find((chapter) =>
    chapter.id === activeId || chapter.sections.some(([id]) => id === activeId)
  )?.id;
  const choose = (id: string) => {
    setActiveId(id);
    if (window.matchMedia("(max-width: 1100px)").matches) setOpen(false);
  };

  return (
    <aside className="delegation-local-toc" aria-label="多 Agent 原理目录">
      <div className="local-toc-head">
        <strong>本页目录</strong>
        <button
          type="button"
          aria-expanded={open}
          aria-controls="delegation-local-toc-list"
          onClick={() => setOpen((value) => !value)}
        >
          {open ? "收起" : "展开"}
        </button>
      </div>
      <nav id="delegation-local-toc-list" data-open={open} aria-label="多 Agent 原理章节">
        {DELEGATION_ARTICLE_TOC.map((chapter) => (
          <div className="local-toc-group" data-active={activeChapter === chapter.id} key={chapter.id}>
            <a
              data-level="1"
              href={`#${chapter.id}`}
              aria-current={activeId === chapter.id ? "location" : undefined}
              onClick={() => choose(chapter.id)}
            >
              <span>{chapter.index}</span>{chapter.title}
            </a>
            <div className="local-toc-subnav">
              {chapter.sections.map(([id, title]) => (
                <a
                  data-level="2"
                  href={`#${id}`}
                  aria-current={activeId === id ? "location" : undefined}
                  onClick={() => choose(id)}
                  key={id}
                >
                  {title}
                </a>
              ))}
            </div>
          </div>
        ))}
      </nav>
    </aside>
  );
}

function DelegationPrinciples({ onScenario }: { onScenario: (id: DelegationScenarioId) => void }) {
  return (
    <section className="principle-panel delegation-principles" id="principle">
      <div className="principle-copy">
        <span className="eyebrow">UNDER THE SURFACE</span>
        <h2>执行路径与失败边界</h2>
      </div>
      <DelegationArticleToc />

      <section className="principle-chapter principle-article" id="principle-002-1">
        <div className="chapter-heading"><span className="chapter-index">01</span><div><span className="eyebrow">ASYNC LIFECYCLE</span><h3>异步委派生命周期</h3></div></div>
        <p className="principle-question">本章回答：父 Agent 已经结束当前 turn 后，子 Agent 的身份、执行权和结果交付分别由谁持有？</p>
        <div className="principle-depth delegation-article">
          <section className="article-section" id="delegation-lifecycle-trace">
            <h4>先走完一次真实委派</h4>
            <p>下面沿用页面上方的成功场景：用户要求 Aberforth 让 Dobby 独立审查 streaming anatomy 的事件去重设计，父会话 id 是 <code>specimen-parent-aberforth</code>，新建的子会话 id 是 <code>dlg-dobby-a1b2c3</code></p>
            <pre className="article-code article-sequence"><code>{`User
  │  "让 Dobby 独立审查事件去重设计"
  ▼
Aberforth / parent Session
  │  mcp__ask_agent__ask(name="Dobby", request=...)
  ▼
DelegationManager
  │  create child Session: dlg-dobby-a1b2c3
  │  return delegation_id immediately
  ▼
Dobby / child Harness
  │  assistant_text ... assistant_text ... result
  ▼
DelegationManager broadcast listener
  │  start_message(parent, "[agent-reply:Dobby ...]")
  ▼
Aberforth / next parent turn
  │  consumes Dobby's result and answers the user
  ▼
archive child Session`}</code></pre>
            <ol className="delegation-trace" aria-label="一次成功委派的完整时间线">
              <li><span>T0</span><div><strong>请求进入父会话</strong><p>用户消息写入 Aberforth 的 Session，父 Harness 开始当前 turn，此时还没有任何子会话</p></div><code>parent.status = running</code></li>
              <li><span>T1</span><div><strong>父模型发起委派</strong><p>Aberforth 调用 <code>mcp__ask_agent__ask</code>，MCP shim 把 name、request 和 files POST 到父会话的 delegation 路由</p></div><code>POST /sessions/:id/delegations</code></li>
              <li><span>T2</span><div><strong>Manager 校验调用链</strong><p>目标 Agent 必须唯一存在，不能是当前 Agent，也不能已经出现在祖先链中；新的一跳不能超过深度上限 3</p></div><code>_resolve_target_agent → _check_chain</code></li>
              <li><span>T3</span><div><strong>创建并登记子会话</strong><p>SessionManager 创建 <code>origin=delegation</code> 的 Dobby Session，Manager 随即用 child.id 建立 running 记录</p></div><code>delegation_id = child.id</code></li>
              <li><span>T4</span><div><strong>启动子 turn</strong><p>Manager 组装仅包含调用者、任务和文件路径的初始 prompt，然后调用 <code>start_message(child.id, prompt)</code>，父 transcript 不会整段复制过去</p></div><code>child.status = running</code></li>
              <li><span>T5</span><div><strong>父 Agent 先拿到身份</strong><p>API 返回 running 记录和 <code>dlg-dobby-a1b2c3</code>，父模型知道任务已经交给谁，可以结束当前 turn，不必等 Dobby 完成</p></div><code>201 Created · state=running</code></li>
              <li><span>T6</span><div><strong>Dobby 独立执行</strong><p>子 Harness 使用 Dobby 自己的 backend、工具和记忆处理任务，产生的完整 <code>assistant_text</code> 块由广播监听器暂存</p></div><code>captured_text += block</code></li>
              <li><span>T7</span><div><strong>子 turn 到达终态</strong><p>正常 <code>result</code> 把记录改为 completed；错误和取消走各自终态，只有 <code>limit_paused</code> 会继续保持 running 等待自动恢复</p></div><code>running → completed</code></li>
              <li><span>T8</span><div><strong>结果进入父会话的新 turn</strong><p>Manager 把文本拼成 <code>[agent-reply:Dobby delegation=dlg-dobby-a1b2c3]</code>，通过父 Session 的 <code>start_message</code> 排队，Aberforth 在下一轮读取结果并继续回答用户</p></div><code>parent.queue += agent-reply</code></li>
              <li><span>T9</span><div><strong>归档执行现场</strong><p>终态注入尝试完成后，子 Session 从活跃列表隐藏，但数据库中的 lineage、消息和工具事件仍可审计</p></div><code>child.archived = true</code></li>
            </ol>
            <table className="article-table trace-state-table">
              <thead><tr><th>时间</th><th>父 Session</th><th>委派记录</th><th>子 Session</th><th>调用者能看到什么</th></tr></thead>
              <tbody>
                <tr><td>T0–T2</td><td>running</td><td>尚未创建</td><td>尚未创建</td><td>父 Agent 正在处理用户请求</td></tr>
                <tr><td>T3–T5</td><td>可结束当前 turn</td><td>running</td><td>running</td><td>Asked Dobby + delegation_id</td></tr>
                <tr><td>T6–T7</td><td>idle 或处理其他消息</td><td>running → completed</td><td>running → idle</td><td>子任务仍可单独观察</td></tr>
                <tr><td>T8–T9</td><td>收到新的 queued turn</td><td>completed</td><td>archived</td><td>Dobby replied + 完整结果</td></tr>
              </tbody>
            </table>
            <p>后面的身份、广播和失败规则都只是在解释这条 T0 → T9 主线为什么不会丢失归属、重复交付或提前归档</p>
          </section>

          <section className="article-section" id="delegation-identity">
            <h4>身份模型：委派 id 就是子会话 id</h4>
            <p>系统没有再建一张 delegation 实体表，也没有维护“委派 id 到会话 id”的映射，<code>DelegationRunState</code> 只是运行期索引，真正落库并拥有消息历史的是 Session</p>
            <pre className="article-code"><code>{`child = await create_session(
    agent_id=target.id,
    origin="delegation",
    parent_session_id=parent.id,
    delegation_request=request,
)

record = DelegationRunState(
    delegation_id=child.id,
    parent_session_id=parent.id,
)
records[child.id] = record`}</code></pre>
            <table className="article-table identity-table">
              <thead><tr><th>数据</th><th>存放位置</th><th>承担的职责</th></tr></thead>
              <tbody>
                <tr><td><code>Session</code></td><td>数据库 + 活跃会话内存</td><td>身份、消息、Harness 状态与归档历史</td></tr>
                <tr><td><code>DelegationRunState</code></td><td>DelegationManager 内存注册表</td><td>当前轮状态、捕获文本与单次终态护栏</td></tr>
                <tr><td>父会话注入消息</td><td>父 Session 消息流</td><td>让调用者在新 turn 中消费 reply、question 或 error</td></tr>
              </tbody>
            </table>
            <aside className="article-callout"><strong>关键不变量</strong><code>delegation_id === child_session.id</code><p>继续、取消、回答问题和审计始终引用同一个身份源，避免两套 id 在异常路径中漂移</p></aside>
          </section>

          <section className="article-section principle-mechanism" id="delegation-boundaries">
            <h4>一次委派实际经过哪些边界</h4>
            <span>MECHANISM / 实际机制</span>
            <ol>
              <li>父模型调用 <code>mcp__ask_agent__ask</code>，提交目标 Agent、任务说明和可选文件引用；REST 层只做鉴权与参数翻译</li>
              <li>Manager 解析目标 Agent，拒绝同名歧义、自委派、祖先环和超过三跳的调用链</li>
              <li>系统创建 <code>origin=delegation</code> 的真实子 Session，继承父会话工作目录，但使用目标 Agent 的 backend、身份、工具与长期记忆</li>
              <li>运行记录必须先写入 <code>records[child.id]</code>，然后才能启动子 turn；否则极快的测试 Harness 可能先广播结果，监听器却找不到归属</li>
              <li>初始 prompt 只传调用者、请求和文件路径，不复制父会话 transcript，这是上下文与隐私边界</li>
              <li>启动成功后立即向父模型返回 <code>delegation_id</code>，子 Harness 继续运行，父 turn 无需阻塞等待终态</li>
              <li>广播监听器累计完整的 <code>assistant_text</code> 块，收到 <code>result</code> 或真实 <code>error</code> 后才封装终态消息</li>
              <li>终态消息通过 <code>SessionManager.start_message(parent_id, prompt)</code> 进入父会话，即使父会话正在执行也会按普通消息队列顺序处理</li>
              <li>交付尝试结束后才归档子 Session，数据库中的 lineage、消息与工具事件仍可从管理页重新审计</li>
            </ol>
          </section>

          <section className="article-section" id="delegation-broadcast">
            <h4>广播监听器如何把事件压成一个父 turn</h4>
            <p>子 Harness 会产生很多中间事件，但委派协议只让三类事件越过会话边界，避免把增量 token、内部工具噪声和半成品直接灌给父 Agent</p>
            <pre className="article-code"><code>{`assistant_text  -> append complete text block
question_request -> inject [agent-question:...]
result            -> completed | failed, then inject terminal
error             -> failed, except code == "limit_paused"`}</code></pre>
            <p>完成态会把捕获到的文本块用明确换行拼接为一条 <code>[agent-reply:&lt;name&gt; delegation=&lt;id&gt;]</code> 消息；失败或取消则产生 <code>[agent-error:...]</code>，父 Agent 因此能用普通 turn 继续决策，而不是依赖一个已经结束的工具调用栈</p>
            <aside className="article-callout article-callout-muted"><strong>单轮交付契约</strong><p>每一轮委派只在子 turn 结束时交付一次完整答复，后续迭代要显式复用同一个 <code>delegation_id</code> 发起 follow-up，不能指望子 Agent 在后台自行开启第二轮</p></aside>
          </section>

          <section className="article-section" id="delegation-failures">
            <h4>失败并不只有一个出口</h4>
            <p>如果所有异常都折叠成 failed，调用者就无法判断应该等待、重试、重新委派还是停止整条链，因此终态分类本身就是协议的一部分</p>
            <table className="article-table failure-table">
              <thead><tr><th>事件</th><th>记录状态</th><th>父会话看到什么</th><th>为什么这样处理</th></tr></thead>
              <tbody>
                <tr><td>子 turn 正常 result</td><td><code>completed</code></td><td><code>[agent-reply:...]</code></td><td>交付所有已捕获文本块</td></tr>
                <tr><td>result 标记 is_error</td><td><code>failed</code></td><td><code>[agent-error:...]</code></td><td>Harness 已给出确定失败终态</td></tr>
                <tr><td>普通 error 事件</td><td><code>failed</code></td><td><code>[agent-error:...]</code></td><td>不再等待后续结果</td></tr>
                <tr><td><code>limit_paused</code></td><td><code>running</code></td><td>暂不注入</td><td>窗口重置后同一 turn 会自动恢复</td></tr>
                <tr><td>调用者取消</td><td><code>cancelled</code></td><td><code>[agent-error reason=...]</code></td><td>先改状态再 interrupt，避免错误事件抢先制造第二个终态</td></tr>
                <tr><td>父会话已消失</td><td>保留子终态</td><td>注入失败并记录日志</td><td>交付失败不能抹掉已经完成的执行事实</td></tr>
              </tbody>
            </table>
          </section>

          <section className="article-section" id="delegation-terminal-race">
            <h4>最容易写错的竞态：终态只能注入一次</h4>
            <p>取消、result 和 error 可能在相邻事件循环中到达，如果每个分支都直接向父会话写消息，同一个委派就可能同时出现“已取消”和“已完成”</p>
            <p>实现用两层护栏收口：取消路径先把状态从 running 改为 cancelled，使广播监听器忽略随后由 interrupt 产生的 error；<code>_terminal_injected</code> 再为所有终态生产者提供最终的 at-most-once 检查</p>
            <pre className="article-code"><code>{`if record._terminal_injected:
    return
record._terminal_injected = True
await start_message(parent_id, terminal_prompt)
await archive(child_session_id)`}</code></pre>
            <p>注意这个保证是“最多一次”，不是“必达一次”，父会话若在交付前被删除，系统会保留运行记录和子会话历史，但无法凭空创造一个接收者</p>
          </section>

          <div className="principle-audit-notes">
            <section><span>INVARIANT / 不变量</span><code>delegation_id === child_session.id</code><p>只有一个身份源；继续、取消、提问和审计都引用同一个 id</p></section>
            <section><span>FAILURE MODE / 失败出口</span><p>创建 Session 后若启动 Harness 失败，必须把子会话收口为 error 并回流；不能留下永远处于 running 的委派</p></section>
            <section><span>TRADE-OFF / 代价与边界</span><p>真实子会话带来持久化、广播和归档成本，但换来了跨 turn 生命周期、可取消性与完整审计链</p></section>
            <section><span>IMPLEMENTATION / 实现落点</span><div><code>server/delegations.py</code><code>server/routers/delegations.py</code><code>server/session_manager.py</code></div></section>
          </div>

          <section className="article-section article-source-walk" id="delegation-lifecycle-source">
            <h4>沿源码验证这一章</h4>
            <ol>
              <li>从 <code>start_delegation()</code> 看身份解析、链路护栏、子 Session 创建和“先注册后启动”的顺序</li>
              <li>沿 <code>_on_broadcast()</code> 追踪哪些事件被捕获，重点确认 <code>limit_paused</code> 为什么保持 running</li>
              <li>最后读 <code>_inject_terminal()</code>，检查单次注入、父会话队列和归档发生的先后关系</li>
            </ol>
            <div className="article-exercises"><strong>读完应该能回答</strong><ul><li>为什么不能在子 Session idle 时立刻归档</li><li>为什么运行记录必须早于 <code>start_message</code> 写入注册表</li><li>如果父会话在子任务完成前被删除，系统还保留哪些事实</li></ul></div>
          </section>
        </div>
      </section>

      <section className="principle-chapter principle-article" id="principle-002-2">
        <div className="chapter-heading"><span className="chapter-index">02</span><div><span className="eyebrow">PRINCIPAL CHAIN</span><h3>问题沿调用链返回</h3></div></div>
        <p className="principle-question">本章回答：Dobby 在子会话里缺少一个选择时，问题在子会话、父会话和回答接口之间的完整往返路径</p>
        <div className="principle-depth delegation-article">
          <section className="article-section" id="delegation-question-trace">
            <h4>一次问题往返的完整路径</h4>
            <p>沿用同一个委派：Dobby 正在审查事件去重设计，子会话 <code>dlg-dobby-a1b2c3</code> 需要确认去重游标应放在 session store 还是页面组件中，问题 id 为 <code>q-dedup-owner-01</code></p>
            <pre className="article-code article-sequence"><code>{`Dobby / child Session
  │  mcp__ask__user([{ question, options }])
  ▼
POST /api/sessions/dlg-dobby-a1b2c3/questions
  │  child._pending_questions[q-dedup-owner-01] = PendingQuestion
  │  broadcast question_request
  ▼
DelegationManager._inject_question
  │  start_message(parent, "[agent-question:Dobby ...]")
  ▼
Aberforth / next parent turn
  │  answer(delegation_id, choice="session store")
  ▼
DelegationManager.answer_pending_question
  │  SessionManager.answer_question(child, question_id, answers)
  ▼
Dobby's long-poll wakes and the same child turn resumes
  │  completes review
  ▼
[agent-reply:Dobby delegation=dlg-dobby-a1b2c3]`}</code></pre>
            <ol className="delegation-trace" aria-label="一次委派问题的完整往返">
              <li><span>Q0</span><div><strong>子 Agent 创建待答问题</strong><p>Dobby 的 ask MCP 把问题写到自己的 Session，HTTP 请求不携带父会话 id，因为调用进程已经由 <code>OWLERY_SESSION_ID</code> 绑定到子会话</p></div><code>child._pending_questions[qid]</code></li>
              <li><span>Q1</span><div><strong>Session 广播 question_request</strong><p>待答对象包含问题文本、选项、创建时间和用于唤醒长轮询的 Event，前端与委派监听器看到的是同一条广播</p></div><code>broadcast(session_id=child.id)</code></li>
              <li><span>Q2</span><div><strong>委派监听器识别归属</strong><p>监听器用广播中的 child session id 查找运行记录，只有记录仍为 running 才把问题向父会话转发</p></div><code>records[child.id]</code></li>
              <li><span>Q3</span><div><strong>父会话收到结构化问题</strong><p>Manager 渲染选项并注入 <code>[agent-question:Dobby delegation=… question_id=…]</code>，注入会成为 Aberforth 的新 turn</p></div><code>start_message(parent.id, prompt)</code></li>
              <li><span>Q4</span><div><strong>父 Agent 决定由谁回答</strong><p>若父上下文已经包含答案，直接调用 answer；缺少真实用户偏好时才调用自己的 ask_user；任务已无继续价值时可以取消委派</p></div><code>answer | ask_user | cancel</code></li>
              <li><span>Q5</span><div><strong>回答接口验证所有权</strong><p>路由要求 delegation 仍属于当前父 Session，Manager 再检查记录是 running、子 Session 仍存活且确有 pending question</p></div><code>POST /delegations/:id/answer</code></li>
              <li><span>Q6</span><div><strong>回答写回原问题队列</strong><p>Manager 取子 Session 中最早的 pending question，把父 Agent 选择的 label 转成标准 answers 数组</p></div><code>answer_question(child.id, qid, answers)</code></li>
              <li><span>Q7</span><div><strong>子 turn 原地恢复</strong><p>ask MCP 的长轮询收到答案后返回给 Dobby，恢复的是创建问题的同一个 Harness turn，没有新建第二个子会话</p></div><code>pending.event.set()</code></li>
              <li><span>Q8</span><div><strong>最终答复仍按普通终态回流</strong><p>Dobby 完成审查后产生 result，Manager 再走上一章的单次终态注入和归档流程</p></div><code>[agent-reply:Dobby ...]</code></li>
            </ol>
            <p>这条链路的关键是问题和答案都留在 Dobby 的 Session 中，父会话只获得一次可决策的通知，不接管子会话的 pending queue</p>
          </section>

          <section className="article-section" id="delegation-question-source">
            <h4>待答问题只有一个事实源</h4>
            <p><code>[agent-question:...]</code> 只负责通知父模型，真正决定 Dobby 是否继续阻塞的是子 Session 的 <code>_pending_questions</code></p>
            <pre className="article-code"><code>{`question_id, pending = next(iter(child._pending_questions.items()))

answers = [{ "selected": [choice], "text": None }]
answers += empty_answers_for(pending.questions[1:])

ok = await session_manager.answer_question(
    child.id,
    question_id,
    answers,
)`}</code></pre>
            <table className="article-table">
              <thead><tr><th>对象</th><th>位置</th><th>作用</th><th>是否拥有阻塞状态</th></tr></thead>
              <tbody>
                <tr><td><code>PendingQuestion</code></td><td>子 Session 内存</td><td>保存题目、答案和唤醒 Event</td><td>是</td></tr>
                <tr><td><code>question_request</code></td><td>广播事件</td><td>通知 UI 与 DelegationManager</td><td>否</td></tr>
                <tr><td><code>[agent-question:...]</code></td><td>父 Session 消息流</td><td>给父模型一个决策 turn</td><td>否</td></tr>
                <tr><td><code>answers</code></td><td>子 Session 的问题处理路径</td><td>完成 pending 并唤醒长轮询</td><td>完成后清除</td></tr>
              </tbody>
            </table>
          </section>

          <section className="article-section" id="delegation-question-decisions">
            <h4>父 Agent 的三个分支</h4>
            <table className="article-table">
              <thead><tr><th>父会话掌握的信息</th><th>采取动作</th><th>后续路径</th></tr></thead>
              <tbody>
                <tr><td>答案已经在任务说明、代码或此前对话中</td><td><code>mcp__ask_agent__answer</code></td><td>直接唤醒 Dobby，不打扰用户</td></tr>
                <tr><td>答案依赖用户偏好、授权或产品取舍</td><td><code>mcp__ask__user</code></td><td>用户回答进入父 turn，父 Agent 再把选项 label 转发给 Dobby</td></tr>
                <tr><td>问题暴露出任务已过期、越权或不值得继续</td><td><code>mcp__ask_agent__cancel</code></td><td>子 turn 被 interrupt，父会话收到 cancelled 终态</td></tr>
              </tbody>
            </table>
            <aside className="article-callout"><strong>调用链规则</strong><p>每个 Agent 只与直接调用者通信，Dobby 不直接占用 Aberforth 会话里的用户交互通道；若存在 Researcher → Dobby → Aberforth 的三层链，问题也只向上移动一跳</p></aside>
          </section>

          <section className="article-section" id="delegation-question-race">
            <h4>并发回答与当前限制</h4>
            <p>同一个 pending question 也会出现在子会话 UI 中，因此用户可能在 Dobby 页面直接回答，同时 Aberforth 也调用 answer，两个入口最终竞争同一个 <code>SessionManager.answer_question</code></p>
            <table className="article-table">
              <thead><tr><th>情况</th><th>结果</th><th>调用者应如何处理</th></tr></thead>
              <tbody>
                <tr><td>父 Agent 先完成 pending</td><td>子长轮询恢复，UI 的迟到提交失败</td><td>把问题视为已回答</td></tr>
                <tr><td>子会话 UI 先完成 pending</td><td>父 answer 收到 409</td><td>不要重试，等待 Dobby 的终态回复</td></tr>
                <tr><td>委派已 completed、failed 或 cancelled</td><td>answer 收到 409</td><td>终态不可重新打开，必要时走 follow-up</td></tr>
                <tr><td>一批包含多个问题</td><td>v1 只把 choice 应用于第一题，其余题写入空选择</td><td>需要精确多题转发时应拆成单题调用</td></tr>
                <tr><td>父会话注入失败</td><td>子问题仍留在 pending queue</td><td>可从子会话 UI 回答，或由超时策略收口</td></tr>
              </tbody>
            </table>
          </section>

          <div className="principle-audit-notes">
            <section><span>INVARIANT / 不变量</span><p>问题始终归属创建它的 child Session，父会话只做一跳决策</p></section>
            <section><span>FAILURE MODE / 失败出口</span><p>父 answer 与子 UI 同时提交时只有第一条能移除 pending，第二条必须收到明确冲突</p></section>
            <section><span>TRADE-OFF / 代价与边界</span><p>一跳转发避免子 Agent 直接轰炸用户，但会增加一个父 turn，并要求父 Agent正确判断自己是否知道答案</p></section>
            <section><span>IMPLEMENTATION / 实现落点</span><div><code>server/delegations.py::_inject_question</code><code>server/delegations.py::answer_pending_question</code><code>server/session_manager.py::answer_question</code><code>server/mcp_servers/ask.py</code></div></section>
          </div>

          <section className="article-section article-source-walk" id="delegation-question-source-code">
            <h4>沿源码验证问题回路</h4>
            <ol>
              <li>从 <code>server/mcp_servers/ask.py</code> 看子 Agent 如何创建问题并长轮询答案</li>
              <li>在 <code>SessionManager</code> 中跟踪 <code>_pending_questions</code> 的写入、广播、唤醒和删除</li>
              <li>最后读 <code>_inject_question()</code> 与 <code>answer_pending_question()</code>，确认通知副本和真实 pending 状态没有混为一谈</li>
            </ol>
            <button className="principle-experiment-link" type="button" onClick={() => onScenario("question")}>在上方重放问题逐级返回 →</button>
            <div className="article-exercises"><strong>读完应该能回答</strong><ul><li>为什么父会话收到问题后不能直接把一条文本消息发给 Dobby</li><li>父 answer 与子会话 UI 同时提交时，哪一层负责保证只有一个答案生效</li><li>当 Aberforth 也不知道答案时，为什么应该先 ask_user 再 answer，而不是让 Dobby 直接询问用户</li></ul></div>
          </section>
        </div>
      </section>

      <section className="principle-chapter principle-article" id="principle-002-3">
        <div className="chapter-heading"><span className="chapter-index">03</span><div><span className="eyebrow">IDENTITY & CONTINUATION</span><h3>同一子会话上的后续轮次</h3></div></div>
        <p className="principle-question">本章回答：首轮委派已经完成并归档后，系统如何恢复 Dobby 的原有 transcript，同时重新建立一轮可独立收口的运行状态</p>
        <div className="principle-depth delegation-article">
          <section className="article-section" id="delegation-followup-trace">
            <h4>从首轮回复到第二轮回复</h4>
            <p>首轮审查已经由 <code>dlg-dobby-a1b2c3</code> 返回，Dobby 的子 Session 已归档；Aberforth 修改代码后要求“只复查去重竞态”，这属于同一工作线的第二轮</p>
            <pre className="article-code article-sequence"><code>{`Round 1 terminal
  child dlg-dobby-a1b2c3 = archived
  record.state = completed
  transcript = [brief, tools, first review]

Aberforth calls ask(
  delegation_id="dlg-dobby-a1b2c3",
  request="只复查去重竞态",
)
  ▼
POST /delegations/dlg-dobby-a1b2c3/follow-up
  ▼
validate parent + ownership + terminal state
  ▼
unarchive the same child Session
  ▼
reset round-local DelegationRunState fields
  ▼
start_message(child.id, thin follow-up prompt)
  ▼
Dobby sees the old transcript, finishes round 2
  ▼
new [agent-reply:Dobby ...] → archive same child again`}</code></pre>
            <ol className="delegation-trace" aria-label="一次委派后续轮次的完整路径">
              <li><span>F0</span><div><strong>首轮已经到达终态</strong><p>运行记录是 completed、failed 或 cancelled，子 Session 通常已由终态注入路径归档</p></div><code>record.state != running</code></li>
              <li><span>F1</span><div><strong>父 Agent 传入旧 delegation_id</strong><p>MCP ask 的两种模式互斥，提供 <code>delegation_id</code> 表示继续，不能同时再传 name</p></div><code>ask(delegation_id=..., request=...)</code></li>
              <li><span>F2</span><div><strong>路由先确认父会话仍可接收结果</strong><p>已归档或删除的父 Session 会在启动子任务前收到 404，避免子任务完成后找不到投递目标</p></div><code>_require_session(parent.id)</code></li>
              <li><span>F3</span><div><strong>Manager 校验记录所有权与轮次状态</strong><p>记录必须存在、属于当前 parent_session_id，并且上一轮不能仍为 running</p></div><code>record.parent_session_id == parent.id</code></li>
              <li><span>F4</span><div><strong>恢复同一个 child Session</strong><p>若内存中没有活跃 child，Manager 从数据库 unarchive；硬删除后的 Session 无法恢复 transcript</p></div><code>unarchive_session(record.delegation_id)</code></li>
              <li><span>F5</span><div><strong>重置本轮易变字段</strong><p>状态回到 running，清空 captured_text、error 和 finished_at，并把 <code>_terminal_injected</code> 重新置为 false</p></div><code>round_reset(record)</code></li>
              <li><span>F6</span><div><strong>追加薄 follow-up prompt</strong><p>新 prompt 只说明调用者和新增请求，原始 brief、首轮工具结果与回复已在 child transcript 中，不再重复复制</p></div><code>start_message(child.id, follow_up)</code></li>
              <li><span>F7</span><div><strong>第二轮独立收口</strong><p>新一轮重新捕获文本并允许一次终态注入，结束后仍归档同一个 child Session</p></div><code>running → completed → archived</code></li>
            </ol>
          </section>

          <section className="article-section" id="delegation-followup-fields">
            <h4>身份保持不变，轮次字段必须重置</h4>
            <pre className="article-code"><code>{`# stable across every round
delegation_id
parent_session_id
target_agent_id
target_agent_name

# reset before each follow-up turn
state = "running"
captured_text = []
error = None
finished_at = None
_terminal_injected = False
request = new_request`}</code></pre>
            <table className="article-table">
              <thead><tr><th>字段</th><th>第二轮是否变化</th><th>原因</th></tr></thead>
              <tbody>
                <tr><td><code>delegation_id</code></td><td>不变</td><td>它就是 child session.id，也是 transcript 的身份</td></tr>
                <tr><td><code>parent_session_id</code></td><td>不变</td><td>后续结果仍只能回到原调用者</td></tr>
                <tr><td><code>request</code></td><td>更新</td><td>list 接口要显示当前轮正在处理的请求</td></tr>
                <tr><td><code>captured_text</code></td><td>清空</td><td>不能把首轮答复再次拼进第二轮结果</td></tr>
                <tr><td><code>_terminal_injected</code></td><td>重置为 false</td><td>每轮各允许一次终态，而不是整个 child 生命周期只允许一次</td></tr>
                <tr><td>child transcript</td><td>保留并追加</td><td>Dobby 需要看到首轮上下文才能做增量复查</td></tr>
              </tbody>
            </table>
          </section>

          <section className="article-section" id="delegation-followup-modes">
            <h4>继续、重新委派与并行扇出</h4>
            <table className="article-table">
              <thead><tr><th>意图</th><th>调用方式</th><th>会话结果</th></tr></thead>
              <tbody>
                <tr><td>基于首轮结论继续修改或复查</td><td><code>ask(delegation_id="dlg-dobby-a1b2c3", request=...)</code></td><td>复用同一个 child，保留 transcript</td></tr>
                <tr><td>把无关任务交给 Dobby</td><td><code>ask(name="Dobby", request=...)</code></td><td>创建新的 child 和新的 delegation_id</td></tr>
                <tr><td>让 Dobby 同时检查两个独立模块</td><td>并行发起两次 name 模式</td><td>两个 child 各自运行，不共享 captured_text 或终态</td></tr>
                <tr><td>上一轮尚未结束就追加要求</td><td>不允许 follow-up</td><td>返回 409，等待终态或取消后再继续</td></tr>
              </tbody>
            </table>
            <p>同一 Session 的 turn 会串行化，因此把并行任务塞进同一个 delegation_id 不会得到真正并发，还会把两个无关问题写进同一 transcript</p>
          </section>

          <section className="article-section" id="delegation-followup-errors">
            <h4>后续轮次的拒绝路径</h4>
            <table className="article-table">
              <thead><tr><th>条件</th><th>HTTP 结果</th><th>恢复策略</th></tr></thead>
              <tbody>
                <tr><td>delegation_id 不存在或属于另一个父会话</td><td>404</td><td>从 list 查询本会话记录，或用 name 新建</td></tr>
                <tr><td>上一轮仍为 running</td><td>409</td><td>等待 reply/error，不能边执行边改 brief</td></tr>
                <tr><td>child 已硬删除</td><td>409</td><td>旧 transcript 不可恢复，只能新建委派</td></tr>
                <tr><td>父 Session 已归档或删除</td><td>404</td><td>从一个活跃父会话重新发起，避免结果无处投递</td></tr>
                <tr><td>unarchive 成功但 start_message 失败</td><td>500</td><td>记录写为 failed 并尝试向父会话注入错误终态</td></tr>
              </tbody>
            </table>
          </section>

          <div className="principle-audit-notes">
            <section><span>INVARIANT / 不变量</span><p>继续模式复用 delegation_id、child session.id、目标 Agent 和父会话归属</p></section>
            <section><span>FAILURE MODE / 失败出口</span><p>必须先确认父会话活跃再重开 child，否则系统可能完成一轮无法交付的工作</p></section>
            <section><span>TRADE-OFF / 代价与边界</span><p>保留 transcript 降低重复阅读成本，也会把旧假设带入新轮次；任务语义变化时应创建新 child</p></section>
            <section><span>IMPLEMENTATION / 实现落点</span><div><code>server/delegations.py::follow_up_delegation</code><code>server/routers/delegations.py::follow_up_delegation</code><code>server/mcp_servers/ask_agent.py::ask_agent</code><code>server/session_manager.py::unarchive_session</code></div></section>
          </div>

          <section className="article-section article-source-walk" id="delegation-followup-source">
            <h4>沿源码验证后续轮次</h4>
            <ol>
              <li>从 ask MCP 的互斥参数检查开始，确认 name 与 delegation_id 如何选择两条路由</li>
              <li>阅读 <code>follow_up_delegation()</code> 的校验顺序，尤其是父会话 liveness 与 running 冲突</li>
              <li>对照 <code>unarchive_session()</code> 和 round reset，区分持久 transcript 与本轮运行缓存</li>
            </ol>
            <div className="article-exercises"><strong>读完应该能回答</strong><ul><li>为什么 completed child 已归档仍能继续，而硬删除后不能</li><li>为什么 <code>_terminal_injected</code> 必须按轮次重置，delegation_id 却不能变</li><li>两个并行任务都交给 Dobby 时，为什么必须创建两个 child Session</li></ul></div>
          </section>
        </div>
      </section>

      <section className="principle-chapter principle-article" id="principle-002-4">
        <div className="chapter-heading"><span className="chapter-index">04</span><div><span className="eyebrow">GUARDS & BOUNDARIES</span><h3>会话树的深度、环与取消</h3></div></div>
        <p className="principle-question">本章回答：Agent 可以继续委派其他 Agent 时，系统如何拒绝失控的调用链，并在上层任务取消后停止所有仍在运行的后代</p>
        <div className="principle-depth delegation-article">
          <section className="article-section" id="delegation-guard-tree">
            <h4>一棵达到上限的会话树</h4>
            <p>根会话由 Aberforth 持有，不计入 delegation hop；Aberforth 委派 Dobby，Dobby 委派 Researcher，Researcher 再委派 Archivist，形成允许的三跳链</p>
            <pre className="article-code article-sequence"><code>{`User
  └─ Aberforth / session-root              hop 0 · origin=user
       └─ Dobby / dlg-dobby-a1b2c3         hop 1 · origin=delegation
            └─ Researcher / dlg-res-r4d5   hop 2 · origin=delegation
                 └─ Archivist / dlg-arc-e6f7  hop 3 · origin=delegation

Rejected from Archivist:
  ask(name="Reviewer", ...)   -> depth would become 4
  ask(name="Researcher", ...) -> target already in ancestor agent chain
  ask(name="Archivist", ...)  -> direct self-delegation`}</code></pre>
            <ol className="delegation-trace" aria-label="一次嵌套委派的护栏检查">
              <li><span>G0</span><div><strong>确定当前 parent Session</strong><p>每一层 MCP 进程由自己的 <code>OWLERY_SESSION_ID</code> 绑定，Researcher 发起委派时 parent 是 Researcher child，而不是最外层 Aberforth</p></div><code>parent = get_session(session_id)</code></li>
              <li><span>G1</span><div><strong>拒绝直接自委派</strong><p>目标 agent_id 与当前 parent.agent_id 相同会立即返回 409，给调用者一个明确错误</p></div><code>target.id == parent.agent_id</code></li>
              <li><span>G2</span><div><strong>沿 parent_session_id 向上走</strong><p>Manager 从当前 parent 开始读取每个 Session 的 agent_id、origin 和上级指针</p></div><code>sid = parent.id → parent.parent_session_id</code></li>
              <li><span>G3</span><div><strong>同时累计 Agent 集合与委派跳数</strong><p>每个 <code>origin=delegation</code> 的 Session 计一跳，出现过的 agent_id 放入 chain_agent_ids</p></div><code>existing_hops += 1</code></li>
              <li><span>G4</span><div><strong>内存缺失时读取归档数据库</strong><p>终态 child 会被归档，因此祖先不一定在活跃内存；链路检查会一次加载包含 archived 的 Session 行继续追踪</p></div><code>load_sessions(include_archived=True)</code></li>
              <li><span>G5</span><div><strong>检查目标是否已在祖先 Agent 链</strong><p>Dobby → Researcher → Dobby 即使使用新的 Session id，也仍是 Agent 级环，必须拒绝</p></div><code>target_agent_id in chain_agent_ids</code></li>
              <li><span>G6</span><div><strong>检查新增一跳后的深度</strong><p>只有 <code>existing_hops + 1 ≤ 3</code> 才能创建新 child，用户根和普通父会话不计作委派跳</p></div><code>existing_hops + 1 &gt; DEPTH_CAP</code></li>
              <li><span>G7</span><div><strong>通过所有护栏后才创建 Session</strong><p>拒绝发生在 create_session 之前，不会产生半成品 child 或需要清理的运行记录</p></div><code>create_session(... origin="delegation")</code></li>
            </ol>
          </section>

          <section className="article-section" id="delegation-guard-archives">
            <h4>链路检查为何必须读取归档祖先</h4>
            <p>委派 child 在终态注入后会自动归档，但它仍可能在后续轮次被 unarchive 并继续向下委派；若检查只看活跃内存，归档的 Dobby 祖先会从链中消失，系统可能错误允许 Researcher 再委派回 Dobby</p>
            <pre className="article-code"><code>{`while sid is not None:
    if sid in visited_session_ids:
        reject("session-id cycle")

    session = active_sessions.get(sid)
    if session is None:
        session = archived_rows_by_id.get(sid)
        if session is None:
            reject("missing parent pointer")

    chain_agent_ids.add(session.agent_id)
    existing_hops += session.origin == "delegation"
    sid = session.parent_session_id`}</code></pre>
            <table className="article-table">
              <thead><tr><th>异常</th><th>检测依据</th><th>为什么不能继续猜</th></tr></thead>
              <tbody>
                <tr><td>目标 Agent 已在祖先链</td><td><code>chain_agent_ids</code></td><td>新 Session id 不能消除语义上的递归环</td></tr>
                <tr><td>Session 指针 A → B → A</td><td><code>visited_session_ids</code></td><td>数据库链已损坏，继续遍历会死循环</td></tr>
                <tr><td>parent_session_id 指向不存在的行</td><td>内存与数据库都 miss</td><td>跳过缺口会低估深度并漏掉祖先 Agent</td></tr>
                <tr><td>链长超过 64</td><td>内部 safety cap</td><td>即使业务深度检查异常，也要有最终的 fail-closed 边界</td></tr>
              </tbody>
            </table>
          </section>

          <section className="article-section" id="delegation-guard-types">
            <h4>四类护栏解决不同问题</h4>
            <table className="article-table">
              <thead><tr><th>护栏</th><th>检查时机</th><th>保护对象</th><th>返回</th></tr></thead>
              <tbody>
                <tr><td>目标 Agent 唯一解析</td><td>建 child 前</td><td>避免同名 Agent 随机命中</td><td>无匹配 404，歧义 409</td></tr>
                <tr><td>禁止直接自委派</td><td>目标解析后</td><td>避免当前 Agent 立刻复制自己</td><td>409</td></tr>
                <tr><td>禁止祖先 Agent 环</td><td>向上遍历后</td><td>避免 A → B → A 递归</td><td>409</td></tr>
                <tr><td>最大三跳</td><td>创建新 child 前</td><td>限制成本、延迟和责任链长度</td><td>409</td></tr>
              </tbody>
            </table>
          </section>

          <section className="article-section" id="delegation-cascade-cancel">
            <h4>取消必须沿后代链收口</h4>
            <p>若 Aberforth 取消 Dobby，而 Researcher 和 Archivist 仍继续运行，后代会消耗资源，并把结果投递给已经不再处理任务的父 child；Manager 因此先把根记录置为 cancelled，再按 <code>parent_session_id</code> 建邻接表向下取消</p>
            <pre className="article-code"><code>{`root.state = "cancelled"       # before interrupt
await interrupt(root.id)

children_of = group_running_records_by_parent_session_id()
for child in running_children_of(root.id):
    await cancel_delegation(
        child.delegation_id,
        reason=f"parent delegation cancelled ({root.error})",
    )

await inject_terminal_once(root)`}</code></pre>
            <table className="article-table">
              <thead><tr><th>顺序</th><th>动作</th><th>避免的问题</th></tr></thead>
              <tbody>
                <tr><td>1</td><td>根记录 running → cancelled</td><td>interrupt 产生的 error 广播不会把它再次写成 failed</td></tr>
                <tr><td>2</td><td>interrupt 根 child Harness</td><td>停止当前工具与模型执行</td></tr>
                <tr><td>3</td><td>递归调用每个后代的公共 cancel 路径</td><td>每层都得到相同的状态先行、interrupt 与终态注入语义</td></tr>
                <tr><td>4</td><td>后代错误先回到直接父 child</td><td>中间 Agent 能知道自己的子任务为何结束</td></tr>
                <tr><td>5</td><td>最后把根 cancelled 注入 Aberforth</td><td>最外层调用者看到整棵子树已经停止</td></tr>
              </tbody>
            </table>
          </section>

          <section className="article-section" id="delegation-cancel-race">
            <h4>取消、完成与错误的竞态</h4>
            <p>状态先行只能挡住最常见的 interrupt error 竞态，result 与 error 仍可能在相邻调度中到达，因此每个记录还有 <code>_terminal_injected</code> 作为最终的 at-most-once 护栏</p>
            <p>这项保证只覆盖“同一轮最多注入一次”，不承诺父 Session 必然存在；若父会话被硬删除，子记录和 Session 历史仍可保留终态，但通知无法投递</p>
            <aside className="article-callout"><strong>级联取消的边界</strong><p>Manager 只遍历内存注册表中仍为 running 的 delegation 记录；已经终态的后代不会被改写，进程重启后遗留的 delegation child 则由 SessionManager 启动恢复逻辑归档</p></aside>
          </section>

          <div className="principle-audit-notes">
            <section><span>INVARIANT / 不变量</span><p>新目标不能出现在调用者 Agent 祖先链中，且任何一条链最多包含三个 delegation-origin Session</p></section>
            <section><span>FAILURE MODE / 失败出口</span><p>祖先缺失、Session 指针成环或链长异常都按 409 fail closed，不能把损坏状态当作一条更短的合法链</p></section>
            <section><span>TRADE-OFF / 代价与边界</span><p>三跳上限牺牲任意深度编排，换取可预测成本、较短责任链与可以解释的取消传播</p></section>
            <section><span>IMPLEMENTATION / 实现落点</span><div><code>server/delegations.py::_check_chain</code><code>server/delegations.py::cancel_delegation</code><code>server/delegations.py::_cascade_cancel_descendants</code><code>server/session_manager.py::_recover_orphaned_delegations</code></div></section>
          </div>

          <section className="article-section article-source-walk" id="delegation-guard-source">
            <h4>沿源码验证树形护栏</h4>
            <ol>
              <li>从 <code>_check_chain()</code> 画出 visited_session_ids、chain_agent_ids 与 existing_hops 三份状态各自负责什么</li>
              <li>构造一个归档祖先场景，确认数据库 fallback 不会错误丢失 agent_id 与 parent_session_id</li>
              <li>从根 <code>cancel_delegation()</code> 跟到后代递归和 <code>_inject_terminal()</code>，记录每一层状态翻转、interrupt、注入和归档的顺序</li>
            </ol>
            <div className="article-exercises"><strong>读完应该能回答</strong><ul><li>为什么 Session id 没有形成环，Agent 调用链仍可能形成环</li><li>为什么根取消要先修改 state，再 interrupt，再级联后代</li><li>三跳业务上限已经存在后，内部遍历为何还需要 64 层 safety cap</li></ul></div>
          </section>
        </div>
      </section>
    </section>
  );
}

export function AgentDelegationSpecimen() {
  const scripts = useMemo(() => DELEGATION_SCRIPTS, []);
  const [activeId, setActiveId] = useState<DelegationScenarioId>("success");
  const activeScript = scripts.find((item) => item.id === activeId) ?? scripts[0];
  const engineRef = useRef(new DelegationReplayEngine(activeScript.events));
  const nextLogIndexRef = useRef(0);
  const [logs, setLogs] = useState<DelegationLog[]>([]);
  const [selectedLog, setSelectedLog] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);

  const parentMessages = useSessionStore((state) => state.messages[PARENT_SESSION_ID] ?? EMPTY_MESSAGES);
  const childMessages = useSessionStore((state) => state.messages[DOBBY_SESSION_ID] ?? EMPTY_MESSAGES);
  const rootDelegations = useSessionStore((state) => state.delegations[PARENT_SESSION_ID] ?? EMPTY_DELEGATIONS);
  const nestedDelegations = useSessionStore((state) => state.delegations[DOBBY_SESSION_ID] ?? EMPTY_DELEGATIONS);
  const childStatus = useSessionStore((state) => state.sessions.find((item) => item.id === DOBBY_SESSION_ID)?.status ?? "idle");

  const reset = useCallback((script: DelegationScript) => {
    setPlaying(false);
    resetDelegationSpecimenStore();
    engineRef.current.reset(script.events);
    nextLogIndexRef.current = 0;
    setLogs([]);
    setSelectedLog(null);
  }, []);

  useEffect(() => reset(activeScript), [activeScript, reset]);

  const step = useCallback(() => {
    const next = engineRef.current.step();
    if (!next) { setPlaying(false); return; }
    const before = snapshot();
    applyDelegationSpecimenEvent(next.event);
    const entry = { ...next, before, after: snapshot() };
    setSelectedLog(nextLogIndexRef.current);
    nextLogIndexRef.current += 1;
    setLogs((current) => [...current, entry]);
    if (engineRef.current.done) setPlaying(false);
  }, []);

  useEffect(() => {
    if (!playing) return;
    const timer = window.setTimeout(step, 950);
    return () => window.clearTimeout(timer);
  }, [playing, logs.length, step]);

  const openScenario = useCallback((id: DelegationScenarioId) => {
    setActiveId(id);
    window.requestAnimationFrame(() => document.querySelector(".delegation-workbench")?.scrollIntoView({ behavior: "smooth", block: "start" }));
  }, []);

  const progress = engineRef.current.length ? Math.round(engineRef.current.position / engineRef.current.length * 100) : 100;

  return (
    <main className="anatomy-page delegation-page">
      <header className="anatomy-nav">
        <a className="anatomy-brand" href="/function-cabinet.html" aria-label="Owlery 功能标本馆首页"><span className="brand-mark"><OwleryLogo size={22} /></span><span><strong>Owlery</strong><small>FUNCTION CABINET</small></span></a>
        <div className="nav-center"><span>标本 002</span><strong>多 Agent 委派解剖</strong></div>
        <div className="cabinet-nav-links">
          <a className="nav-principle" href="#principle">查看原理</a>
          <a className="nav-principle nav-prev" href="/streaming-anatomy.html">← 001</a>
          <a className="nav-principle nav-next" href="/bg-task-pipeline.html">下一件 003 <IconArrowRight /></a>
        </div>
      </header>

      <section className="anatomy-hero delegation-hero" id="top">
        <div className="hero-index">SPECIMEN / 002</div>
        <div className="hero-copy"><span className="eyebrow"><span className="eyebrow-line" /> MULTI-AGENT SYSTEM STUDY</span><h1>多 Agent 委派的<br />会话模型</h1></div>
        <div className="hero-proof"><div><strong>3</strong><span>最大委派深度</span></div><div><strong>1:1</strong><span>委派与会话身份</span></div><div><strong>0</strong><span>次无主回调</span></div></div>
      </section>

      <nav className="scenario-tabs" aria-label="选择委派场景">
        {scripts.map((script) => <button type="button" className="scenario-tab" data-active={script.id === activeId} key={script.id} onClick={() => setActiveId(script.id)}><span>{script.index}</span><div><strong>{script.title}</strong><small>{script.description}</small></div></button>)}
      </nav>

      <section className="delegation-workbench">
        <div className="delegation-demo">
          <div className="panel-heading demo-heading"><div><span className="eyebrow">OPERABLE SESSION TREE</span><h2>{activeScript.title}</h2></div><p>{activeScript.lesson}</p></div>
          <CallChain root={rootDelegations[0]} nested={nestedDelegations[0]} />
          <div className="session-panes">
            <SessionPane title="Parent session" subtitle="Aberforth · caller" agentName="Aberforth" agentId="agent-aberforth" status="idle" messages={parentMessages} delegations={rootDelegations} sessionId={PARENT_SESSION_ID} />
            <SessionPane title="Child session" subtitle={`Dobby · ${DOBBY_SESSION_ID.slice(0, 12)}`} agentName="Dobby" agentId="agent-dobby" status={childStatus} messages={childMessages} delegations={nestedDelegations} sessionId={DOBBY_SESSION_ID} />
          </div>
          <div className="replay-controls">
            <Button size="icon" aria-label={playing ? "暂停" : "播放"} onClick={() => { if (engineRef.current.done) reset(activeScript); setPlaying((value) => !value); }}>{playing ? <IconPlayerPause /> : <IconPlayerPlay />}</Button>
            <Button variant="outline" size="icon" aria-label="单步执行" onClick={step} disabled={playing || engineRef.current.done}><IconStepInto /></Button>
            <Button variant="ghost" size="icon" aria-label="重置演示" onClick={() => reset(activeScript)}><IconRefresh /></Button>
            <div className="replay-progress"><span style={{ width: `${progress}%` }} /></div>
            <span className="delegation-progress-label">{engineRef.current.position} / {engineRef.current.length} events</span>
          </div>
        </div>
        <DelegationInspector logs={logs} selected={selectedLog} onSelect={setSelectedLog} />
      </section>

      <DelegationPrinciples onScenario={openScenario} />

      <footer className="anatomy-footer"><div><OwleryLogo size={18} /><span>OWLERY FUNCTION CABINET · 002</span></div><p>能委派不等于该委派；调用者始终对结果负责。</p></footer>
    </main>
  );
}
