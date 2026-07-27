import {
  IconArrowRight,
  IconBell,
  IconBrain,
  IconBrandDatabricks,
  IconGitBranch,
  IconMessages,
  IconRoute,
  IconSearch,
  IconTerminal2,
  IconUsers,
} from "@tabler/icons-react";

import { OwleryLogo } from "../../components/OwleryLogo";

const SPECIMENS = [
  { number: "001", title: "流式 AI 对话", subtitle: "事件如何变成界面", href: "/streaming-anatomy.html", group: "实时执行", icon: IconMessages, tags: ["WebSocket", "seq 去重", "审批"] },
  { number: "002", title: "多 Agent 委派", subtitle: "工作如何沿调用链回流", href: "/agent-delegation.html", group: "协作扩展", icon: IconUsers, tags: ["子会话", "问题回传", "嵌套"] },
  { number: "003", title: "后台任务回流", subtitle: "Turn 结束后进程如何存活", href: "/bg-task-pipeline.html", group: "实时执行", icon: IconBrandDatabricks, tags: ["进程 owner", "持久化", "结果注入"] },
  { number: "004", title: "原生深度研究", subtitle: "搜索如何经过验证变成结论", href: "/deep-research.html", group: "协作扩展", icon: IconSearch, tags: ["并行叶子", "反驳投票", "合成"] },
  { number: "005", title: "会话 Fork / Rewind", subtitle: "如何分叉而不伪造副作用", href: "/session-fork-rewind.html", group: "协作扩展", icon: IconGitBranch, tags: ["消息序号", "副作用审计", "安全还原"] },
  { number: "006", title: "Agent 长期记忆", subtitle: "身份如何跨会话和后端延续", href: "/agent-memory.html", group: "持续运行", icon: IconBrain, tags: ["Markdown", "短索引", "生命周期"] },
  { number: "007", title: "Harness 与故障恢复", subtitle: "不同 CLI 如何共享一套运行法则", href: "/harness-recovery.html", group: "实时执行", icon: IconTerminal2, tags: ["RuntimeProfile", "有界重试", "看门狗"] },
  { number: "008", title: "调度与通知", subtitle: "无人聊天时 Agent 如何按时工作", href: "/automation-pipeline.html", group: "持续运行", icon: IconBell, tags: ["recurrence", "新会话", "通知隔离"] },
] as const;

const GROUPS = [
  { name: "实时执行", note: "输入进入系统后，如何被可靠地执行、观察和恢复。", numbers: ["001", "003", "007"] },
  { name: "协作扩展", note: "一个会话如何扩展成多个执行分支，又保持边界清楚。", numbers: ["002", "004", "005"] },
  { name: "持续运行", note: "会话之外，身份与工作如何继续存在。", numbers: ["006", "008"] },
] as const;

function CapabilityMap() {
  return (
    <section className="cabinet-map" aria-labelledby="map-title">
      <div className="cabinet-section-heading">
        <span className="eyebrow">SYSTEM MAP</span>
        <h2 id="map-title">八件标本，其实是一套完整运行系统。</h2>
        <p>从一条消息进入，到工作扩展、状态保留，再到未来某个时刻自动重启。</p>
      </div>
      <div className="capability-map" aria-label="Owlery 八项能力关系图">
        <div className="map-spine"><span>MESSAGE</span><i /><span>WORK</span><i /><span>STATE</span><i /><span>TIME</span></div>
        {GROUPS.map((group, groupIndex) => (
          <div className="map-lane" key={group.name} data-lane={groupIndex + 1}>
            <div className="map-lane-label"><strong>{group.name}</strong><small>{group.note}</small></div>
            <div className="map-nodes">
              {group.numbers.map((number) => {
                const specimen = SPECIMENS.find((item) => item.number === number)!;
                const Icon = specimen.icon;
                return <a key={number} href={specimen.href} aria-label={`${number} ${specimen.title}`}><span>{number}</span><Icon /><strong>{specimen.title}</strong></a>;
              })}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

export function FunctionCabinet() {
  return (
    <main className="anatomy-page cabinet-page">
      <header className="anatomy-nav cabinet-home-nav">
        <a className="anatomy-brand" href="#top"><span className="brand-mark"><OwleryLogo size={22} /></span><span><strong>Owlery</strong><small>FUNCTION CABINET</small></span></a>
        <div className="nav-center"><span>总览</span><strong>功能标本馆</strong></div>
        <a className="nav-principle" href="#specimens">浏览全部标本 <IconArrowRight /></a>
      </header>

      <section className="cabinet-home-hero" id="top">
        <div className="cabinet-home-index">CABINET / 001—008</div>
        <div className="cabinet-home-copy"><span className="eyebrow"><span className="eyebrow-line" /> INTERACTIVE SYSTEM SPECIMENS</span><h1>不只展示功能。<br />把它的骨头也摆出来。</h1><p>每件标本都可以播放、暂停和单步执行。你看到的不只是效果，还包括事件、状态、失败路径和实现约束。</p></div>
        <div className="cabinet-home-proof"><div><strong>8</strong><span>件可交互标本</span></div><div><strong>24</strong><span>条场景路径</span></div><div><strong>40+</strong><span>项系统法则</span></div></div>
      </section>

      <CapabilityMap />

      <section className="cabinet-catalog" id="specimens" aria-labelledby="catalog-title">
        <div className="cabinet-section-heading"><span className="eyebrow">ALL SPECIMENS</span><h2 id="catalog-title">从任何一件开始。</h2><p>编号代表阅读顺序，不代表依赖顺序。</p></div>
        <div className="specimen-grid">
          {SPECIMENS.map((specimen) => {
            const Icon = specimen.icon;
            return (
              <a className="specimen-card" href={specimen.href} key={specimen.number}>
                <header><span>SPECIMEN / {specimen.number}</span><em>{specimen.group}</em></header>
                <div className="specimen-card-glyph"><Icon /><i /><i /><i /></div>
                <h3>{specimen.title}</h3>
                <p>{specimen.subtitle}</p>
                <div className="specimen-tags">{specimen.tags.map((tag) => <span key={tag}>{tag}</span>)}</div>
                <footer><span>打开解剖台</span><IconArrowRight /></footer>
              </a>
            );
          })}
        </div>
      </section>

      <footer className="cabinet-footer"><OwleryLogo size={28} /><div><strong>Owlery Function Cabinet</strong><span>功能可以被演示，原理必须经得起追问。</span></div><IconRoute /></footer>
    </main>
  );
}
