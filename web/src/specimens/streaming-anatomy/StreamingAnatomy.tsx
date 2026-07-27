import {
  IconAlertTriangle,
  IconArrowRight,
  IconBrandSpeedtest,
  IconCheck,
  IconChevronRight,
  IconPlayerPause,
  IconPlayerPlay,
  IconRefresh,
  IconStepInto,
  IconX,
} from "@tabler/icons-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { MessageBubble } from "../../components/MessageBubble";
import { OwleryLogo } from "../../components/OwleryLogo";
import { QuestionPrompt, type AnswerPayload } from "../../components/QuestionPrompt";
import { ToolApproval } from "../../components/ToolApproval";
import { Button } from "../../components/ui/button";
import { applyWsEvent, type WsEventApplication } from "../../hooks/useWebSocket";
import {
  useSessionStore,
  type Message,
  type PendingQuestion,
  type SessionInfo,
  type SessionStatus,
} from "../../stores/sessionStore";
import { ReplayEngine, type ReplayStep } from "./ReplayEngine";
import {
  makeScripts,
  SPECIMEN_SESSION_ID,
  type SpecimenEvent,
  type SpecimenScript,
} from "./scripts";
import {
  DESIGN_DECISIONS,
  EVENT_CONTRACTS,
  EVENT_PRINCIPLES,
} from "./principles";

const EMPTY_MESSAGES: Message[] = [];
const EMPTY_QUESTIONS: PendingQuestion[] = [];
const SPEEDS = [0.5, 1, 2] as const;
type Speed = (typeof SPEEDS)[number];

interface StoreSnapshot {
  status: SessionStatus;
  messages: number;
  questions: number;
  queued: number;
  parked: boolean;
  lastSeq: number | null;
}

interface InspectorStep extends ReplayStep {
  before: StoreSnapshot;
  after: StoreSnapshot;
}

const SPECIMEN_SESSION: SessionInfo = {
  id: SPECIMEN_SESSION_ID,
  name: "Streaming anatomy specimen",
  working_dir: "/owlery/specimens",
  status: "idle",
  created_at: "2026-07-21T00:00:00.000Z",
  message_count: 0,
  claude_session_id: null,
  credential_id: null,
  agent_id: null,
  origin: "specimen",
  backend: "claude-code",
  can_fork: false,
  fork_is_full_copy: false,
  archived: false,
};

function readSnapshot(): StoreSnapshot {
  const store = useSessionStore.getState();
  const session = store.sessions.find((item) => item.id === SPECIMEN_SESSION_ID);
  return {
    status: session?.status ?? "idle",
    messages: store.messages[SPECIMEN_SESSION_ID]?.length ?? 0,
    questions: store.pendingQuestions[SPECIMEN_SESSION_ID]?.length ?? 0,
    queued: store.pendingQueue[SPECIMEN_SESSION_ID]?.length ?? 0,
    parked: !!store.parkedTurns[SPECIMEN_SESSION_ID],
    lastSeq: store.lastAppliedSeq[SPECIMEN_SESSION_ID] ?? null,
  };
}

function resetSpecimenStore(): void {
  useSessionStore.setState({
    sessions: [{ ...SPECIMEN_SESSION }],
    activeSessionId: SPECIMEN_SESSION_ID,
    messages: {},
    lastAppliedSeq: {},
    pendingQueue: {},
    pendingQuestions: {},
    parkedTurns: {},
    bgTasks: {},
    research: {},
  });
}

function eventLabel(event: SpecimenEvent): string {
  const labels: Record<string, string> = {
    status: `状态 → ${String(event.status)}`,
    user_message: "用户消息入库",
    assistant_text: "助手文本到达",
    tool_use: `调用工具 · ${String(event.tool ?? "unknown")}`,
    tool_approval_request: "等待人工审批",
    tool_result: "工具返回结果",
    question_request: "请求用户补充信息",
    question_answer: "用户回答问题",
    error: `异常 · ${String(event.code ?? "error")}`,
    limit_resumed: "额度恢复",
    result: "本轮结算",
  };
  return labels[event.type] ?? event.type;
}

function delta(before: StoreSnapshot, after: StoreSnapshot): string[] {
  const changes: string[] = [];
  if (before.status !== after.status) changes.push(`${before.status} → ${after.status}`);
  if (before.messages !== after.messages) changes.push(`消息 +${after.messages - before.messages}`);
  if (before.questions !== after.questions) changes.push(`待回答 ${after.questions}`);
  if (before.parked !== after.parked) changes.push(after.parked ? "任务已停放" : "任务已恢复");
  if (before.lastSeq !== after.lastSeq) changes.push(`seq ${after.lastSeq ?? "—"}`);
  return changes;
}

function remainingLabel(resumeAt: string | null, now: number): string {
  if (!resumeAt) return "等待额度窗口恢复";
  const seconds = Math.max(0, Math.ceil((new Date(resumeAt).getTime() - now) / 1000));
  return seconds > 0 ? `${seconds} 秒后自动恢复` : "正在恢复";
}

