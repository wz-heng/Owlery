import {
  IconArrowRight,
  IconBolt,
  IconBrandDatabricks,
  IconCheck,
  IconDatabase,
  IconHandStop,
  IconLoader2,
  IconPlayerPause,
  IconPlayerPlay,
  IconRefresh,
  IconRoute,
  IconStepInto,
  IconTerminal2,
} from "@tabler/icons-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { MessageBubble } from "../../components/MessageBubble";
import { OwleryLogo } from "../../components/OwleryLogo";
import { Button } from "../../components/ui/button";
import { useSessionStore, type BgTask, type Message } from "../../stores/sessionStore";
import { BgReplayEngine } from "./BgReplayEngine";
import { applyBgSpecimenEvent, resetBgSpecimenStore } from "./bgState";
import {
  BG_SCRIPTS,
  BG_SESSION_ID,
  type BgScenarioId,
  type BgScript,
  type BgSpecimenEvent,
} from "./scripts";
const EMPTY_MESSAGES: Message[] = [];
const EMPTY_TASKS: BgTask[] = [];

interface BgSnapshot {
  task: BgTask["status"] | "not_started";
  model: "alive" | "closed" | "followup";
  persisted: boolean;
  outputBytes: number;
  delivery: "waiting" | "queued" | "delivered";
}

interface BgLog {
  index: number;
  event: BgSpecimenEvent;
  before: BgSnapshot;
  after: BgSnapshot;
}

const EVENT_LABELS: Record<BgSpecimenEvent["type"], string> = {
  user_prompt: "用户提出长任务",
  tool_use: "模型调用 bg_run",
  bg_started: "Manager 接管进程",
  turn_closed: "原模型 turn 结束",
  worker_output: "Worker 产生输出",
  cancel_requested: "用户请求取消",
  watchdog_fired: "静默看门狗触发",
  bg_completed: "终态经 WS 广播",
  rest_hydrated: "按需读取完整输出",
  prompt_spilled: "大结果原子溢写",
  result_injected: "结果进入消息队列",
  followup_reply: "下一轮模型收口",
};

function readSnapshot(logs: BgLog[] = []): BgSnapshot {
  const store = useSessionStore.getState();
  const task = store.bgTasks[BG_SESSION_ID]?.[0];
  const types = logs.map((log) => log.event.type);
  const last = types.at(-1);
  const outputBytes = [...logs].reverse().find((log) => log.event.bytes)?.event.bytes ?? (task?.stdout.length || 0);
  return {
    task: task?.status ?? "not_started",
    model: last === "followup_reply" ? "followup" : types.includes("turn_closed") ? "closed" : "alive",
    persisted: types.includes("bg_started"),
    outputBytes,
    delivery: types.includes("followup_reply") ? "delivered" : types.includes("result_injected") ? "queued" : "waiting",
  };
}

