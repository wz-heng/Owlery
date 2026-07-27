import type { LaterSpecimenId } from "./laterSpecimens";

export interface LaterArticleTraceStep {
  id: string;
  actor: string;
  title: string;
  detail: string;
}

export interface LaterArticleMeta {
  title: string;
  traceTitle: string;
  trace: LaterArticleTraceStep[];
}

export const LATER_ARTICLE_META: Record<LaterSpecimenId, LaterArticleMeta> = {
  research: {
    title: "深度研究的有界编排与证据交付",
    traceTitle: "一次研究作业从问题到报告",
    trace: [
      { id: "R0", actor: "Session", title: "接收研究问题", detail: "主会话创建 research job，记录问题、父会话和初始 queued 状态" },
      { id: "R1", actor: "Scope leaf", title: "把问题收口为有限角度", detail: "规划结果解析为不超过 5 个角度，异常格式退化为有限输入" },
      { id: "R2", actor: "Manager", title: "启动隔离搜索叶子", detail: "每个角度进入 scratch cwd，清空 MCP、连接器与长期记忆，并发度限制为 4" },
      { id: "R3", actor: "Ranker", title: "合并候选事实", detail: "每角度最多 6 条 finding，去重排序后最多保留 12 条待验证 claim" },
      { id: "R4", actor: "Verifier", title: "对每条主张独立投票", detail: "默认发出 2 个验证叶子；两票都明确反驳时，该 claim 从后续输入中消失" },
      { id: "R5", actor: "Synthesizer", title: "只读取 survivors", detail: "合成器接收原问题、通过验证的主张与来源；零 survivors 仍生成明确的无证据报告" },
      { id: "R6", actor: "ResearchManager", title: "持久化报告与作业终态", detail: "先写 Markdown 报告，再标记 completed 并广播 research_completed" },
      { id: "R7", actor: "SessionManager", title: "把报告注入普通 Turn", detail: "injection_status 单独记录 delivered 或 failed，研究完成不被交付失败反写" },
    ],
  },
  fork: {
    title: "会话分叉、历史身份与副作用补偿",
    traceTitle: "一次 Rewind 从选点到新会话",
    trace: [
      { id: "F0", actor: "Browser", title: "选择一条人类消息", detail: "界面提交服务端 message seq，而不是当前数组下标或屏幕位置" },
      { id: "F1", actor: "SessionManager", title: "构造分叉预览", detail: "读取分叉点后的持久消息，排除机器注入提示，并解析预填问题" },
      { id: "F2", actor: "Side-effect scanner", title: "分类已经发生的外部作用", detail: "文件编辑、后台任务和不可逆工具分别列账，Bash 写入只做保守推断" },
      { id: "F3", actor: "Revert preflight", title: "检查文件还原资格", detail: "仓库身份、fork HEAD、初始 clean 状态和当前 dirty paths 必须同时通过" },
      { id: "F4", actor: "SessionManager", title: "创建带 lineage 的子会话", detail: "子会话保存 parent_session_id、fork seq、模式和恢复元数据，旧消息从不原地删除" },
      { id: "F5", actor: "RuntimeProfile", title: "复制或重放后端上下文", detail: "Claude 复制原生 transcript；Codex 可采用中立历史 replay，失败时按 fork id 清理孤儿材料" },
      { id: "F6", actor: "Fork helper", title: "可选执行文件补偿", detail: "只对已审计路径 stash 并还原；拒绝或失败不会撤销已经创建的分支" },
      { id: "F7", actor: "Session lifecycle", title: "启动子分支并处理父会话", detail: "Rewind 归档父分支，Fork 保留父子并存；两者都继续使用普通会话执行语义" },
    ],
  },
  memory: {
    title: "长期记忆的文件模型与身份边界",
    traceTitle: "一条偏好如何跨会话生效",
    trace: [
      { id: "M0", actor: "SessionManager", title: "从 session 解析 Agent 身份", detail: "只有绑定 agent_id 的主会话才能得到规范 memory 目录" },
      { id: "M1", actor: "Harness assembly", title: "注入同一个绝对目录", detail: "Claude 使用 memory path override，Codex 使用开发者指令；认证目录保持原位" },
      { id: "M2", actor: "Agent", title: "任务开始先读 MEMORY.md", detail: "索引只包含标题、链接和一行用途，不把全部事实灌入上下文" },
      { id: "M3", actor: "Agent", title: "选择少量相关主题文件", detail: "当前问题命中 preferences.md 时才读取正文，未命中事实不占本轮上下文" },
      { id: "M4", actor: "Agent", title: "判断事实是否值得持久化", detail: "只保存 durable、non-secret、actionable 的 user、feedback、project 或 reference 事实" },
      { id: "M5", actor: "Filesystem", title: "更新主题文件并同步索引", detail: "同主题优先修改原文件，避免 preferences-2.md 一类近义副本" },
      { id: "M6", actor: "Next session", title: "换后端仍解析到同一身份目录", detail: "会话 resume 数据互不共享，但稳定事实继续由 Agent 身份拥有" },
      { id: "M7", actor: "Lifecycle", title: "归档保留，硬删除清除", detail: "归档只改变会话可见性；删除 Agent 才递归移除整个身份目录" },
    ],
  },
  harness: {
    title: "统一 Harness 的运行边界与故障恢复",
    traceTitle: "一个 Turn 如何穿过不同 CLI 后端",
    trace: [
      { id: "H0", actor: "SessionManager", title: "组装中立 TurnContext", detail: "prompt、cwd、resume id、工具、MCP、模型、凭证和记忆路径先进入统一结构" },
      { id: "H1", actor: "RuntimeProfile", title: "选择后端能力与渲染器", detail: "binary、argv、环境变量、stream parser、错误模式和 fork codec 都来自 Profile" },
      { id: "H2", actor: "HarnessRun", title: "启动受控子进程", detail: "运行器建立 stdout/stderr 消费、取消句柄、idle watchdog 与 overall cap" },
      { id: "H3", actor: "Stream parser", title: "归一化后端输出", detail: "Claude 与 Codex 的原始事件被翻译成同一组文本、工具、结果和 usage 事件" },
      { id: "H4", actor: "Session reducer", title: "广播并持久化中立事件", detail: "产品层不读取某个 CLI 的私有 JSON，界面和消息历史只依赖 Owlery 契约" },
      { id: "H5", actor: "Failure classifier", title: "按具体原因选择出口", detail: "auth、limit、transient、premature exit 与 timeout 按互斥顺序分类" },
      { id: "H6", actor: "Recovery policy", title: "只在安全谓词成立时恢复", detail: "无可见输出的瞬时失败可有界重试；工具后早退只允许 continue 一次" },
      { id: "H7", actor: "HarnessRun", title: "结算、清理并释放控制权", detail: "停止进程、汇总 usage、写入终态；timeout 和认证失败不会伪装成普通完成" },
    ],
  },
  automation: {
    title: "调度解析、会话执行与通知隔离",
    traceTitle: "一个自然语言计划如何执行一次",
    trace: [
      { id: "A0", actor: "API", title: "接收计划文本与任务 Prompt", detail: "请求携带 recurrence 意图、时区、origin session 和通知配置" },
      { id: "A1", actor: "Schedule parser", title: "先尝试确定性快路径", detail: "规则明确的 interval 直接解析；复杂自然语言才调用一次结构化模型" },
      { id: "A2", actor: "Validator", title: "把模型输出约束为三种结构", detail: "once、cron、interval 分别校验必需字段，未知时区规范化而非直接执行自由文本" },
      { id: "A3", actor: "Scheduler", title: "持久化并注册触发器", detail: "数据库保存计划语义，APScheduler 只负责在时间到达时唤醒统一执行入口" },
      { id: "A4", actor: "Trigger", title: "决定复用还是创建会话", detail: "原会话仍存活就排队；缺失时创建 origin=schedule 的临时会话" },
      { id: "A5", actor: "SessionManager", title: "按普通 Turn 执行任务", detail: "自动化不拥有第二套 Agent 运行器，仍走 start_message、Harness、消息持久化与队列" },
      { id: "A6", actor: "Lifecycle", title: "收口计划与临时会话", detail: "one-shot 在 finally 删除，临时会话完成后归档，原会话保持自己的生命周期" },
      { id: "A7", actor: "NotifierManager", title: "并行通知且隔离失败", detail: "每个目标独立捕获异常；Webhook 超时不回滚 Agent 结果，也不阻断其他通知器" },
    ],
  },
};