function StatusTimeline({ history }: { history: InspectorStep[] }) {
  const states = [
    { status: "idle" as SessionStatus, label: "准备" },
    ...history
      .filter((step) => step.outcome.status === "applied" && step.event.type === "status")
      .map((step) => ({
        status: step.event.status as SessionStatus,
        label:
          step.event.status === "running"
            ? "生成中"
            : step.event.status === "waiting_approval"
              ? "待批准"
              : "空闲",
      })),
  ];

  return (
    <div className="anatomy-timeline" aria-label="状态机时间线">
      {states.map((state, index) => (
        <div className="timeline-node-wrap" key={`${state.status}-${index}`}>
          {index > 0 && <span className="timeline-link" aria-hidden />}
          <div className="timeline-node" data-status={state.status}>
            <span className="timeline-dot" />
            <span>{state.label}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

function EventInspector({
  history,
  currentIndex,
  selectedOrder,
  onSelect,
}: {
  history: InspectorStep[];
  currentIndex: number | null;
  selectedOrder: number | null;
  onSelect: (order: number) => void;
}) {
  const current = history.at(-1);

  return (
    <aside className="anatomy-inspector" aria-label="事件检查器">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">LIVE INSPECTOR</span>
          <h2>系统此刻发生了什么</h2>
        </div>
        <span className="live-indicator"><span />监听中</span>
      </div>

      <StatusTimeline history={history} />

      <div className="inspector-log" aria-live="polite">
        {history.length === 0 ? (
          <div className="inspector-empty">
            <IconStepInto />
            <p>播放或单步执行后，每个真实事件都会在这里留下证据。</p>
          </div>
        ) : (
          history.map((step, order) => {
            const dropped = step.outcome.status === "dropped";
            const changes = delta(step.before, step.after);
            return (
              <details
                className="event-row"
                data-outcome={step.outcome.status}
                data-selected={order === selectedOrder}
                key={`${order}-${step.index}-${step.event.type}`}
                open={order === selectedOrder}
              >
                <summary onClick={() => onSelect(order)}>
                  <span className="event-order">{String(order + 1).padStart(2, "0")}</span>
                  <span className="event-main">
                    <strong>{eventLabel(step.event)}</strong>
                    <small>
                      {dropped
                        ? `seq ${String(step.event.seq)} ≤ baseline ${String(step.outcome.baseline)}`
                        : changes.join(" · ") || "瞬时事件"}
                    </small>
                  </span>
                  <span className="event-outcome">
                    {dropped ? <IconX /> : <IconCheck />}
                    {dropped ? "已拦截" : "已应用"}
                  </span>
                  <IconChevronRight className="event-chevron" />
                </summary>
                <pre>{JSON.stringify(step.event, null, 2)}</pre>
              </details>
            );
          })
        )}
      </div>

      <div className="store-readout">
        <div><span>CURSOR</span><strong>{currentIndex == null ? "—" : currentIndex + 1}</strong></div>
        <div><span>MESSAGES</span><strong>{current?.after.messages ?? 0}</strong></div>
        <div><span>LAST SEQ</span><strong>{current?.after.lastSeq ?? "—"}</strong></div>
      </div>
    </aside>
  );
}

function ChatSpecimen({
  status,
  messages,
  parkedResumeAt,
  now,
  onApprove,
  onDeny,
  onAnswer,
}: {
  status: SessionStatus;
  messages: Message[];
  parkedResumeAt: string | null;
  now: number;
  onApprove: () => void;
  onDeny: () => void;
  onAnswer: (questionId: string, answers: AnswerPayload[]) => void;
}) {
  const pendingQuestions = useSessionStore(
    (state) => state.pendingQuestions[SPECIMEN_SESSION_ID] ?? EMPTY_QUESTIONS
  );

  return (
    <section className="chat-specimen" aria-label="可操作对话演示">
      <div className="specimen-chat-header">
        <div className="agent-identity">
          <span className="agent-seal">A</span>
          <div><strong>Archivist</strong><small>流式会话标本</small></div>
        </div>
        <span className="status-pill" data-status={status}>
          <span />{status === "running" ? "生成中" : status === "waiting_approval" ? "等待批准" : "空闲"}
        </span>
      </div>

      <div className="specimen-transcript">
        {messages.length === 0 && (
          <div className="transcript-empty">
            <OwleryLogo size={34} />
            <strong>尚未释放事件</strong>
            <span>点击播放，或者一帧一帧查看。</span>
          </div>
        )}
        {messages.map((message, index) => {
          if (message.type === "tool_approval_request") {
            return status === "waiting_approval" ? (
              <ToolApproval
                key={index}
                message={message}
                onApprove={onApprove}
                onDeny={onDeny}
              />
            ) : null;
          }
          if (message.type === "question_request") {
            const pending = pendingQuestions.find(
              (question) => question.question_id === message.tool_use_id
            );
            if (pending) {
              return <QuestionPrompt key={index} question={pending} onSubmit={onAnswer} />;
            }
          }
          return (
            <MessageBubble
              key={index}
              message={message}
              sessionId={SPECIMEN_SESSION_ID}
              agentName="Archivist"
              agentId="specimen-archivist"
            />
          );
        })}
        {status === "running" && messages.length > 0 && (
          <div className="typing-pulse" aria-label="助手正在生成"><span /><span /><span /></div>
        )}
      </div>

      {parkedResumeAt !== null && (
        <div className="specimen-limit-banner">
          <IconAlertTriangle />
          <div><strong>任务已安全停放</strong><span>{remainingLabel(parkedResumeAt, now)}</span></div>
          <span className="auto-resume">AUTO RESUME</span>
        </div>
      )}
    </section>
  );
}

function EventAnatomy({
  step,
  fallbackEvent,
}: {
  step: InspectorStep | undefined;
  fallbackEvent: SpecimenEvent;
}) {
  const event = step?.event ?? fallbackEvent;
  const principle = EVENT_PRINCIPLES[event.type] ?? EVENT_PRINCIPLES.status;
  const snapshotRows: Array<{
    key: keyof StoreSnapshot;
    label: string;
  }> = [
    { key: "status", label: "session.status" },
    { key: "messages", label: "messages.length" },
    { key: "questions", label: "pendingQuestions" },
    { key: "queued", label: "pendingQueue" },
    { key: "parked", label: "parkedTurn" },
    { key: "lastSeq", label: "lastAppliedSeq" },
  ];
  const printValue = (value: StoreSnapshot[keyof StoreSnapshot]) => {
    if (value === null) return "—";
    if (typeof value === "boolean") return value ? "true" : "false";
    return String(value);
  };

  return (
    <section className="event-anatomy" aria-label="选中事件的完整解剖">
      <div className="event-anatomy-heading">
        <div>
          <span className="eyebrow">EVENT AUTOPSY</span>
          <h2>一条事件，穿过四层边界</h2>
        </div>
        <div className="selected-event-chip">
          <span>{step ? `EVENT ${String(step.index + 1).padStart(2, "0")}` : "SCRIPT PREVIEW"}</span>
          <strong>{event.type}</strong>
          {step && (
            <small data-outcome={step.outcome.status}>
              {step.outcome.status === "dropped" ? "被去重守卫拦截" : "已进入 Store"}
            </small>
          )}
        </div>
      </div>

      <div className="autopsy-grid">
        <article className="autopsy-contract">
          <span className="autopsy-number">01 / PROTOCOL</span>
          <h3>协议：谁产生它</h3>
          <dl>
            <div><dt>事件</dt><dd>{principle.title}</dd></div>
            <div><dt>生产者</dt><dd>{principle.producer}</dd></div>
            <div><dt>寿命</dt><dd>{principle.durability}</dd></div>
            <div><dt>顺序</dt><dd>{principle.sequence}</dd></div>
          </dl>
          <pre>{JSON.stringify(event, null, 2)}</pre>
        </article>

        <article className="autopsy-reducer">
          <span className="autopsy-number">02 / REDUCER</span>
          <h3>处理：哪段代码接住它</h3>
          <pre><code>{principle.reducer}</code></pre>
          <p>
            所有现场事件和正式 WebSocket 都调用同一个
            <code> applyWsEvent </code>，这里不是另一套演示逻辑。
          </p>
        </article>

        <article className="autopsy-store">
          <span className="autopsy-number">03 / STATE</span>
          <h3>状态：前后究竟变了什么</h3>
          {step ? (
            <div className="snapshot-table">
              <div className="snapshot-head"><span>字段</span><span>BEFORE</span><span>AFTER</span></div>
              {snapshotRows.map(({ key, label }) => {
                const changed = step.before[key] !== step.after[key];
                return (
                  <div className="snapshot-row" data-changed={changed} key={key}>
                    <span>{label}</span>
                    <code>{printValue(step.before[key])}</code>
                    <code>{printValue(step.after[key])}</code>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="snapshot-placeholder">
              <IconStepInto />
              <p>执行一个事件后，这里会逐字段比较 Store 的真实前后快照。</p>
            </div>
          )}
          <div className="store-target"><span>写入目标</span><strong>{principle.store}</strong></div>
        </article>

        <article className="autopsy-render">
          <span className="autopsy-number">04 / RENDER</span>
          <h3>界面：谁会因此重绘</h3>
          <div className="render-target">
            <span className="render-pulse" />
            <div><strong>{principle.renderer}</strong><small>React subscriber</small></div>
          </div>
          <h4>为什么这样设计</h4>
          <p>{principle.why}</p>
          <h4>省掉这层会怎样</h4>
          <p className="risk-copy">{principle.risk}</p>
        </article>
      </div>
    </section>
  );
}

const STREAMING_TOC = [
  { id: "principle-001-1", title: "事件契约与持久身份", sections: [["stream-event-families", "事件家族"], ["stream-persisted-vs-live", "历史与当前状态"], ["stream-seq-identity", "序号身份"], ["stream-contract-source", "源码阅读"]] },
  { id: "principle-001-2", title: "消息从进程到界面的完整路径", sections: [["stream-server-order", "服务端顺序"], ["stream-client-order", "客户端顺序"], ["stream-reconnect", "重连恢复"], ["stream-sequence-source", "源码阅读"]] },
  { id: "principle-001-3", title: "控制状态、审批与追问", sections: [["stream-control-plane", "控制面"], ["stream-approval", "审批闸门"], ["stream-question", "追问状态"], ["stream-parked", "限额停放"], ["stream-state-source", "源码阅读"]] },
  { id: "principle-001-4", title: "重复、失败与恢复出口", sections: [["stream-duplicate", "重复广播"], ["stream-limit-failure", "限额恢复"], ["stream-tool-failure", "工具拒绝"], ["stream-question-failure", "信息不足"], ["stream-failure-source", "源码阅读"]] },
  { id: "principle-001-5", title: "取舍、规模与演进边界", sections: [["stream-decisions", "当前取舍"], ["stream-scale", "规模边界"], ["stream-evolution", "协议演进"], ["stream-tradeoff-source", "源码阅读"]] },
] as const;

function StreamingArticleToc() {
  const [open, setOpen] = useState(false);
  const [activeChapter, setActiveChapter] = useState("principle-001-1");
  const [activeSection, setActiveSection] = useState("stream-event-families");
  useEffect(() => {
    const media = window.matchMedia("(min-width: 1101px)");
    const sync = () => setOpen(media.matches);
    sync(); media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);
  useEffect(() => {
    const targets = Array.from(document.querySelectorAll<HTMLElement>(".streaming-article-chapter, .streaming-article-chapter .article-section"));
    const update = () => {
      let current: HTMLElement | undefined;
      for (const target of targets) if (target.getBoundingClientRect().top <= 150) current = target;
      if (!current) return;
      if (current.classList.contains("article-section")) {
        setActiveSection(current.id);
        setActiveChapter(current.closest<HTMLElement>(".streaming-article-chapter")?.id ?? "principle-001-1");
      } else setActiveChapter(current.id);
    };
    update(); window.addEventListener("scroll", update, { passive: true }); window.addEventListener("hashchange", update);
    return () => { window.removeEventListener("scroll", update); window.removeEventListener("hashchange", update); };
  }, []);
  const follow = () => { if (!window.matchMedia("(min-width: 1101px)").matches) setOpen(false); };
  return <aside className="streaming-local-toc" data-open={open}>
    <button type="button" aria-expanded={open} onClick={() => setOpen((value) => !value)}><span>本页目录</span><span>{open ? "收起" : "展开"}</span></button>
    <nav aria-label="流式响应文章目录">
      <a className="toc-trace-link" href="#stream-complete-trace" onClick={follow}>一次消息的完整时序</a>
      {STREAMING_TOC.map((chapter, index) => <div className="streaming-toc-chapter" data-active={activeChapter === chapter.id} key={chapter.id}>
        <a className="streaming-toc-chapter-link" aria-current={activeChapter === chapter.id ? "location" : undefined} href={`#${chapter.id}`} onClick={follow}><span>{String(index + 1).padStart(2, "0")}</span>{chapter.title}</a>
        <div>{chapter.sections.map(([id, title]) => <a data-current={activeSection === id} href={`#${id}`} onClick={follow} key={id}>{title}</a>)}</div>
      </div>)}
    </nav>
  </aside>;
}

function ArticleSource({ id, files, question }: { id: string; files: string[]; question: string }) {
  return <section className="article-section article-source-walk" id={id}><h4>源码阅读</h4><p>先沿事件生产者读到持久层，再沿广播入口读到 Store；只看 React 组件无法解释重连和去重</p><ol>{files.map((file) => <li key={file}><code>{file}</code></li>)}</ol><div className="article-exercises"><strong>读完应该能回答</strong><p>{question}</p></div></section>;
}

function PrinciplePanel({ onScenario }: { onScenario: (id: SpecimenScript["id"]) => void }) {
  const completeTrace = [
    ["S0", "Browser", "发送用户意图", "前端不先插入一条假消息，发送端和其他标签页等待同一服务端广播"],
    ["S1", "SessionManager", "持久化 user_message", "数据库分配单调递增 seq，消息身份从这里建立"],
    ["S2", "Harness", "启动 CLI 并读取 stream-json", "Claude 或 Codex 私有格式在解析层转换为 Owlery 中立事件"],
    ["S3", "SessionManager", "持久化每条历史事件", "assistant_text、tool_use、tool_result 与 result 先提交事实再广播"],
    ["S4", "WebSocket", "向会话订阅者广播", "载荷带 session_id；持久消息带 seq，瞬时控制事件不带"],
    ["S5", "applyWsEvent", "执行序号守卫与类型分派", "旧 seq 在写 Store 前丢弃，status 等当前状态则每次应用"],
    ["S6", "Zustand", "更新会话分片", "messages、pendingQuestions、parkedTurns 和 session status 分开保存"],
    ["S7", "React", "只重绘相关消费者", "MessageBubble、审批表单、追问表单和限额横幅读取各自状态"],
  ];
  return <section className="principle-panel streaming-principles" id="principle">
    <div className="principle-copy streaming-article-title"><span className="eyebrow">IMPLEMENTATION NOTES</span><h2>流式消息的事件协议与控制状态</h2></div>
    <div className="streaming-article-layout">
      <div className="streaming-article-body">
        <section className="streaming-execution-trace" id="stream-complete-trace"><h3>一次消息的完整时序</h3><ol aria-label="流式消息完整时序">{completeTrace.map(([id, actor, title, detail]) => <li key={id}><span>{id}</span><div><small>{actor}</small><strong>{title}</strong><p>{detail}</p></div></li>)}</ol></section>

        <section className="principle-chapter principle-article streaming-article-chapter" id="principle-001-1">
          <div className="chapter-heading"><span className="chapter-index">01</span><div><span className="eyebrow">EVENT CONTRACT</span><h3>事件契约与持久身份</h3></div></div>
          <div className="streaming-article-content">
            <section className="article-section" id="stream-event-families"><h4>事件家族决定保存和恢复方式</h4><div className="contract-table" role="table" aria-label="WebSocket 事件契约"><div className="contract-row contract-head" role="row"><span>家族</span><span>事件</span><span>持久化</span><span>SEQ</span><span>职责</span></div>{EVENT_CONTRACTS.map((row) => <div className="contract-row" role="row" key={row.family}><strong>{row.family}</strong><code>{row.events}</code><span>{row.persisted}</span><span>{row.seq}</span><p>{row.purpose}</p></div>)}</div></section>
            <section className="article-section" id="stream-persisted-vs-live"><h4>历史事实与当前状态不能共用恢复语义</h4><p>消息回答已经发生过什么，status、pendingQuestions 和 parkedTurns 回答现在还能做什么。历史事实必须能从数据库重放；当前状态允许被更新覆盖，却必须在刷新后由服务端重新水合</p><p>如果 running、idle 也按消息 seq 去重，重连时一个较旧但仍有效的控制事件可能被错误丢弃。反过来，持久文本没有 seq，重复广播就会在历史中制造第二份事实</p></section>
            <section className="article-section" id="stream-seq-identity"><h4>seq 是服务端日志位置，不是显示序号</h4><p>只有服务端同时掌握数据库提交与广播顺序，因此只有它能分配所有客户端一致的消息身份。Store 按 sessionId 维护 lastSeq；收到 seq 小于等于当前值的事件时，在 reducer 之前直接返回 dropped</p><p>多区域写入或分区历史会让单个整数序号不再够用，那时需要分区日志位置或全局事件 id，不能让浏览器时间戳替代因果顺序</p></section>
            <ArticleSource id="stream-contract-source" files={["server/session_manager.py", "web/src/hooks/useWebSocket.ts", "web/src/stores/sessionStore.ts"]} question="为什么 status 不带 seq，而 assistant_text 必须带 seq" />
          </div>
        </section>

        <section className="principle-chapter principle-article streaming-article-chapter" id="principle-001-2">
          <div className="chapter-heading"><span className="chapter-index">02</span><div><span className="eyebrow">END-TO-END SEQUENCE</span><h3>消息从进程到界面的完整路径</h3></div></div>
          <div className="streaming-article-content">
            <section className="article-section" id="stream-server-order"><h4>服务端顺序是 persist → seq → broadcast</h4><p>CLI 输出首先由对应 RuntimeProfile 的 parser 归一化，SessionManager 再把需要回放的事件写入消息表并获得 seq。只有提交成功的事实才进入 WebSocket 广播，因此客户端收到的序号能够在刷新后从数据库重新找到</p><p>若先广播再提交，数据库失败会留下只存在于某个浏览器内存里的幽灵消息；若每个标签页自己生成身份，重连合并时也无法判断两条文本是重复还是恰好内容相同</p></section>
            <section className="article-section" id="stream-client-order"><h4>客户端顺序是 guard → reduce → render</h4><table className="article-table"><thead><tr><th>阶段</th><th>输入</th><th>职责</th></tr></thead><tbody><tr><td>shouldApplyWsEvent</td><td>session_id、seq、type</td><td>拒绝已经应用的持久事件</td></tr><tr><td>applyWsEvent</td><td>中立事件载荷</td><td>按类型更新消息、状态或交互队列</td></tr><tr><td>Zustand</td><td>规范化 UI 状态</td><td>按 sessionId 隔离多个会话</td></tr><tr><td>React</td><td>selector 返回值</td><td>只重绘受影响组件</td></tr></tbody></table><p>演示页面直接调用生产用的 applyWsEvent，因此重复事件、审批和追问不是另一套动画状态机</p></section>
            <section className="article-section" id="stream-reconnect"><h4>重连先恢复快照，再接收增量</h4><p>页面重新加载时先从 REST 或初始会话载荷恢复消息与当前状态，并把已有最大 seq 写入 Store。随后 WebSocket 增量继续到达；服务器重发的重叠区间会被序号守卫丢弃</p><p>这要求快照和增量使用同一种消息身份。只按文本内容去重会误删用户连续发送的相同短句，也无法处理 tool_result 等结构化消息</p></section>
            <ArticleSource id="stream-sequence-source" files={["server/harness/run.py", "server/session_manager.py", "web/src/hooks/useWebSocket.ts"]} question="如果数据库提交成功但广播失败，刷新后为什么仍能恢复消息" />
          </div>
        </section>

        <section className="principle-chapter principle-article streaming-article-chapter" id="principle-001-3">
          <div className="chapter-heading"><span className="chapter-index">03</span><div><span className="eyebrow">CONTROL STATE</span><h3>控制状态、审批与追问</h3></div></div>
          <div className="streaming-article-content">
            <section className="article-section" id="stream-control-plane"><h4>控制权独立于消息历史</h4><table className="article-table"><thead><tr><th>状态</th><th>当前控制者</th><th>允许的下一步</th></tr></thead><tbody><tr><td>idle</td><td>用户</td><td>发送新 Turn</td></tr><tr><td>running</td><td>Harness</td><td>继续接收文本与工具事件</td></tr><tr><td>waiting_approval</td><td>用户审批</td><td>Allow 或 Deny</td></tr><tr><td>parked</td><td>ParkedTurnRunner</td><td>等待 reset_at 后恢复</td></tr></tbody></table><p>最后一段 assistant_text 到达不代表控制权已经释放，result 负责本轮结算，idle 才允许输入区进入下一轮</p></section>
            <section className="article-section" id="stream-approval"><h4>审批是执行闸门</h4><p>tool_approval_request 同时创建可交互消息并把会话置为 waiting_approval。允许后恢复原运行链路；拒绝则产生带 is_error 的 tool_result，让模型和用户都能看见工具没有执行</p><p>只有弹窗而没有服务端状态约束，后台进程仍可能继续；只有状态而没有持久工具意图，事后又无法审计模型请求过什么</p></section>
            <section className="article-section" id="stream-question"><h4>问题历史与待回答表单分开保存</h4><p>question_request 的内容属于历史，pendingQuestions 表示仍可提交的交互状态。question_answer 一次性追加回答消息并按 question_id 移除待回答项，避免答案已经出现但表单仍可重复提交</p></section>
            <section className="article-section" id="stream-parked"><h4>限额停放保存原 Turn</h4><p>limit_paused 不是终局失败。服务端保存 Prompt、恢复时间和限额类型，ParkedTurnRunner 到点后重新取得执行权。浏览器倒计时只负责展示，不能自行断言任务已经恢复</p></section>
            <ArticleSource id="stream-state-source" files={["web/src/stores/sessionStore.ts", "server/parked_turns.py", "server/session_manager.py"]} question="为什么审批、追问和限额不能只表示为聊天文本" />
          </div>
        </section>

        <section className="principle-chapter principle-article streaming-article-chapter" id="principle-001-4">
          <div className="chapter-heading"><span className="chapter-index">04</span><div><span className="eyebrow">FAILURE ROUTES</span><h3>重复、失败与恢复出口</h3></div></div>
          <div className="streaming-article-content streaming-failure-sections">
            <section className="article-section" id="stream-duplicate"><h4>重复广播</h4><p>相同 seq 第二次到达时，Store 数量必须保持不变。点击实验可观察 Inspector 将事件标记为 dropped，而不是先写入再删除</p><p>去重发生在任何 reducer 之前，因此重复 tool_result 也不会再次触发组件状态变化。守卫只能拒绝已经见过的身份，不能自动修复中间缺失的 seq；发现序号跳跃时应重新拉取会话快照，而不是伪造缺失消息</p><button type="button" onClick={() => onScenario("recovery")}>重放重复 seq</button></section>
            <section className="article-section" id="stream-limit-failure"><h4>半轮命中套餐限额</h4><p>已经完成的工具结果不能因 429 被重放。原 Turn 进入 parkedTurns，reset_at 到达后沿同一上下文继续</p><p>停放记录由服务端持有，浏览器关闭不会取消恢复。恢复前输入区不得把任务显示为普通 idle；恢复失败也必须保留原 Prompt 和原因，不能悄悄退化为让用户重新发送整轮</p><button type="button" onClick={() => onScenario("recovery")}>重放限额停放</button></section>
            <section className="article-section" id="stream-tool-failure"><h4>危险工具被拒绝</h4><p>Deny 不是丢弃事件，而是形成明确失败的 tool_result 并关闭审批状态，让后续模型知道不能假设副作用已经发生</p><p>拒绝结果沿原 tool_use_id 返回，保证意图与决定能够配对。系统不能把拒绝翻译成空字符串或成功结果，否则模型可能继续基于不存在的文件、命令输出或外部写入推理</p><button type="button" onClick={() => onScenario("tool")}>重放工具审批</button></section>
            <section className="article-section" id="stream-question-failure"><h4>输入信息不足</h4><p>模型通过 question_request 把决定权返回用户；表单答案带 question_id 回到同一链路，避免在服务端凭自由文本猜测对应问题</p><p>问题内容作为历史保留，待回答状态则在提交后删除。刷新时二者必须分别恢复，否则会出现看得见问题却无法回答，或已经回答仍可再次提交的半完成状态</p><button type="button" onClick={() => onScenario("question")}>重放用户追问</button></section>
            <ArticleSource id="stream-failure-source" files={["web/src/hooks/useWebSocket.ts", "server/session_manager.py", "server/parked_turns.py"]} question="重复、拒绝和暂停为何需要三个不同结果" />
          </div>
        </section>

        <section className="principle-chapter principle-article streaming-article-chapter" id="principle-001-5">
          <div className="chapter-heading"><span className="chapter-index">05</span><div><span className="eyebrow">TRADE-OFFS</span><h3>取舍、规模与演进边界</h3></div></div>
          <div className="streaming-article-content">
            <section className="article-section" id="stream-decisions"><h4>当前实现的四项取舍</h4><table className="article-table"><thead><tr><th>选择</th><th>原因</th><th>代价</th></tr></thead><tbody>{DESIGN_DECISIONS.map((item) => <tr key={item.decision}><td>{item.decision}</td><td>{item.why}</td><td>{item.cost}</td></tr>)}</tbody></table></section>
            <section className="article-section" id="stream-scale"><h4>规模边界</h4>{DESIGN_DECISIONS.map((item) => <p key={item.decision}><strong>{item.decision}：</strong>{item.boundary}</p>)}<p>消息历史继续增长后，前端还需要分页和虚拟化；这与事件身份协议是两层问题，不能用删除 seq 换取渲染速度</p></section>
            <section className="article-section" id="stream-evolution"><h4>协议演进必须保持向后可判定</h4><p>新增事件类型时，需要同时定义生产者、是否持久化、身份字段、Store 写入目标和渲染消费者。旧客户端无法识别的新控制事件应安全忽略；改变既有事件语义则需要版本字段或新事件名</p><p>观测指标至少应覆盖广播失败、重复事件丢弃、序号缺口、待回答表单数量和 parked Turn 恢复延迟，否则协议出错只会表现为“偶尔卡住”</p></section>
            <ArticleSource id="stream-tradeoff-source" files={["web/src/hooks/useWebSocket.ts", "web/src/stores/sessionStore.ts", "server/routers/ws.py"]} question="当系统升级到多区域写入时，当前单调 seq 需要如何演进" />
          </div>
        </section>
      </div>
      <StreamingArticleToc />
    </div>
  </section>;
}

export function StreamingAnatomy() {
  const scripts = useMemo(() => makeScripts(), []);
  const [activeId, setActiveId] = useState<SpecimenScript["id"]>("happy");
  const activeScript = scripts.find((script) => script.id === activeId) ?? scripts[0];
  const engineRef = useRef(new ReplayEngine(activeScript.events));
  const nextHistoryOrderRef = useRef(0);
  const [history, setHistory] = useState<InspectorStep[]>([]);
  const [selectedOrder, setSelectedOrder] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<Speed>(1);
  const [currentIndex, setCurrentIndex] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());

  const messages = useSessionStore(
    (state) => state.messages[SPECIMEN_SESSION_ID] ?? EMPTY_MESSAGES
  );
  const sessionStatus = useSessionStore(
    (state) =>
      state.sessions.find((session) => session.id === SPECIMEN_SESSION_ID)?.status ?? "idle"
  );
  const parkedResumeAt = useSessionStore(
    (state) => state.parkedTurns[SPECIMEN_SESSION_ID]?.resumeAt ?? null
  );

  const reset = useCallback((script: SpecimenScript) => {
    setPlaying(false);
    resetSpecimenStore();
    engineRef.current.reset(script.events);
    nextHistoryOrderRef.current = 0;
    setHistory([]);
    setSelectedOrder(null);
    setCurrentIndex(null);
  }, []);

  useEffect(() => {
    reset(activeScript);
  }, [activeScript, reset]);

  useEffect(() => {
    if (parkedResumeAt === null) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [parkedResumeAt]);

  const record = useCallback((step: ReplayStep, before: StoreSnapshot) => {
    const entry: InspectorStep = { ...step, before, after: readSnapshot() };
    setHistory((items) => [...items, entry]);
    setSelectedOrder(nextHistoryOrderRef.current);
    nextHistoryOrderRef.current += 1;
    setCurrentIndex(step.index);
  }, []);

  const runStep = useCallback((override?: SpecimenEvent) => {
    const before = readSnapshot();
    const step = engineRef.current.step(override);
    if (!step) {
      setPlaying(false);
      return;
    }
    record(step, before);
    if (engineRef.current.done) setPlaying(false);
  }, [record]);

  useEffect(() => {
    if (!playing) return;
    const timer = window.setTimeout(() => runStep(), 900 / speed);
    return () => window.clearTimeout(timer);
  }, [playing, speed, history.length, runStep]);

  const approve = useCallback(() => {
    runStep();
    setPlaying(true);
  }, [runStep]);

  const deny = useCallback(() => {
    const before = readSnapshot();
    const seq = (before.lastSeq ?? 0) + 1;
    const event: SpecimenEvent = {
      type: "tool_result",
      session_id: SPECIMEN_SESSION_ID,
      seq,
      output: "用户拒绝了这次工具调用。",
      tool_use_id: "tool-tsc-1",
      is_error: true,
    };
    const outcome: WsEventApplication = applyWsEvent(event);
    record({ index: engineRef.current.position, event, outcome }, before);
    applyWsEvent({ type: "status", session_id: SPECIMEN_SESSION_ID, status: "idle" });
    engineRef.current.reset([]);
    setPlaying(false);
  }, [record]);

  const answer = useCallback((questionId: string, answers: AnswerPayload[]) => {
    const selected = answers.flatMap((item) => item.selected ?? []).join("、");
    const typed = answers.map((item) => item.text?.trim()).filter(Boolean).join("、");
    const event: SpecimenEvent = {
      type: "question_answer",
      session_id: SPECIMEN_SESSION_ID,
      seq: engineRef.current.peek()?.seq,
      question_id: questionId,
      content: typed || selected || "已回答",
    };
    runStep(event);
    setPlaying(true);
  }, [runStep]);

  const progress = engineRef.current.length
    ? Math.round((engineRef.current.position / engineRef.current.length) * 100)
    : 100;
  const selectedStep =
    selectedOrder === null ? history.at(-1) : history[selectedOrder];

  const openScenario = useCallback((id: SpecimenScript["id"]) => {
    setActiveId(id);
    window.requestAnimationFrame(() => {
      document.querySelector(".anatomy-workbench")?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    });
  }, []);

  return (
    <main className="anatomy-page">
      <header className="anatomy-nav">
        <a className="anatomy-brand" href="/function-cabinet.html" aria-label="Owlery 功能标本馆首页">
          <span className="brand-mark"><OwleryLogo size={22} /></span>
          <span><strong>Owlery</strong><small>FUNCTION CABINET</small></span>
        </a>
        <div className="nav-center"><span>标本 001</span><strong>流式 AI 对话解剖</strong></div>
        <div className="cabinet-nav-links">
          <a className="nav-principle" href="#principle">查看原理</a>
          <a className="nav-principle nav-next" href="/agent-delegation.html">下一件 002 <IconArrowRight /></a>
        </div>
      </header>

      <section className="anatomy-hero" id="top">
        <div className="hero-index">SPECIMEN / 001</div>
        <div className="hero-copy">
          <span className="eyebrow"><span className="eyebrow-line" /> INTERACTIVE SYSTEM STUDY</span>
          <h1>流式消息的<br />事件处理链路</h1>
        </div>
        <div className="hero-proof">
          <div><strong>4</strong><span>种真实场景</span></div>
          <div><strong>1×</strong><span>生产事件处理器</span></div>
          <div><strong>0</strong><span>张假截图</span></div>
        </div>
      </section>

      <nav className="scenario-tabs" aria-label="选择演示场景">
        {scripts.map((script) => (
          <button
            type="button"
            className="scenario-tab"
            data-active={script.id === activeId}
            key={script.id}
            onClick={() => setActiveId(script.id)}
          >
            <span>{script.index}</span>
            <div><strong>{script.title}</strong><small>{script.description}</small></div>
          </button>
        ))}
      </nav>

      <section className="anatomy-workbench">
        <div className="demo-column">
          <div className="panel-heading demo-heading">
            <div><span className="eyebrow">OPERABLE SPECIMEN</span><h2>{activeScript.title}</h2></div>
            <p>{activeScript.lesson}</p>
          </div>
          <ChatSpecimen
            status={sessionStatus}
            messages={messages}
            parkedResumeAt={parkedResumeAt}
            now={now}
            onApprove={approve}
            onDeny={deny}
            onAnswer={answer}
          />
          <div className="replay-controls">
            <Button
              size="icon"
              aria-label={playing ? "暂停" : "播放"}
              onClick={() => {
                if (engineRef.current.done) reset(activeScript);
                setPlaying((value) => !value);
              }}
            >
              {playing ? <IconPlayerPause /> : <IconPlayerPlay />}
            </Button>
            <Button variant="outline" size="icon" aria-label="单步执行" onClick={() => runStep()} disabled={playing || engineRef.current.done}>
              <IconStepInto />
            </Button>
            <Button variant="ghost" size="icon" aria-label="重置演示" onClick={() => reset(activeScript)}>
              <IconRefresh />
            </Button>
            <div className="replay-progress" aria-label={`播放进度 ${progress}%`}>
              <span style={{ width: `${progress}%` }} />
            </div>
            <div className="speed-control" aria-label="播放速度">
              <IconBrandSpeedtest />
              {SPEEDS.map((value) => (
                <button type="button" data-active={speed === value} key={value} onClick={() => setSpeed(value)}>{value}×</button>
              ))}
            </div>
          </div>
        </div>

        <EventInspector
          history={history}
          currentIndex={currentIndex}
          selectedOrder={selectedOrder}
          onSelect={setSelectedOrder}
        />
      </section>

      <EventAnatomy
        step={selectedStep}
        fallbackEvent={activeScript.events[0]}
      />

      <PrinciplePanel onScenario={openScenario} />

      <footer className="anatomy-footer">
        <div><OwleryLogo size={18} /><span>OWLERY FUNCTION CABINET</span></div>
        <p>展品必须能运行、能解释，也必须承认自己的边界。</p>
      </footer>
    </main>
  );
}
