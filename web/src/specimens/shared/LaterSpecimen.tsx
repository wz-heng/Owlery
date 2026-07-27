import {
  IconArchive,
  IconArrowRight,
  IconBell,
  IconBook2,
  IconBrain,
  IconBrandOpenai,
  IconCheck,
  IconClock,
  IconCode,
  IconDatabase,
  IconFileText,
  IconGitBranch,
  IconPlayerPause,
  IconPlayerPlay,
  IconRefresh,
  IconRoute,
  IconSearch,
  IconShieldCheck,
  IconStepInto,
  IconTerminal2,
  IconTrash,
  IconWebhook,
  IconWorldSearch,
} from "@tabler/icons-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { ForkConfirmView, type ForkPreview } from "../../components/ForkDialog";
import { OwleryLogo } from "../../components/OwleryLogo";
import { ResearchCard } from "../../components/ResearchCard";
import { Button } from "../../components/ui/button";
import { applyWsEvent } from "../../hooks/useWebSocket";
import { useSessionStore } from "../../stores/sessionStore";
import { LATER_ARTICLE_META } from "./laterArticleMeta";
import { LaterReplayEngine, type ReplayFrame } from "./LaterReplayEngine";
import { LATER_SPECIMENS, type LaterSpecimenId, type LaterState } from "./laterSpecimens";
import { LATER_PRINCIPLE_DEPTH } from "./laterPrincipleDepth";

const RESEARCH_SESSION_ID = "specimen-research";

function ResearchVisual({ state, frames }: { state: LaterState; frames: ReplayFrame[] }) {
  const phases = ["scope", "search", "rank", "verify", "synthesize", "done"];
  const current = phases.indexOf(state.active ?? "");
  const leaves = Array.from({ length: Math.min(state.phase === "search" || state.active === "rank" ? Math.max(4, state.primary) : 4) });
  return (
    <div className="later-visual research-visual" aria-label="深度研究管线">
      <div className="research-phase-rail">
        {phases.map((phase, index) => <div key={phase} data-active={phase === state.active} data-past={current >= index}><span>{index + 1}</span><strong>{phase}</strong></div>)}
      </div>
      <div className="research-fanout">
        <div className="research-origin"><IconWorldSearch /><strong>Question</strong><small>bounded scope</small></div>
        <div className="fan-lines" aria-hidden="true"><i /><i /><i /><i /></div>
        <div className="research-leaves">
          {leaves.slice(0, 4).map((_, index) => <div key={index} data-live={state.active === "search"}><IconSearch /><span>angle {index + 1}</span><small>{state.active === "search" ? "searching" : current > 1 ? "evidence" : "waiting"}</small></div>)}
        </div>
        <IconArrowRight className="flow-arrow" />
        <div className="research-funnel" data-phase={state.active}><span>{state.primary}</span><small>candidates</small><i /><strong>{state.secondary}</strong><small>survivors</small></div>
        <IconArrowRight className="flow-arrow" />
        <div className="research-report" data-ready={state.status === "completed"}><IconFileText /><strong>cited report</strong><small>{state.status}</small></div>
      </div>
      <div className="real-component"><span>PRODUCTION COMPONENT</span><ResearchCard sessionId={RESEARCH_SESSION_ID} /></div>
      <p className="visual-caption">{frames.length ? state.detail : "等待事件进入"}</p>
    </div>
  );
}

function previewFor(state: LaterState, scenarioId: string): ForkPreview {
  const dirty = scenarioId === "dirty";
  return {
    rewind_to_msg_seq: state.primary || 12,
    prefilled_prompt: "重做登录页实现",
    side_effect_summary: {
      file_edits: state.phase === "select" ? [] : [{ path: "web/Login.tsx", turns: 2 }, { path: "web/login.css", turns: 1 }],
      bg_tasks: state.phase === "audit" || state.phase === "branch" ? [{ task_id: "bg-41", command: "bun run test", description: "tests", status: "completed" }] : [],
      other_tools: state.phase === "audit" ? [{ label: "webhook call", count: 1 }] : [],
      counts: { total: state.secondary, file_edits: state.phase === "select" ? 0 : 2, bg_tasks: state.phase === "audit" ? 1 : 0 },
    },
    revert: { available: !dirty && state.phase !== "select", refused_reason: dirty ? "Working tree has changes after the fork point" : null },
    can_fork: true,
  };
}

