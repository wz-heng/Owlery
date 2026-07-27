import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { flushSync } from "react-dom";

import { AgentDelegationSpecimen } from "./agent-delegation/AgentDelegationSpecimen";
import { BgTaskPipelineSpecimen } from "./bg-task-pipeline/BgTaskPipelineSpecimen";
import { FunctionCabinet } from "./function-cabinet/FunctionCabinet";
import { CabinetSidebar, type CabinetLocation } from "./shared/CabinetSidebar";
import { LaterSpecimen } from "./shared/LaterSpecimen";
import { StreamingAnatomy } from "./streaming-anatomy/StreamingAnatomy";

interface CabinetRoute {
  current: CabinetLocation;
  title: string;
  render: () => ReactNode;
}

const CABINET_ROUTES: Record<string, CabinetRoute> = {
  "/function-cabinet.html": {
    current: "overview",
    title: "Owlery 功能标本馆 — 全局概览",
    render: () => <FunctionCabinet />,
  },
  "/streaming-anatomy.html": {
    current: "001",
    title: "流式 AI 对话解剖 — Owlery 功能标本馆",
    render: () => <StreamingAnatomy />,
  },
  "/agent-delegation.html": {
    current: "002",
    title: "多 Agent 委派解剖 — Owlery 功能标本馆",
    render: () => <AgentDelegationSpecimen />,
  },
  "/bg-task-pipeline.html": {
    current: "003",
    title: "后台任务回流解剖 — Owlery 功能标本馆",
    render: () => <BgTaskPipelineSpecimen />,
  },
  "/deep-research.html": {
    current: "004",
    title: "原生深度研究解剖 — Owlery 功能标本馆",
    render: () => <LaterSpecimen id="research" />,
  },
  "/session-fork-rewind.html": {
    current: "005",
    title: "会话 Fork / Rewind 解剖 — Owlery 功能标本馆",
    render: () => <LaterSpecimen id="fork" />,
  },
  "/agent-memory.html": {
    current: "006",
    title: "Agent 长期记忆解剖 — Owlery 功能标本馆",
    render: () => <LaterSpecimen id="memory" />,
  },
  "/harness-recovery.html": {
    current: "007",
    title: "Harness 与故障恢复解剖 — Owlery 功能标本馆",
    render: () => <LaterSpecimen id="harness" />,
  },
  "/automation-pipeline.html": {
    current: "008",
    title: "调度与通知解剖 — Owlery 功能标本馆",
    render: () => <LaterSpecimen id="automation" />,
  },
};

function routePath(): string {
  return CABINET_ROUTES[window.location.pathname]
    ? window.location.pathname
    : "/function-cabinet.html";
}

export function CabinetApp() {
  const [path, setPath] = useState(routePath);
  const pendingHash = useRef(window.location.hash);

  const commitRoute = useCallback((nextPath: string) => {
    const update = () => flushSync(() => setPath(nextPath));
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    if (typeof document.startViewTransition === "function" && !reducedMotion) {
      document.startViewTransition(update);
    } else {
      update();
    }
  }, []);

  const navigate = useCallback((url: URL, push: boolean) => {
    if (!CABINET_ROUTES[url.pathname]) return false;
    pendingHash.current = url.hash;
    if (push) window.history.pushState({ cabinet: true }, "", `${url.pathname}${url.search}${url.hash}`);
    commitRoute(url.pathname);
    return true;
  }, [commitRoute]);

  useEffect(() => {
    const onClick = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const target = event.target;
      if (!(target instanceof Element)) return;
      const anchor = target.closest<HTMLAnchorElement>("a[href]");
      if (!anchor || anchor.target || anchor.hasAttribute("download")) return;

      const url = new URL(anchor.href, window.location.href);
      if (url.origin !== window.location.origin || !CABINET_ROUTES[url.pathname]) return;
      if (url.pathname === window.location.pathname && url.search === window.location.search) return;

      event.preventDefault();
      navigate(url, true);
    };
    const onPopState = () => navigate(new URL(window.location.href), false);

    document.addEventListener("click", onClick);
    window.addEventListener("popstate", onPopState);
    return () => {
      document.removeEventListener("click", onClick);
      window.removeEventListener("popstate", onPopState);
    };
  }, [navigate]);

  useLayoutEffect(() => {
    const route = CABINET_ROUTES[path];
    document.title = route.title;
    const frame = window.requestAnimationFrame(() => {
      const hash = pendingHash.current;
      if (hash) document.getElementById(hash.slice(1))?.scrollIntoView();
      else window.scrollTo({ top: 0, left: 0 });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [path]);

  const route = CABINET_ROUTES[path];
  return (
    <div className="cabinet-app">
      <CabinetSidebar current={route.current} />
      <div className="cabinet-route">
        <div className="cabinet-route-page" key={path}>{route.render()}</div>
      </div>
    </div>
  );
}
