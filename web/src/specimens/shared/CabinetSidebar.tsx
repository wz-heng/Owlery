import {
  IconChevronLeft,
  IconChevronRight,
  IconMenu2,
  IconX,
} from "@tabler/icons-react";
import { useEffect, useState } from "react";

const CABINET_NAV = [
  { id: "001", title: "流式 AI 对话", href: "/streaming-anatomy.html" },
  { id: "002", title: "多 Agent 委派", href: "/agent-delegation.html" },
  { id: "003", title: "后台任务回流", href: "/bg-task-pipeline.html" },
  { id: "004", title: "原生深度研究", href: "/deep-research.html" },
  { id: "005", title: "会话 Fork / Rewind", href: "/session-fork-rewind.html" },
  { id: "006", title: "Agent 长期记忆", href: "/agent-memory.html" },
  { id: "007", title: "Harness 与故障恢复", href: "/harness-recovery.html" },
  { id: "008", title: "调度与通知", href: "/automation-pipeline.html" },
] as const;

export type CabinetLocation = "overview" | (typeof CABINET_NAV)[number]["id"];

export function CabinetSidebar({ current }: { current: CabinetLocation }) {
  const [open, setOpen] = useState(false);
  const [mobile, setMobile] = useState(false);
  const [collapsed, setCollapsed] = useState(() => window.localStorage.getItem("owlery:cabinet-sidebar-collapsed") === "true");

  useEffect(() => {
    const query = window.matchMedia("(max-width: 900px)");
    const sync = () => {
      setMobile(query.matches);
      if (!query.matches) setOpen(false);
    };
    sync();
    query.addEventListener("change", sync);
    return () => query.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    if (!open) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [open]);

  const toggleCollapsed = () => {
    setCollapsed((value) => {
      const next = !value;
      window.localStorage.setItem("owlery:cabinet-sidebar-collapsed", String(next));
      return next;
    });
  };

  return (
    <>
      <button
        className="cabinet-sidebar-toggle"
        type="button"
        aria-label="打开标本目录"
        aria-expanded={open}
        aria-controls="cabinet-sidebar"
        onClick={() => setOpen(true)}
      >
        <IconMenu2 />
        <span>书签</span>
      </button>

      <button
        className="cabinet-sidebar-backdrop"
        type="button"
        aria-label="关闭标本目录"
        data-open={open}
        onClick={() => setOpen(false)}
      />

      <aside
        className="cabinet-sidebar"
        id="cabinet-sidebar"
        data-open={open}
        data-collapsed={collapsed}
        aria-label="功能标本馆目录"
        aria-hidden={mobile && !open}
        inert={mobile && !open ? true : undefined}
      >
        <header className="cabinet-sidebar-header">
          <div className="cabinet-sidebar-title"><small>CABINET INDEX</small><strong>标本书签</strong></div>
          <button className="cabinet-sidebar-collapse" type="button" aria-label={collapsed ? "展开标本目录" : "折叠标本目录"} aria-expanded={!collapsed} onClick={toggleCollapsed}>{collapsed ? <IconChevronRight /> : <IconChevronLeft />}</button>
          <button className="cabinet-sidebar-close" type="button" aria-label="关闭标本目录" onClick={() => setOpen(false)}><IconX /></button>
        </header>

        <nav className="cabinet-sidebar-nav" aria-label="标本导航">
          <span className="cabinet-sidebar-label">OVERVIEW</span>
          <a className="cabinet-bookmark" href="/function-cabinet.html" aria-label="总览 功能标本馆" aria-current={current === "overview" ? "page" : undefined} onClick={() => setOpen(false)}>
            <span className="cabinet-bookmark-index">◎</span>
            <span className="cabinet-bookmark-copy"><small>总览</small><strong>功能标本馆</strong></span>
          </a>

          <span className="cabinet-sidebar-label">SPECIMENS / 001—008</span>
          {CABINET_NAV.map((item) => (
            <a className="cabinet-bookmark" href={item.href} key={item.id} aria-label={`${item.id} ${item.title}`} aria-current={current === item.id ? "page" : undefined} onClick={() => setOpen(false)}>
              <span className="cabinet-bookmark-index">{item.id}</span>
              <span className="cabinet-bookmark-copy"><small>SPECIMEN</small><strong>{item.title}</strong></span>
            </a>
          ))}
        </nav>

        <footer className="cabinet-sidebar-footer"><span>8 / 8</span><small>COLLECTION COMPLETE</small></footer>
      </aside>
    </>
  );
}