function ForkVisual({ state, scenarioId }: { state: LaterState; scenarioId: string }) {
  const [checked, setChecked] = useState(false);
  const [label, setLabel] = useState("Alternative login");
  const child = ["branch", "revert"].includes(state.phase) || state.active === "both";
  return (
    <div className="later-visual fork-visual" aria-label="会话分叉树">
      <div className="conversation-tree">
        <div className="tree-trunk"><span>#04</span><span>#08</span><span data-active={state.active === "parent"}>#{state.primary || 12}</span><span>#24</span></div>
        <div className="branch-junction" data-open={child}><i /><IconGitBranch /></div>
        <div className="tree-outcomes">
          <div data-active={state.active === "parent" || state.active === "both"}><strong>Parent</strong><small>{scenarioId === "rewind" && child ? "archived" : "preserved"}</small></div>
          <div data-active={child}><strong>Child</strong><small>{child ? "lineage persisted" : "not created"}</small></div>
        </div>
      </div>
      <div className="fork-production"><span>PRODUCTION CONFIRM VIEW</span><ForkConfirmView parentName="Owlery build" preview={previewFor(state, scenarioId)} revertChecked={checked} onRevertChange={setChecked} label={label} onLabelChange={setLabel} /></div>
    </div>
  );
}

function MemoryVisual({ state }: { state: LaterState }) {
  const exists = state.active !== "delete";
  return (
    <div className="later-visual memory-visual" aria-label="Agent 长期记忆目录">
      <div className="memory-files" data-exists={exists}>
        <header><IconBrain /><strong>agent-aberforth/memory/</strong></header>
        {exists ? <><div data-active={state.active === "index"}><IconBook2 /><span>MEMORY.md</span><small>1-line pointers</small></div><div data-active={state.active === "file" || state.active === "fact"}><IconFileText /><span>preferences.md</span><small>metadata.type: user</small></div><div><IconFileText /><span>owlery-project.md</span><small>metadata.type: project</small></div></> : <div className="memory-deleted"><IconTrash />identity directory removed</div>}
      </div>
      <div className="memory-index-link"><IconArrowRight /><code>[Preferences](preferences.md)</code><IconArrowRight /></div>
      <div className="memory-sessions">
        <div data-active={state.active === "session-a"}><IconTerminal2 /><strong>Claude session</strong><small>path override</small></div>
        <div data-active={state.active === "session-b"}><IconBrandOpenai /><strong>Codex session</strong><small>file instruction</small></div>
        <div data-active={state.active === "archive"}><IconArchive /><strong>Archived session</strong><small>memory retained</small></div>
      </div>
      <div className="memory-rule"><IconShieldCheck /><span>research leaf</span><code>memory_dir = None</code></div>
    </div>
  );
}

function HarnessVisual({ state }: { state: LaterState }) {
  const routes = [
    ["auth", "stop + reconnect"], ["limit", "park until reset"], ["transient", "bounded retry"], ["premature", "continue once"], ["watchdog", "terminal timeout"],
  ];
  return (
    <div className="later-visual harness-visual" aria-label="Harness 运行与故障路由">
      <div className="harness-pipeline">
        <div data-active={state.active === "context"}><IconDatabase /><strong>TurnContext</strong><small>neutral input</small></div><IconArrowRight />
        <div data-active={state.active === "profile"}><IconCode /><strong>RuntimeProfile</strong><small>argv + parser</small></div><IconArrowRight />
        <div data-active={state.active === "process"}><IconTerminal2 /><strong>subprocess</strong><small>Claude / Codex</small></div><IconArrowRight />
        <div data-active={state.active === "result"}><IconCheck /><strong>neutral events</strong><small>one reducer</small></div>
      </div>
      <div className="failure-router">
        <div className="router-core" data-active={state.active === "router"}><IconRoute /><strong>failure classifier</strong><small>specific before broad</small></div>
        <div className="router-routes">{routes.map(([key, result]) => <div key={key} data-active={state.active === key || (key === "premature" && state.status === "premature_exit")}><span>{key}</span><IconArrowRight /><strong>{result}</strong></div>)}</div>
      </div>
      <div className="recovery-meter" data-active={state.active === "retry"}><span>RECOVERY BUDGET</span><div><i data-filled={state.secondary >= 1} /><i /></div><strong>{state.secondary} / {state.active === "retry" ? "1" : "bounded"}</strong></div>
    </div>
  );
}

