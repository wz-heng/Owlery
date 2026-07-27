export type DelegationScenarioId = "success" | "question" | "cancel" | "nested";

export interface DelegationSpecimenEvent {
  type:
    | "parent_prompt"
    | "delegation_started"
    | "child_running"
    | "child_text"
    | "child_question"
    | "parent_answer"
    | "nested_started"
    | "nested_reply"
    | "child_reply"
    | "child_error"
    | "child_cancelled";
  actor: "User" | "Aberforth" | "Dobby" | "Researcher" | "Owlery";
  content?: string;
  delegationId?: string;
  target?: string;
  questionId?: string;
  reason?: string;
}

export interface DelegationScript {
  id: DelegationScenarioId;
  index: string;
  title: string;
  description: string;
  lesson: string;
  events: DelegationSpecimenEvent[];
}

export const PARENT_SESSION_ID = "specimen-parent-aberforth";
export const DOBBY_SESSION_ID = "dlg-dobby-a1b2c3";
export const RESEARCH_SESSION_ID = "dlg-research-d4e5f6";

export const DELEGATION_SCRIPTS: DelegationScript[] = [
  {
    id: "success",
    index: "01",
    title: "独立执行与结果回流",
    description: "父 Agent 立即拿到 id，子会话完成后再把结果注入回来。",
    lesson: "委派不是阻塞函数调用；它是一个跨 turn、可独立观察的子会话。",
    events: [
      { type: "parent_prompt", actor: "User", content: "让 Dobby 独立审查 streaming anatomy 的事件去重设计。" },
      { type: "delegation_started", actor: "Aberforth", delegationId: DOBBY_SESSION_ID, target: "Dobby", content: "审查事件去重设计，重点检查重连竞态和 seq 边界。" },
      { type: "child_running", actor: "Owlery", delegationId: DOBBY_SESSION_ID, content: "子会话已创建并开始运行。" },
      { type: "child_text", actor: "Dobby", content: "我先核对快照基线与 WebSocket 广播之间的竞争窗口。" },
      { type: "child_text", actor: "Dobby", content: "结论：服务端 seq + lastAppliedSeq 守卫成立；无 seq 的瞬时状态必须始终应用。" },
      { type: "child_reply", actor: "Owlery", delegationId: DOBBY_SESSION_ID, target: "Dobby", content: "审查通过。重复消息会在写入 Store 前被丢弃；状态事件不会被快照基线误伤。" },
    ],
  },
  {
    id: "question",
    index: "02",
    title: "问题逐级返回",
    description: "子 Agent 信息不足，问题先回父 Agent，再决定是否升级给用户。",
    lesson: "子 Agent 不越级找用户；调用链上的每一层都要先承担判断责任。",
    events: [
      { type: "parent_prompt", actor: "User", content: "请 Dobby 设计部署方案。" },
      { type: "delegation_started", actor: "Aberforth", delegationId: DOBBY_SESSION_ID, target: "Dobby", content: "为功能标本馆设计部署方案。" },
      { type: "child_running", actor: "Owlery", delegationId: DOBBY_SESSION_ID },
      { type: "child_question", actor: "Dobby", delegationId: DOBBY_SESSION_ID, questionId: "q-deploy-1", content: "首个公开版本需要匿名访问，还是必须登录？\n\nA. 匿名公开预览\nB. Owlery 登录后访问" },
      { type: "parent_answer", actor: "Aberforth", delegationId: DOBBY_SESSION_ID, questionId: "q-deploy-1", content: "选择 A：匿名公开预览。这个决定已有用户上下文，不必再次打扰用户。" },
      { type: "child_text", actor: "Dobby", content: "收到。我会把标本作为独立静态入口构建，不依赖产品登录态。" },
      { type: "child_reply", actor: "Owlery", delegationId: DOBBY_SESSION_ID, target: "Dobby", content: "部署方案完成：独立 HTML 入口、静态托管、可选自定义域名。" },
    ],
  },
  {
    id: "cancel",
    index: "03",
    title: "取消与失败收口",
    description: "父 Agent 撤回任务，运行中的子会话和后代一起停止。",
    lesson: "取消不是把卡片染灰；它必须沿 parent_session_id 链真正终止执行。",
    events: [
      { type: "parent_prompt", actor: "User", content: "让 Dobby 跑一次长时间兼容性研究。" },
      { type: "delegation_started", actor: "Aberforth", delegationId: DOBBY_SESSION_ID, target: "Dobby", content: "运行跨浏览器兼容性研究，并等待所有结果。" },
      { type: "child_running", actor: "Owlery", delegationId: DOBBY_SESSION_ID },
      { type: "child_text", actor: "Dobby", content: "研究任务已经启动，正在等待多个浏览器环境返回。" },
      { type: "child_cancelled", actor: "Owlery", delegationId: DOBBY_SESSION_ID, target: "Dobby", reason: "scope changed", content: "父会话取消了委派；子进程和运行中的后代已停止。" },
    ],
  },
  {
    id: "nested",
    index: "04",
    title: "嵌套委派",
    description: "Dobby 再委派给 Researcher，结果按原路逐层返回。",
    lesson: "嵌套不是广播；每一跳都有明确父会话、深度上限和结果所有者。",
    events: [
      { type: "parent_prompt", actor: "User", content: "让 Dobby 判断浏览器兼容策略是否合理。" },
      { type: "delegation_started", actor: "Aberforth", delegationId: DOBBY_SESSION_ID, target: "Dobby", content: "审查兼容策略，需要数据时可以委派研究。" },
      { type: "child_running", actor: "Owlery", delegationId: DOBBY_SESSION_ID },
      { type: "child_text", actor: "Dobby", content: "我需要最新浏览器份额数据，交给 Researcher 独立查证。" },
      { type: "nested_started", actor: "Dobby", delegationId: RESEARCH_SESSION_ID, target: "Researcher", content: "查证目标浏览器份额与 WebSocket 支持情况。" },
      { type: "nested_reply", actor: "Researcher", delegationId: RESEARCH_SESSION_ID, target: "Researcher", content: "查证完成：目标现代浏览器均支持所需 WebSocket 与 ES2022 能力。" },
      { type: "child_text", actor: "Dobby", content: "我已合并 Researcher 的证据，兼容策略成立。" },
      { type: "child_reply", actor: "Owlery", delegationId: DOBBY_SESSION_ID, target: "Dobby", content: "审查通过：当前浏览器基线合理，证据来自独立研究子会话。" },
    ],
  },
];