function LifecycleTrace({ logs }: { logs: BgLog[] }) {
  const seen = new Set(logs.map((log) => log.event.type));
  const terminal = [...logs].reverse().find((log) => log.event.type === "bg_completed")?.event.status;
  const stages = [
    { key: "tool_use", label: "Turn #1", note: seen.has("turn_closed") ? "closed" : "model active", icon: IconTerminal2 },
    { key: "bg_started", label: "FastAPI owner", note: terminal ?? (seen.has("bg_started") ? "process held" : "waiting"), icon: IconBrandDatabricks },
    { key: "bg_started", label: "SQLite row", note: seen.has("bg_started") ? "durable" : "empty", icon: IconDatabase },
    { key: "bg_completed", label: "WS terminal", note: seen.has("bg_completed") ? "broadcast" : "waiting", icon: IconBolt },
    { key: "result_injected", label: "Message queue", note: seen.has("result_injected") ? "accepted" : "waiting", icon: IconRoute },
    { key: "followup_reply", label: "Turn #2", note: seen.has("followup_reply") ? "responded" : "not spawned", icon: IconTerminal2 },
  ];
  return (
    <div className="bg-lifecycle" aria-label="后台任务跨轮次链路">
      {stages.map((stage, index) => {
        const Icon = stage.icon;
        const active = seen.has(stage.key as BgSpecimenEvent["type"]);
        return (
          <div className="bg-stage-wrap" key={`${stage.key}-${stage.label}`}>
            {index > 0 && <div className="bg-stage-edge"><IconArrowRight /></div>}
            <div className="bg-stage" data-active={active} data-terminal={stage.label === "FastAPI owner" ? terminal : undefined}>
              <Icon /><div><strong>{stage.label}</strong><small>{stage.note}</small></div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function TaskInspector({ logs, selected, onSelect }: { logs: BgLog[]; selected: number | null; onSelect: (index: number) => void }) {
  const current = selected === null ? logs.at(-1) : logs[selected];
  const fields: Array<[keyof BgSnapshot, string]> = [
    ["task", "task.status"], ["model", "model process"], ["persisted", "DB row"], ["outputBytes", "captured bytes"], ["delivery", "result delivery"],
  ];
  return (
    <aside className="bg-inspector">
      <div className="panel-heading"><div><span className="eyebrow">LIFECYCLE INSPECTOR</span><h2>谁在持有任务？</h2></div><span className="live-indicator"><span />逐跳审计</span></div>
      <div className="bg-event-log">
        {logs.length === 0 ? <div className="inspector-empty"><IconRoute /><p>开始重放后，这里会记录所有权、持久化和回流状态。</p></div> : logs.map((log, index) => (
          <button type="button" key={`${log.index}-${log.event.type}`} data-selected={selected === index} onClick={() => onSelect(index)}>
            <span>{String(index + 1).padStart(2, "0")}</span><div><strong>{EVENT_LABELS[log.event.type]}</strong><small>{log.event.actor}</small></div><IconArrowRight />
          </button>
        ))}
      </div>
      <div className="bg-state-diff">
        <div className="bg-diff-head"><span>STATE</span><span>BEFORE</span><span>AFTER</span></div>
        {fields.map(([field, label]) => <div className="bg-diff-row" data-changed={!!current && current.before[field] !== current.after[field]} key={field}><span>{label}</span><code>{current ? String(current.before[field]) : "—"}</code><code>{current ? String(current.after[field]) : "—"}</code></div>)}
      </div>
      <pre className="bg-event-json">{current ? JSON.stringify(current.event, null, 2) : "// 选择事件查看载荷"}</pre>
    </aside>
  );
}

const BG_ARTICLE_TOC = [
  { id: "principle-003-1", index: "01", title: "后台任务从请求到回流", sections: [
    ["bg-trace", "一次完整执行"], ["bg-record", "身份与持久字段"], ["bg-start-order", "启动顺序"],
    ["bg-live-state", "运行期内存"], ["bg-restart", "服务重启"], ["bg-owner-source", "源码阅读顺序"],
  ] },
  { id: "principle-003-2", index: "02", title: "状态广播与完整输出读取", sections: [
    ["bg-two-planes", "两条读取路径"], ["bg-start-event", "启动事件"], ["bg-terminal-event", "终态事件"],
    ["bg-rest-output", "REST 读取输出"], ["bg-plane-failures", "失败边界"], ["bg-plane-source", "源码阅读顺序"],
  ] },
  { id: "principle-003-3", index: "03", title: "输出截断与 Prompt 溢写", sections: [
    ["bg-output-trace", "字节处理路径"], ["bg-concurrent-read", "并发读取"], ["bg-tail-cap", "保留尾部"],
    ["bg-thresholds", "两个阈值"], ["bg-pointer-prompt", "指针 Prompt"], ["bg-output-source", "源码阅读顺序"],
  ] },
  { id: "principle-003-4", index: "04", title: "终止语义、看门狗与进程组", sections: [
    ["bg-status-order", "状态判定顺序"], ["bg-cancel-trace", "主动取消"], ["bg-idle-watchdog", "静默看门狗"],
    ["bg-hard-timeout", "硬超时"], ["bg-process-group", "进程组清理"], ["bg-termination-source", "源码阅读顺序"],
  ] },
  { id: "principle-003-5", index: "05", title: "结果注入与消息队列", sections: [
    ["bg-delivery-trace", "回流路径"], ["bg-delivery-contract", "结果消息契约"], ["bg-busy-queue", "忙碌会话排队"],
    ["bg-delivery-failures", "交付失败"], ["bg-followup", "下一轮收口"], ["bg-delivery-source", "源码阅读顺序"],
  ] },
] as const;

function BgArticleToc() {
  const [open, setOpen] = useState(false);
  const [activeId, setActiveId] = useState<string>(BG_ARTICLE_TOC[0].id);

  useEffect(() => {
    const media = window.matchMedia("(min-width: 1101px)");
    const update = () => setOpen(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    const ids = BG_ARTICLE_TOC.flatMap((chapter) => [chapter.id, ...chapter.sections.map(([id]) => id)]);
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
    const schedule = () => { if (!frame) frame = window.requestAnimationFrame(update); };
    update();
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("hashchange", schedule);
    return () => {
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("hashchange", schedule);
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, []);

  const activeChapter = BG_ARTICLE_TOC.find((chapter) => chapter.id === activeId || chapter.sections.some(([id]) => id === activeId))?.id;
  const choose = (id: string) => {
    setActiveId(id);
    if (window.matchMedia("(max-width: 1100px)").matches) setOpen(false);
  };

  return (
    <aside className="bg-local-toc" aria-label="后台任务原理目录">
      <div className="bg-toc-head"><strong>本页目录</strong><button type="button" aria-expanded={open} aria-controls="bg-local-toc-list" onClick={() => setOpen((value) => !value)}>{open ? "收起" : "展开"}</button></div>
      <nav id="bg-local-toc-list" data-open={open} aria-label="后台任务原理章节">
        {BG_ARTICLE_TOC.map((chapter) => (
          <div className="bg-toc-group" data-active={activeChapter === chapter.id} key={chapter.id}>
            <a data-level="1" href={`#${chapter.id}`} aria-current={activeId === chapter.id ? "location" : undefined} onClick={() => choose(chapter.id)}><span>{chapter.index}</span>{chapter.title}</a>
            <div className="bg-toc-subnav">{chapter.sections.map(([id, title]) => <a data-level="2" href={`#${id}`} aria-current={activeId === id ? "location" : undefined} onClick={() => choose(id)} key={id}>{title}</a>)}</div>
          </div>
        ))}
      </nav>
    </aside>
  );
}

function BgPrinciples({ onScenario }: { onScenario: (id: BgScenarioId) => void }) {
  return (
    <section className="principle-panel bg-principles" id="principle">
      <div className="principle-copy"><span className="eyebrow">UNDER THE SURFACE</span><h2>进程所有权、持久状态与结果回流</h2></div>
      <BgArticleToc />

      <section className="principle-chapter principle-article bg-principle-article" id="principle-003-1">
        <div className="chapter-heading"><span className="chapter-index">01</span><div><span className="eyebrow">PROCESS OWNERSHIP</span><h3>后台任务从请求到回流</h3></div></div>
        <p className="principle-question">本章跟踪任务 <code>8e7f7e257ebb</code>：模型发起测试命令，当前 turn 结束，命令继续运行，最终结果进入下一轮</p>
        <div className="principle-depth bg-article">
          <section className="article-section" id="bg-trace">
            <h4>一次后台命令如何跨越两个 turn</h4>
            <pre className="article-code article-sequence"><code>{`Turn 1 / session s-42
  mcp__bg__run("bun run test", "运行测试")
    → POST /api/sessions/s-42/bg-tasks
    → spawn /bin/sh -c "bun run test"
    → INSERT bg_tasks(id=8e7f7e257ebb, status=running)
    → broadcast bg_started
    → return task_id immediately
  model tells user that the test is running
  Turn 1 closes

FastAPI process
  owns process group 8e7f7e257ebb
  reads stdout and stderr concurrently
  process exits 0
  → UPDATE bg_tasks(status=completed, stdout=..., exit_code=0)
  → broadcast bg_completed metadata
  → deliver_bg_result(session=s-42)

Turn 2 / same session
  receives [bg-task-result]
  summarizes result and continues the original task`}</code></pre>
            <ol className="bg-article-trace" aria-label="后台任务一次完整执行">
              <li><span>B0</span><div><strong>MCP 进程接收工具调用</strong><p><code>OWLERY_SESSION_ID</code> 决定任务归属，模型不能自行指定另一个会话</p></div></li>
              <li><span>B1</span><div><strong>HTTP 路由解析工作目录</strong><p>只有活跃 Session 可以启动任务，命令固定在该 Session 的 <code>working_dir</code> 中执行</p></div></li>
              <li><span>B2</span><div><strong>Manager 创建独立进程组</strong><p><code>start_new_session=True</code> 让后台 shell 脱离模型 CLI 的进程组</p></div></li>
              <li><span>B3</span><div><strong>数据库写入 running 行</strong><p>任务 id、命令、描述、目录与开始时间成为可查询历史</p></div></li>
              <li><span>B4</span><div><strong>任务身份立即返回</strong><p>模型拿到短 id 后结束当前 turn，不等待子进程退出</p></div></li>
              <li><span>B5</span><div><strong>常驻服务读取两个输出流</strong><p>stdout 与 stderr 各有独立缓冲区和 200 KB 上限</p></div></li>
              <li><span>B6</span><div><strong>进程退出后写入终态</strong><p>状态、退出码、输出、截断标记和完成时间一次更新到数据库</p></div></li>
              <li><span>B7</span><div><strong>先广播轻量终态</strong><p>页面上的任务卡可以立即翻转，但广播不携带大段输出</p></div></li>
              <li><span>B8</span><div><strong>再注入完整结果消息</strong><p><code>deliver_bg_result</code> 通过 <code>start_message</code> 创建同一会话的新 turn</p></div></li>
            </ol>
          </section>

          <section className="article-section" id="bg-record">
            <h4>任务身份与持久字段</h4>
            <table className="article-table"><thead><tr><th>字段</th><th>创建时</th><th>终态时</th><th>用途</th></tr></thead><tbody>
              <tr><td><code>id</code></td><td>12 位随机十六进制</td><td>不变</td><td>取消、读取详情和回流消息的共同身份</td></tr>
              <tr><td><code>session_id</code></td><td>由 MCP 环境绑定</td><td>不变</td><td>限制任务只能回到原会话</td></tr>
              <tr><td><code>command / working_dir</code></td><td>完整写入</td><td>不变</td><td>审计实际运行内容和路径</td></tr>
              <tr><td><code>status</code></td><td>running</td><td>四类终态之一</td><td>驱动任务卡和模型恢复策略</td></tr>
              <tr><td><code>stdout / stderr</code></td><td>空字符串</td><td>各自最多 200 KB</td><td>供详情读取和结果注入</td></tr>
              <tr><td><code>truncated</code></td><td>false</td><td>任一流超限即 true</td><td>提醒模型当前只看到尾部</td></tr>
            </tbody></table>
            <p>数据库记录保存可恢复事实，但不保存活的进程句柄；句柄只存在于当前 FastAPI 进程的 <code>_running</code> 字典</p>
          </section>

          <section className="article-section" id="bg-start-order">
            <h4>当前实现的启动顺序</h4>
            <pre className="article-code"><code>{`proc = await asyncio.create_subprocess_exec(...)
record = BgTaskRecord(status="running", ...)
await db.create_bg_task(...)
await broadcast({type: "bg_started", ...})
running[task_id] = _RunningTask(record, proc)
running[task_id].task = create_task(_run_task(...))
return record`}</code></pre>
            <p>这里有一个必须如实记录的窗口：subprocess 先创建，数据库行后写入。若 <code>create_bg_task</code> 在两者之间失败，当前代码没有显式终止刚生成的进程，它可能成为未登记进程</p>
            <aside className="article-callout"><strong>审计结论</strong><p>页面旧文案声称“先写行再启动”与代码不符。更稳妥的实现需要在 DB 写入失败时回收进程，或引入 pending 行与启动补偿事务</p></aside>
          </section>

          <section className="article-section" id="bg-live-state">
            <h4>为什么运行期仍需要内存状态</h4>
            <p><code>BgTaskRecord</code> 能落库，但取消任务需要真实的 <code>asyncio.subprocess.Process</code>、进程组 pid、两个字节缓冲区以及 orchestration task，这些对象无法序列化到 SQLite</p>
            <table className="article-table"><thead><tr><th>状态源</th><th>保存内容</th><th>进程重启后</th></tr></thead><tbody>
              <tr><td>SQLite <code>bg_tasks</code></td><td>命令、归属、终态和有界输出</td><td>仍可读取</td></tr>
              <tr><td><code>BgTaskManager._running</code></td><td>进程句柄、缓冲区、取消标记</td><td>全部丢失</td></tr>
              <tr><td>Session pending queue</td><td>等待进入模型的结果 Prompt</td><td>仅当前进程内有效</td></tr>
            </tbody></table>
          </section>

          <section className="article-section" id="bg-restart">
            <h4>服务重启后能恢复什么</h4>
            <p>启动阶段会把数据库里遗留的 running 或 pending 行统一改为 interrupted，使界面不会永久显示旋转状态。这不是进程恢复：旧进程句柄已经丢失，服务也不会重新执行命令</p>
            <p>恢复动作只修正持久状态，没有补发 <code>[bg-task-result]</code>。因此“任务历史不撒谎”和“终态一定触发新 turn”是两种不同保证，当前实现只在正常运行期提供后者</p>
          </section>

          <section className="article-section article-source-walk" id="bg-owner-source">
            <h4>沿源码验证这一章</h4>
            <ol><li>从 <code>server/mcp_servers/bg.py::bg_run</code> 看 Session 身份如何通过环境变量固定</li><li>阅读 <code>server/routers/bg_tasks.py::start_bg_task</code> 的活跃会话和工作目录边界</li><li>逐行跟踪 <code>BgTaskManager.start_task</code>，特别注意 spawn、DB、broadcast 和内存登记的顺序</li><li>最后看 <code>Database.mark_in_flight_bg_tasks_interrupted</code>，确认重启只修状态、不恢复进程</li></ol>
            <div className="article-exercises"><strong>读完应该能回答</strong><ul><li>为什么模型 CLI 退出不会杀死这条命令</li><li>为什么数据库行无法单独支持取消</li><li>spawn 成功但 DB 写入失败时会留下什么风险</li></ul></div>
          </section>
        </div>
      </section>

      <section className="principle-chapter principle-article bg-principle-article" id="principle-003-2">
        <div className="chapter-heading"><span className="chapter-index">02</span><div><span className="eyebrow">DELIVERY PLANES</span><h3>状态广播与完整输出读取</h3></div></div>
        <p className="principle-question">同一个任务终态同时服务于任务卡、详情面板和下一轮模型，三者不应强制消费相同大小的载荷</p>
        <div className="principle-depth bg-article">
          <section className="article-section" id="bg-two-planes"><h4>同一终态的两条读取路径</h4><table className="article-table"><thead><tr><th>路径</th><th>触发</th><th>载荷</th><th>消费者</th></tr></thead><tbody><tr><td>WebSocket</td><td>启动与终态立即推送</td><td>id、status、时间、退出码、截断标记</td><td>所有打开该会话的页面</td></tr><tr><td>REST</td><td>首屏加载或用户展开详情</td><td>命令、目录、stdout、stderr 和全部元数据</td><td>真正需要字节的单个客户端</td></tr></tbody></table><p>这种分流减少广播放大：一个 200 KB stdout 不会因为五个标签页在线而被发送五次</p><p>WebSocket 是低延迟通知，不是事实存储。客户端收到事件后只修改已有任务壳；刷新、断线重连或错过事件时，仍以 REST 从 SQLite 重建任务列表。因此事件丢失影响及时性，不改变任务的持久状态</p></section>
          <section className="article-section" id="bg-start-event"><h4><code>bg_started</code> 建立轻量任务壳</h4><pre className="article-code"><code>{`{
  type: "bg_started",
  session_id: "s-42",
  task_id: "8e7f7e257ebb",
  command: "bun run test",
  description: "运行测试",
  started_at: "..."
}`}</code></pre><p>前端可在数据库详情返回之前先创建 running chip。事件包含命令，便于即时展示，但没有工作目录和输出</p><p>该事件在数据库任务行写入后广播，所以前端随后发起详情请求时已有可读取记录。不过广播本身不参与事务；发送失败不会回滚任务，也不会阻止后台进程继续执行</p></section>
          <section className="article-section" id="bg-terminal-event"><h4><code>bg_completed</code> 只声明终态</h4><pre className="article-code"><code>{`{
  type: "bg_completed",
  session_id: "s-42",
  task_id: "8e7f7e257ebb",
  status: "completed",
  exit_code: 0,
  truncated: false,
  completed_at: "..."
}`}</code></pre><p>事件名固定为 <code>bg_completed</code>，真正状态可能是 completed、failed、cancelled 或 interrupted，消费者必须读取 <code>status</code> 而不是根据事件名猜成功</p></section>
          <section className="article-section" id="bg-rest-output"><h4>完整输出由任务详情接口返回</h4><p><code>GET /api/sessions/:sessionId/bg-tasks/:taskId</code> 先按 task id 查行，再校验 <code>rec.session_id === session_id</code>，避免知道短 id 的其他会话读取命令和输出</p><p>列表接口允许归档会话读取历史，启动接口却要求会话仍活跃，因为新任务必须有可用的工作目录和未来结果投递目标</p><p>任务列表适合恢复摘要状态，详情接口才承担大字段读取。这个边界使首屏代价与任务数量相关，而不是与全部历史日志字节数相关；只有展开某个任务时，客户端才为该任务的 stdout 和 stderr 付出传输与渲染成本</p></section>
          <section className="article-section" id="bg-plane-failures"><h4>两条路径可以独立失败</h4><table className="article-table"><thead><tr><th>失败</th><th>仍然成立的事实</th><th>用户影响</th></tr></thead><tbody><tr><td>WS broadcast 抛错</td><td>数据库终态已写入</td><td>当前页面可能不即时翻转，刷新后可恢复</td></tr><tr><td>REST 读取失败</td><td>轻量终态可能已显示</td><td>暂时看不到完整输出</td></tr><tr><td>输出行属于其他 Session</td><td>任务仍存在</td><td>接口返回 404，避免泄漏</td></tr><tr><td>浏览器离线</td><td>SQLite 保留历史</td><td>重连后通过列表重新水合</td></tr></tbody></table></section>
          <section className="article-section article-source-walk" id="bg-plane-source"><h4>沿源码验证传输平面</h4><ol><li>比较 <code>BgTaskManager.start_task</code> 和 <code>_run_task</code> 发出的两个 WS payload</li><li>阅读 <code>server/routers/bg_tasks.py</code> 的 list 与 get 路由，核对归档和归属校验</li><li>回到前端 store，确认终态事件先更新状态，详情请求再补 stdout 与 stderr</li></ol><div className="article-exercises"><strong>读完应该能回答</strong><ul><li>为什么 <code>bg_completed</code> 不携带 stdout</li><li>事件名为何不能代表成功状态</li><li>广播失败后刷新为何仍能恢复任务卡</li></ul></div></section>
        </div>
      </section>

      <section className="principle-chapter principle-article bg-principle-article" id="principle-003-3">
        <div className="chapter-heading"><span className="chapter-index">03</span><div><span className="eyebrow">BOUNDED OUTPUT</span><h3>输出截断与 Prompt 溢写</h3></div></div>
        <p className="principle-question">200 KB 限制数据库中保存多少字节，100 KB 决定下一轮 Prompt 是否改为文件指针，两者位于不同阶段</p>
        <div className="principle-depth bg-article">
          <section className="article-section" id="bg-output-trace"><h4>字节从 pipe 到下一轮模型的路径</h4><pre className="article-code article-sequence"><code>{`subprocess stdout/stderr pipes
  → two concurrent 8 KB readers
  → per-stream tail buffers, max 200 KB
  → UTF-8 decode(errors="replace")
  → SQLite stdout/stderr
  → render_delivery_prompt()
  → augmented user prompt
  → if prompt > 100 KB: atomic spill file
  → backend receives small pointer prompt
  → model reads spill file and responds`}</code></pre><p>截断发生在捕获阶段，溢写发生在模型 dispatch 阶段；前者会永久丢弃 head bytes，后者仍把完整 Prompt 保存在磁盘文件中</p></section>
          <section className="article-section" id="bg-concurrent-read"><h4>stdout 与 stderr 必须并发排空</h4><p>Manager 为两个 pipe 各启动一个 reader，每次读取 8192 bytes。若只读 stdout 再读 stderr，子进程可能因为 stderr pipe 写满而阻塞，父进程又在等待 stdout EOF，形成经典管道死锁</p><p>每次读到任一流的字节都会刷新静默看门狗时钟，因此持续写 stderr 的命令不会被误判为无输出</p></section>
          <section className="article-section" id="bg-tail-cap"><h4>超过上限后保留尾部</h4><pre className="article-code"><code>{`buf.extend(chunk)
if len(buf) > 200 * 1024:
    del buf[:len(buf) - 200 * 1024]
    stream_truncated = True

final = TRUNCATION_MARKER + remaining_tail`}</code></pre><p>测试和构建的最终错误、失败摘要通常出现在尾部，因此实现丢弃最老字节。代价是开头的环境信息可能消失，模型必须看到截断标记，不能把尾部误当成完整日志</p></section>
          <section className="article-section" id="bg-thresholds"><h4>两个阈值保护不同边界</h4><table className="article-table"><thead><tr><th>阈值</th><th>检查对象</th><th>触发后</th><th>没有它的风险</th></tr></thead><tbody><tr><td>200 KB / stream</td><td>后台进程 stdout 与 stderr</td><td>丢 head、保 tail、设置 truncated</td><td>内存和 SQLite 行随日志无界增长</td></tr><tr><td>100 KB / Prompt</td><td>包含结果消息的 UTF-8 Prompt</td><td>原子写文件，dispatch 小型指针</td><td>单个 argv 超过 Linux <code>MAX_ARG_STRLEN</code>，CLI 无法启动</td></tr></tbody></table><p>两条流最多各 200 KB，所以合成结果远可能超过 100 KB；这正是二次溢写仍然必要的原因</p></section>
          <section className="article-section" id="bg-pointer-prompt"><h4>指针 Prompt 如何保留消息语义</h4><p>溢写使用同目录临时文件加 rename，避免模型读到半写文件。小 Prompt 保存绝对路径、原始字节数和“必须完整读取”的指令</p><pre className="article-code"><code>{`[bg-task-result] [owlery-large-prompt]
The actual user message is 120,421 bytes...
It's saved at /.../large-prompts/s-42/<uuid>.txt
Read that file in full, then respond to it`}</code></pre><p>开头的 <code>[bg-task-result]</code> 会被保留，前端仍能识别自动回流消息；spill 文件在硬删除 Session 时随会话目录清理</p></section>
          <section className="article-section article-source-walk" id="bg-output-source"><h4>沿源码验证输出边界</h4><ol><li>阅读 <code>server/bg_tasks.py::reader</code> 和 <code>_finalize_stream</code>，确认限制按 bytes 而不是字符</li><li>跟踪 <code>render_delivery_prompt</code> 如何加入命令、状态、截断说明和两个输出块</li><li>阅读 <code>server/large_prompts.py::spill_if_large</code> 的阈值、原子 rename 和 marker 保留</li></ol><button type="button" className="principle-experiment-link" onClick={() => onScenario("spill")}>在上方重放 120 KB 溢写 →</button><div className="article-exercises"><strong>读完应该能回答</strong><ul><li>为何保留日志尾部而不是头部</li><li>为何 200 KB 捕获上限不能替代 100 KB Prompt 阈值</li><li>溢写后自动回流标记怎样继续生效</li></ul></div></section>
        </div>
      </section>

      <section className="principle-chapter principle-article bg-principle-article" id="principle-003-4">
        <div className="chapter-heading"><span className="chapter-index">04</span><div><span className="eyebrow">TERMINATION</span><h3>终止语义、看门狗与进程组</h3></div></div>
        <p className="principle-question">退出码相同不代表原因相同，状态判定还依赖取消标记、硬超时和 signal 方向</p>
        <div className="principle-depth bg-article">
          <section className="article-section" id="bg-status-order"><h4>终态按固定优先级判定</h4><pre className="article-code"><code>{`if cancel_requested:
    status = "cancelled"
elif timed_out:
    status = "failed"
elif exit_code == 0:
    status = "completed"
elif exit_code < 0:
    status = "interrupted"
else:
    status = "failed"`}</code></pre><table className="article-table"><thead><tr><th>状态</th><th>判定依据</th><th>调用者应如何理解</th></tr></thead><tbody><tr><td>completed</td><td>exit code 0</td><td>命令自然完成</td></tr><tr><td>cancelled</td><td>发 signal 前已设置 cancel_requested</td><td>用户或服务关闭主动要求停止</td></tr><tr><td>interrupted</td><td>没有取消/超时标记但被 signal 杀死</td><td>外部力量中断，命令本身不一定有错</td></tr><tr><td>failed</td><td>正退出码非零或硬超时</td><td>命令失败，或超过允许墙钟时间</td></tr></tbody></table></section>
          <section className="article-section" id="bg-cancel-trace"><h4>主动取消的完整路径</h4><ol className="bg-article-trace" aria-label="后台任务主动取消路径"><li><span>C0</span><div><strong>路由先校验任务归属</strong><p>其他 Session 即使猜中 task id 也只能得到 404</p></div></li><li><span>C1</span><div><strong>Manager 查找活句柄</strong><p>数据库显示 running 但内存无记录时返回 cancelled=false</p></div></li><li><span>C2</span><div><strong>先设置 cancel_requested</strong><p>signal 到达前记录原因，避免负退出码被误写为 interrupted</p></div></li><li><span>C3</span><div><strong>SIGTERM 整个进程组</strong><p>shell 及其子进程同时收到终止信号</p></div></li><li><span>C4</span><div><strong>正常终态管线继续执行</strong><p>保留部分输出、写 cancelled、广播并注入下一轮</p></div></li></ol></section>
          <section className="article-section" id="bg-idle-watchdog"><h4>静默看门狗只在见过首字节后启用</h4><p>看门狗每 5 秒检查一次。命令只有在已经产生过输出、随后 stdout 和 stderr 都静默超过 60 秒、且进程仍存活时才会被终止</p><p>这条前置条件允许 <code>sleep 300</code> 一类从未输出的任务继续运行，却能清理“测试已经打印完成摘要但 atexit 线程卡住”的进程。看门狗不设置 cancel_requested，最终负退出码映射为 interrupted</p></section>
          <section className="article-section" id="bg-hard-timeout"><h4>30 分钟硬超时与静默看门狗不同</h4><p>硬超时使用 <code>asyncio.wait_for(proc.wait(), timeout=1800)</code>，与命令是否输出无关。触发后设置 <code>timed_out=True</code>、清除取消标记并终止进程，最终状态是 failed</p><p>静默看门狗表达“进程疑似卡住”，硬超时表达“后台便利工具不允许成为常驻 daemon”，两者应向模型提供不同恢复建议</p></section>
          <section className="article-section" id="bg-process-group"><h4>为什么终止对象是进程组</h4><p>后台命令通过 <code>/bin/sh -c</code> 执行，shell 可能再启动测试 runner、编译器和浏览器。只终止 shell pid 会把真正耗资源的孙进程留在机器上</p><p><code>start_new_session=True</code> 建立独立进程组；取消先发送 SIGTERM，5 秒后仍未退出则 SIGKILL 同一 pgid。这个延迟任务不阻塞主终态等待</p></section>
          <section className="article-section article-source-walk" id="bg-termination-source"><h4>沿源码验证终止规则</h4><ol><li>从 <code>cancel_task</code> 看原因标记为何必须早于 signal</li><li>阅读 <code>idle_watchdog</code> 的 first-byte 条件和双流共享时钟</li><li>核对 <code>_run_task</code> finally 中状态分支的优先级</li><li>最后读 <code>_terminate_proc</code> 的 pgid、TERM 和五秒 KILL fallback</li></ol><div className="article-exercises"><strong>读完应该能回答</strong><ul><li>为什么同为 exit -15 可能得到 cancelled 或 interrupted</li><li>为什么从未输出的 sleep 不触发静默看门狗</li><li>为什么只杀 shell pid 不够</li></ul></div></section>
        </div>
      </section>

      <section className="principle-chapter principle-article bg-principle-article" id="principle-003-5">
        <div className="chapter-heading"><span className="chapter-index">05</span><div><span className="eyebrow">RESULT DELIVERY</span><h3>结果注入与消息队列</h3></div></div>
        <p className="principle-question">后台进程结束只完成了执行阶段，系统还要把终态转成模型能够消费的新用户消息</p>
        <div className="principle-depth bg-article">
          <section className="article-section" id="bg-delivery-trace"><h4>从数据库终态到下一轮回复</h4><pre className="article-code article-sequence"><code>{`_run_task finally
  → UPDATE bg_tasks terminal fields
  → remove task from _running
  → broadcast bg_completed
  → deliver_cb(record)
      → SessionManager.deliver_bg_result
      → render_delivery_prompt(record)
      → start_message(session_id, prompt)
          busy/parked → append pending queue
          idle        → create _drive_messages task
  → backend reads [bg-task-result]
  → assistant summarizes and continues`}</code></pre><p>顺序上先持久化、再广播、最后交付，因此即使后两步失败，任务执行事实仍然可以从数据库恢复</p></section>
          <section className="article-section" id="bg-delivery-contract"><h4>结果消息必须自包含</h4><p><code>render_delivery_prompt</code> 写入 task id、description、status、exit code、原命令、截断提示、stdout、stderr 和后续指令。模型不必依赖几十条消息之前的工具调用才能判断结果属于什么工作</p><pre className="article-code"><code>{`[bg-task-result] Background task \`8e7f...\` (运行测试)
finished with status \`completed\` (exit code 0)

Command:
  'bun run test'

stdout:
\`\`\`
86 passed
\`\`\`

Respond to the user with what you learned...`}</code></pre></section>
          <section className="article-section" id="bg-busy-queue"><h4>父会话忙碌时为什么不会并发改写</h4><p><code>start_message</code> 检查 usage-limit park 和 <code>session._active_task</code>。任一成立时，结果被包装成 <code>QueuedPrompt</code> 追加到 <code>_pending_queue</code>，并广播 queue_length</p><p>当前 turn 结束后，<code>_drive_messages</code> 按 FIFO 取出下一条并广播 dequeued。相同入口也处理真实用户消息，因此后台结果不会绕过会话锁或与正在生成的回复同时写 transcript</p></section>
          <section className="article-section" id="bg-delivery-failures"><h4>执行成功不等于交付成功</h4><table className="article-table"><thead><tr><th>位置</th><th>失败处理</th><th>是否重试</th></tr></thead><tbody><tr><td>目标 Session 已删除</td><td>记录日志并返回 false</td><td>否</td></tr><tr><td><code>start_message</code> 抛错</td><td>记录异常并返回 false</td><td>否</td></tr><tr><td>WS broadcast 失败</td><td>记录异常，继续 delivery</td><td>否</td></tr><tr><td>delivery callback 抛错</td><td>Manager 捕获并记录</td><td>否</td></tr></tbody></table><p><code>bg_tasks</code> 表目前没有 delivery_status 或 injected_at，因此系统能证明命令完成，却不能从数据库精确证明结果已经进入模型 turn</p><aside className="article-callout"><strong>设计边界</strong><p>若要提供可重试的至少一次交付，需要把执行终态与注入终态分开持久化，并为重复注入增加幂等键</p></aside></section>
          <section className="article-section" id="bg-followup"><h4>为什么完成后必须再启动一个模型 turn</h4><p>后台 worker 只知道进程状态和字节，不知道用户原始目标是否已经完成。测试通过后可能还要修改文档，构建失败后可能要修代码重跑，取消后则应解释已保留的部分输出</p><p>因此 worker 不直接生成最终答复，只把事实注入原会话。拥有完整对话上下文的 Agent 决定总结、继续下一步还是请求用户输入</p></section>
          <section className="article-section article-source-walk" id="bg-delivery-source"><h4>沿源码验证结果回流</h4><ol><li>阅读 <code>render_delivery_prompt</code>，列出模型收到的全部字段和截断分支</li><li>跟踪 <code>SessionManager.deliver_bg_result</code> 对缺失 Session 与异常的处理</li><li>阅读 <code>start_message</code> 的 busy、parked 和 lock 内二次检查</li><li>最后读 <code>_drive_messages</code> 的 FIFO drain，确认取消单个 turn 不会清空后续队列</li></ol><div className="article-exercises"><strong>读完应该能回答</strong><ul><li>为什么任务状态已 completed 仍可能没有下一轮答复</li><li>父会话忙碌时结果存在哪里</li><li>怎样把当前 best-effort 交付升级为可恢复的至少一次交付</li></ul></div></section>
        </div>
      </section>
    </section>
  );
}

export function BgTaskPipelineSpecimen() {
  const scripts = useMemo(() => BG_SCRIPTS, []);
  const [activeId, setActiveId] = useState<BgScenarioId>("success");
  const activeScript = scripts.find((script) => script.id === activeId) ?? scripts[0];
  const engineRef = useRef(new BgReplayEngine(activeScript.events));
  const nextLogIndexRef = useRef(0);
  const logsRef = useRef<BgLog[]>([]);
  const [logs, setLogs] = useState<BgLog[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const messages = useSessionStore((state) => state.messages[BG_SESSION_ID] ?? EMPTY_MESSAGES);
  const tasks = useSessionStore((state) => state.bgTasks[BG_SESSION_ID] ?? EMPTY_TASKS);

  const reset = useCallback((script: BgScript) => {
    setPlaying(false);
    resetBgSpecimenStore();
    engineRef.current.reset(script.events);
    nextLogIndexRef.current = 0;
    logsRef.current = [];
    setLogs([]);
    setSelected(null);
  }, []);

  useEffect(() => reset(activeScript), [activeScript, reset]);

  const step = useCallback(() => {
    const next = engineRef.current.step();
    if (!next) { setPlaying(false); return; }
    const before = readSnapshot(logsRef.current);
    applyBgSpecimenEvent(next.event);
    const draft = { ...next, before, after: readSnapshot([...logsRef.current, { ...next, before, after: before }]) };
    logsRef.current = [...logsRef.current, draft];
    setLogs(logsRef.current);
    setSelected(nextLogIndexRef.current++);
    if (engineRef.current.done) setPlaying(false);
  }, []);

  useEffect(() => {
    if (!playing) return;
    const timer = window.setTimeout(step, 820);
    return () => window.clearTimeout(timer);
  }, [playing, logs.length, step]);

  const openScenario = useCallback((id: BgScenarioId) => {
    setActiveId(id);
    window.requestAnimationFrame(() => document.querySelector(".bg-workbench")?.scrollIntoView({ behavior: "smooth", block: "start" }));
  }, []);

  const progress = engineRef.current.length ? Math.round(engineRef.current.position / engineRef.current.length * 100) : 100;
  const task = tasks[0];

  return (
    <main className="anatomy-page bg-page">
      <header className="anatomy-nav">
        <a className="anatomy-brand" href="/function-cabinet.html" aria-label="Owlery 功能标本馆首页"><span className="brand-mark"><OwleryLogo size={22} /></span><span><strong>Owlery</strong><small>FUNCTION CABINET</small></span></a>
        <div className="nav-center"><span>标本 003</span><strong>后台任务回流解剖</strong></div>
        <div className="cabinet-nav-links"><a className="nav-principle" href="#principle">查看原理</a><a className="nav-principle nav-prev" href="/agent-delegation.html">← 002</a><a className="nav-principle nav-next" href="/deep-research.html">下一件 004 <IconArrowRight /></a></div>
      </header>

      <section className="anatomy-hero bg-hero" id="top">
        <div className="hero-index">SPECIMEN / 003</div>
        <div className="hero-copy"><span className="eyebrow"><span className="eyebrow-line" /> CROSS-TURN PROCESS STUDY</span><h1>后台任务的<br />跨 Turn 生命周期</h1></div>
        <div className="hero-proof"><div><strong>2</strong><span>条结果传输平面</span></div><div><strong>100K</strong><span>安全溢写阈值</span></div><div><strong>1</strong><span>个持久进程 owner</span></div></div>
      </section>

      <nav className="scenario-tabs" aria-label="选择后台任务场景">
        {scripts.map((script) => <button type="button" className="scenario-tab" data-active={script.id === activeId} key={script.id} onClick={() => setActiveId(script.id)}><span>{script.index}</span><div><strong>{script.title}</strong><small>{script.description}</small></div></button>)}
      </nav>

      <section className="bg-workbench">
        <div className="bg-demo">
          <div className="panel-heading demo-heading"><div><span className="eyebrow">OPERABLE PROCESS TIMELINE</span><h2>{activeScript.title}</h2></div><p>{activeScript.lesson}</p></div>
          <LifecycleTrace logs={logs} />
          <div className="bg-runtime">
            <section className="bg-chat"><header><span>A</span><div><strong>Conversation transcript</strong><small>真实 MessageBubble + BgTaskChip</small></div>{task && <em data-status={task.status}>{task.status === "running" ? <IconLoader2 className="animate-spin" /> : task.status === "completed" ? <IconCheck /> : <IconHandStop />}{task.status}</em>}</header><div className="bg-transcript">{messages.length ? messages.map((message, index) => <MessageBubble key={`${message.type}-${index}`} message={message} sessionId={BG_SESSION_ID} agentName="Aberforth" agentId="agent-aberforth" />) : <div className="bg-empty"><IconTerminal2 /><strong>等待第一条事件</strong><span>播放或单步查看跨轮次过程。</span></div>}</div></section>
            <section className="worker-console"><header><span>WORKER PROCESS</span><strong>{task?.id ?? "not spawned"}</strong></header><div className="worker-meta"><span>owner</span><code>{logs.some((log) => log.event.type === "bg_started") ? "FastAPI / asyncio" : "—"}</code><span>model turn</span><code>{logs.some((log) => log.event.type === "turn_closed") ? "already closed" : "active"}</code><span>captured</span><code>{readSnapshot(logs).outputBytes.toLocaleString()} bytes</code></div><div className="worker-stream">{logs.filter((log) => ["worker_output", "watchdog_fired", "cancel_requested", "prompt_spilled"].includes(log.event.type)).map((log) => <p key={log.index} data-type={log.event.type}><span>{log.event.type}</span>{log.event.content}</p>)}{!logs.some((log) => log.event.type === "worker_output") && <p className="worker-wait">$ waiting for process output_</p>}</div></section>
          </div>
          <div className="replay-controls"><Button size="icon" aria-label={playing ? "暂停" : "播放"} onClick={() => { if (engineRef.current.done) reset(activeScript); setPlaying((value) => !value); }}>{playing ? <IconPlayerPause /> : <IconPlayerPlay />}</Button><Button variant="outline" size="icon" aria-label="单步执行" onClick={step} disabled={playing || engineRef.current.done}><IconStepInto /></Button><Button variant="ghost" size="icon" aria-label="重置演示" onClick={() => reset(activeScript)}><IconRefresh /></Button><div className="replay-progress"><span style={{ width: `${progress}%` }} /></div><span className="bg-progress-label">{engineRef.current.position} / {engineRef.current.length} events</span></div>
        </div>
        <TaskInspector logs={logs} selected={selected} onSelect={setSelected} />
      </section>

      <BgPrinciples onScenario={openScenario} />
      <footer className="anatomy-footer"><div><OwleryLogo size={18} /><span>OWLERY FUNCTION CABINET · 003</span></div><p>后台执行不是遗忘；每个终态都必须回到责任人手里。</p></footer>
    </main>
  );
}