function AutomationVisual({ state }: { state: LaterState }) {
  return (
    <div className="later-visual automation-visual" aria-label="调度和通知生命周期">
      <div className="automation-track">
        <div data-active={state.active === "parser" || state.active === "model"}><IconCode /><strong>parse</strong><small>fast / structured</small></div><IconArrowRight />
        <div data-active={state.active === "schedule"}><IconClock /><strong>recurrence</strong><small>once · cron · interval</small></div><IconArrowRight />
        <div data-active={state.active === "clock"}><span className="clock-face"><i /></span><strong>trigger</strong><small>APScheduler</small></div><IconArrowRight />
        <div data-active={state.active === "session"}><IconTerminal2 /><strong>session route</strong><small>live origin / temporary</small></div><IconArrowRight />
        <div data-active={state.active === "archive"}><IconArchive /><strong>archive</strong><small>temporary session only</small></div>
      </div>
      <div className="notifier-fanout" data-active={state.active === "notify"}><div className="notify-source"><IconBell /><strong>session idle</strong></div><div className="notify-lines"><i /><i /><i /></div><div className="notify-targets"><div><IconWebhook /><strong>Webhook A</strong><small>delivered</small></div><div data-failed={state.status === "partial"}><IconWebhook /><strong>Webhook B</strong><small>{state.status === "partial" ? "isolated timeout" : "delivered"}</small></div><div><IconDatabase /><strong>Audit sink</strong><small>independent</small></div></div></div>
    </div>
  );
}

function Stage({ id, state, scenarioId, frames }: { id: LaterSpecimenId; state: LaterState; scenarioId: string; frames: ReplayFrame[] }) {
  if (id === "research") return <ResearchVisual state={state} frames={frames} />;
  if (id === "fork") return <ForkVisual state={state} scenarioId={scenarioId} />;
  if (id === "memory") return <MemoryVisual state={state} />;
  if (id === "harness") return <HarnessVisual state={state} />;
  return <AutomationVisual state={state} />;
}

function Inspector({ frames, selected, onSelect }: { frames: ReplayFrame[]; selected: number | null; onSelect: (index: number) => void }) {
  const current = (selected === null ? frames.at(-1) : frames[selected]) as (ReplayFrame & { state: LaterState }) | undefined;
  return <aside className="later-inspector"><div className="panel-heading"><div><span className="eyebrow">EVENT INSPECTOR</span><h2>事实，不是旁白</h2></div><span className="live-indicator"><span />逐步审计</span></div><div className="later-log">{frames.length === 0 ? <div className="inspector-empty"><IconRoute /><p>单步执行后，这里会留下每个边界事件。</p></div> : frames.map((item, index) => <button type="button" key={`${index}-${item.label}`} data-selected={selected === index} onClick={() => onSelect(index)}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{item.label}</strong><small>{item.actor}</small></div><IconArrowRight /></button>)}</div><div className="later-snapshot"><div><span>phase</span><code>{current?.state.phase ?? "—"}</code></div><div><span>status</span><code>{current?.state.status ?? "—"}</code></div><div><span>primary</span><code>{current?.state.primary ?? "—"}</code></div><div><span>secondary</span><code>{current?.state.secondary ?? "—"}</code></div></div><pre>{current ? JSON.stringify(current.event, null, 2) : "// 等待事件"}</pre></aside>;
}

const ARTICLE_SECTIONS = [
  ["boundary", "运行边界"],
  ["mechanism", "执行机制"],
  ["invariant", "状态与不变量"],
  ["failure", "失败出口"],
  ["tradeoff", "工程取舍"],
  ["source", "源码阅读"],
] as const;

function LaterArticleToc({ id }: { id: LaterSpecimenId }) {
  const config = LATER_SPECIMENS[id];
  const meta = LATER_ARTICLE_META[id];
  const [open, setOpen] = useState(false);
  const [activeChapter, setActiveChapter] = useState(`principle-${config.number}-1`);
  const [activeSection, setActiveSection] = useState(`principle-${config.number}-1-boundary`);

  useEffect(() => {
    const media = window.matchMedia("(min-width: 1101px)");
    const sync = () => setOpen(media.matches);
    sync();
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    const targets = Array.from(document.querySelectorAll<HTMLElement>(`.later-${id} .later-article-chapter, .later-${id} .later-article-chapter .article-section`));
    const update = () => {
      let current: HTMLElement | undefined;
      for (const target of targets) {
        if (target.getBoundingClientRect().top <= 150) current = target;
      }
      if (!current) return;
      if (current.classList.contains("article-section")) {
        setActiveSection(current.id);
        setActiveChapter(current.closest<HTMLElement>(".later-article-chapter")?.id ?? activeChapter);
      } else {
        setActiveChapter(current.id);
        setActiveSection(`${current.id}-boundary`);
      }
    };
    update();
    window.addEventListener("scroll", update, { passive: true });
    window.addEventListener("hashchange", update);
    return () => {
      window.removeEventListener("scroll", update);
      window.removeEventListener("hashchange", update);
    };
  }, [activeChapter, id]);

  const follow = () => {
    if (!window.matchMedia("(min-width: 1101px)").matches) setOpen(false);
  };

  return <aside className="later-local-toc" data-open={open}>
    <button type="button" className="later-toc-toggle" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
      <span>本页目录</span><span>{open ? "收起" : "展开"}</span>
    </button>
    <nav aria-label={`${config.title}文章目录`}>
      <a className="toc-trace-link" href={`#principle-${config.number}-trace`} onClick={follow}>{meta.traceTitle}</a>
      {config.principles.map((chapter, index) => {
        const chapterId = `principle-${config.number}-${index + 1}`;
        const active = activeChapter === chapterId;
        return <div className="later-toc-chapter" data-active={active} key={chapterId}>
          <a className="later-toc-chapter-link" aria-current={active ? "location" : undefined} href={`#${chapterId}`} onClick={follow}>
            <span>{String(index + 1).padStart(2, "0")}</span>{chapter.title}
          </a>
          <div className="later-toc-sections">
            {ARTICLE_SECTIONS.map(([slug, label]) => {
              const sectionId = `${chapterId}-${slug}`;
              return <a data-current={activeSection === sectionId} href={`#${sectionId}`} onClick={follow} key={sectionId}>{label}</a>;
            })}
          </div>
        </div>;
      })}
    </nav>
  </aside>;
}

function Principles({ id }: { id: LaterSpecimenId }) {
  const config = LATER_SPECIMENS[id];
  const meta = LATER_ARTICLE_META[id];
  const depth = LATER_PRINCIPLE_DEPTH[id];
  const chapterId = (index: number) => `principle-${config.number}-${index + 1}`;
  return <section className="principle-panel later-principles" id="principle">
    <div className="principle-copy later-article-title"><span className="eyebrow">IMPLEMENTATION NOTES</span><h2>{meta.title}</h2></div>
    <div className="later-article-layout">
      <div className="later-article-body">
        <section className="later-execution-trace" id={`principle-${config.number}-trace`}>
          <h3>{meta.traceTitle}</h3>
          <ol aria-label={meta.traceTitle}>{meta.trace.map((step) => <li key={step.id}><span>{step.id}</span><div><small>{step.actor}</small><strong>{step.title}</strong><p>{step.detail}</p></div></li>)}</ol>
        </section>
        {config.principles.map((chapter, index) => {
          const detail = depth[index];
          const idBase = chapterId(index);
          return <section className="principle-chapter principle-article later-article-chapter" id={idBase} key={chapter.kicker}>
            <div className="chapter-heading"><span className="chapter-index">{String(index + 1).padStart(2, "0")}</span><div><span className="eyebrow">{chapter.kicker}</span><h3>{chapter.title}</h3></div></div>
            <div className="principle-depth later-article-content">
              <section className="article-section" id={`${idBase}-boundary`}><h4>运行边界</h4><p>{chapter.body}</p><p>{detail.question}</p></section>
              <section className="article-section" id={`${idBase}-mechanism`}><h4>执行机制</h4><ol>{detail.mechanism.map((step) => <li key={step}>{step}</li>)}</ol></section>
              <section className="article-section" id={`${idBase}-invariant`}><h4>状态与不变量</h4><pre className="article-code"><code>{chapter.law}</code></pre><p>{chapter.consequence}</p><p>这条约束必须在写入、恢复和异常清理三条路径上同时成立；只在正常路径检查，重连或中途失败仍会制造相互矛盾的状态</p></section>
              <section className="article-section" id={`${idBase}-failure`}><h4>失败出口</h4><p>{detail.failure}</p><p>这里的终态用于决定恢复动作，不能为了界面统一而压成一个笼统的 failed；调用者需要知道是重试、等待、拒绝还是接受降级结果</p></section>
              <section className="article-section" id={`${idBase}-tradeoff`}><h4>工程取舍</h4><p>{detail.tradeoff}</p><p>边界值和策略属于产品契约。调整它们会同时改变成本、可恢复性和用户对结果完整度的判断，不能只作为性能参数修改</p></section>
              <section className="article-section article-source-walk" id={`${idBase}-source`}><h4>源码阅读</h4><p>从拥有状态的入口开始，沿调用链读到持久层和终态清理，避免只搜索界面文案</p><ol>{detail.code.map((item) => <li key={item}><code>{item}</code></li>)}</ol><div className="article-exercises"><strong>读完应该能回答</strong><p>{detail.question}</p></div></section>
            </div>
          </section>;
        })}
      </div>
      <LaterArticleToc id={id} />
    </div>
  </section>;
}

export function LaterSpecimen({ id }: { id: LaterSpecimenId }) {
  const config = LATER_SPECIMENS[id];
  const [scenarioId, setScenarioId] = useState(config.scenarios[0].id);
  const scenario = config.scenarios.find((item) => item.id === scenarioId) ?? config.scenarios[0];
  const engineRef = useRef(new LaterReplayEngine(scenario.frames));
  const [applied, setApplied] = useState<ReplayFrame[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const initial: LaterState = { phase: "waiting", status: "idle", primary: 0, secondary: 0, detail: "等待第一条事件", active: "" };
  const state = (applied.at(-1)?.state as LaterState | undefined) ?? initial;

  const reset = useCallback(() => {
    setPlaying(false); setApplied([]); setSelected(null); engineRef.current.reset(scenario.frames);
    if (id === "research") useSessionStore.getState().setResearch(RESEARCH_SESSION_ID, []);
  }, [id, scenario.frames]);
  useEffect(() => reset(), [reset]);

  const step = useCallback(() => {
    const next = engineRef.current.step();
    if (!next) { setPlaying(false); return; }
    if (id === "research" && String(next.event.type).startsWith("research_")) applyWsEvent(next.event);
    setApplied((current) => [...current, next]);
    setSelected(engineRef.current.position - 1);
    if (engineRef.current.done) setPlaying(false);
  }, [id]);
  useEffect(() => { if (!playing) return; const timer = window.setTimeout(step, 760); return () => window.clearTimeout(timer); }, [applied.length, playing, step]);
  const progress = engineRef.current.length ? Math.round(engineRef.current.position / engineRef.current.length * 100) : 0;

  return <main className={`anatomy-page later-page later-${id}`}>
    <header className="anatomy-nav"><a className="anatomy-brand" href="/function-cabinet.html" aria-label="Owlery 功能标本馆首页"><span className="brand-mark"><OwleryLogo size={22} /></span><span><strong>Owlery</strong><small>FUNCTION CABINET</small></span></a><div className="nav-center"><span>标本 {config.number}</span><strong>{config.title}</strong></div><div className="cabinet-nav-links"><a className="nav-principle" href="#principle">查看原理</a><a className="nav-principle nav-prev" href={config.prev}>← 上一件</a>{config.next && <a className="nav-principle nav-next" href={config.next}>下一件 →</a>}</div></header>
    <section className="anatomy-hero later-hero" id="top"><div className="hero-index">SPECIMEN / {config.number}</div><div className="hero-copy"><span className="eyebrow"><span className="eyebrow-line" /> {config.study}</span><h1>{config.headline[0]}<br />{config.headline[1]}</h1></div><div className="hero-proof">{config.proofs.map(([value, label]) => <div key={label}><strong>{value}</strong><span>{label}</span></div>)}</div></section>
    <nav className="scenario-tabs later-tabs" aria-label={`选择${config.title}场景`}>{config.scenarios.map((item) => <button type="button" className="scenario-tab" data-active={item.id === scenarioId} key={item.id} onClick={() => setScenarioId(item.id)}><span>{item.index}</span><div><strong>{item.title}</strong><small>{item.description}</small></div></button>)}</nav>
    <section className="later-workbench"><div className="later-demo"><div className="panel-heading demo-heading"><div><span className="eyebrow">OPERABLE SYSTEM MODEL</span><h2>{scenario.title}</h2></div><p>{scenario.lesson}</p></div><Stage id={id} state={state} scenarioId={scenarioId} frames={applied} /><div className="later-controls"><div className="control-buttons"><Button onClick={() => setPlaying((value) => !value)}>{playing ? <IconPlayerPause /> : <IconPlayerPlay />}{playing ? "暂停" : "播放"}</Button><Button variant="outline" onClick={step} disabled={engineRef.current.done}><IconStepInto />单步执行</Button><Button variant="ghost" onClick={reset}><IconRefresh />重置</Button></div><div className="progress-track"><span style={{ width: `${progress}%` }} /></div><code>{engineRef.current.position} / {engineRef.current.length}</code></div></div><Inspector frames={applied} selected={selected} onSelect={setSelected} /></section>
    <Principles id={id} />
  </main>;
}
