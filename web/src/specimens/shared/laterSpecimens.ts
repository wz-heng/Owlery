import type { ReplayFrame } from "./LaterReplayEngine";

export type LaterSpecimenId = "research" | "fork" | "memory" | "harness" | "automation";

export interface LaterState {
  phase: string;
  status: string;
  primary: number;
  secondary: number;
  detail: string;
  active?: string;
}

export interface LaterScenario {
  id: string;
  index: string;
  title: string;
  description: string;
  lesson: string;
  frames: ReplayFrame[];
}

export interface PrincipleChapter {
  kicker: string;
  title: string;
  body: string;
  law: string;
  consequence: string;
}

export interface LaterSpecimenConfig {
  id: LaterSpecimenId;
  number: string;
  title: string;
  study: string;
  headline: [string, string];
  intro: string;
  proofs: [string, string][];
  prev: string;
  next?: string;
  scenarios: LaterScenario[];
  principles: PrincipleChapter[];
}

const frame = (label: string, actor: string, note: string, state: LaterState, event: Record<string, unknown>): ReplayFrame => ({ label, actor, note, state, event });

export const LATER_SPECIMENS: Record<LaterSpecimenId, LaterSpecimenConfig> = {
  research: {
    id: "research", number: "004", title: "原生深度研究解剖", study: "BOUNDED RESEARCH ORCHESTRATION",
    headline: ["深度研究的", "有界证据管线"],
    intro: "把一个开放问题拆成有限角度，让搜索叶子并行取证，再用对抗投票淘汰站不住脚的主张，最后才合成带来源的报告。",
    proofs: [["5", "最大研究角度"], ["4", "并发叶子上限"], [">½", "反驳即淘汰"]], prev: "/bg-task-pipeline.html", next: "/session-fork-rewind.html",
    scenarios: [
      { id: "verified", index: "A", title: "可信报告", description: "完整跑过五个阶段", lesson: "同一张真实 ResearchCard 随生产事件推进；中间结果不是聊天文本，而是有边界的研究状态。", frames: [
        frame("创建研究作业", "orchestrator", "记录问题并进入 scope", { phase: "scope", status: "running", primary: 0, secondary: 0, detail: "正在规划 4 个互不重叠的研究角度", active: "scope" }, { type: "research_started", session_id: "specimen-research", research_id: "research-004", question: "AI Agent 的长期记忆怎样避免污染？" }),
        frame("生成研究角度", "scope model", "输出受 schema 约束", { phase: "scope", status: "running", primary: 4, secondary: 0, detail: "4 angles accepted", active: "scope" }, { type: "research_progress", session_id: "specimen-research", research_id: "research-004", phase: "scope", detail: "4 angles accepted", counts: { angles: 4 } }),
        frame("并行搜索", "search leaves", "semaphore 只放行 4 个叶子", { phase: "search", status: "running", primary: 4, secondary: 18, detail: "18 findings from 4 angles", active: "search" }, { type: "research_progress", session_id: "specimen-research", research_id: "research-004", phase: "search", detail: "18 findings from 4 angles", counts: { angles: 4, findings: 18 } }),
        frame("去重与排序", "ranker", "合并同源与重复证据", { phase: "search", status: "running", primary: 4, secondary: 12, detail: "12 candidate claims remain", active: "rank" }, { type: "research_progress", session_id: "specimen-research", research_id: "research-004", phase: "search", detail: "12 candidate claims remain", counts: { findings: 12 } }),
        frame("对抗验证", "verification voters", "每条主张接受独立反驳票", { phase: "verify", status: "running", primary: 12, secondary: 9, detail: "9 of 12 claims survived", active: "verify" }, { type: "research_progress", session_id: "specimen-research", research_id: "research-004", phase: "verify", detail: "9 of 12 claims survived", counts: { candidates: 12, verified: 9 } }),
        frame("合成报告", "reason model", "只消费通过验证的 findings", { phase: "synthesize", status: "running", primary: 9, secondary: 6, detail: "writing with 6 unique sources", active: "synthesize" }, { type: "research_progress", session_id: "specimen-research", research_id: "research-004", phase: "synthesize", detail: "writing with 6 unique sources", counts: { verified: 9, sources: 6 } }),
        frame("交付", "session manager", "报告作为普通 follow-up turn 注入", { phase: "done", status: "completed", primary: 9, secondary: 6, detail: "report delivered", active: "done" }, { type: "research_completed", session_id: "specimen-research", research_id: "research-004", verified: 9, sources: ["source-1", "source-2", "source-3", "source-4", "source-5", "source-6"] }),
      ] },
      { id: "cancel", index: "B", title: "中途取消", description: "停止仍保留语义", lesson: "取消不是失败别名。作业以 cancelled 收口，已产生的叶子不会被伪装成完整报告。", frames: [
        frame("研究启动", "orchestrator", "job persisted", { phase: "scope", status: "running", primary: 0, secondary: 0, detail: "planning", active: "scope" }, { type: "research_started", session_id: "specimen-research", research_id: "research-004", question: "比较三个 Agent 框架" }),
        frame("搜索进行中", "search leaves", "3 leaves active", { phase: "search", status: "running", primary: 3, secondary: 7, detail: "7 partial findings", active: "search" }, { type: "research_progress", session_id: "specimen-research", research_id: "research-004", phase: "search", detail: "7 partial findings", counts: { findings: 7 } }),
        frame("用户取消", "cancel endpoint", "cooperative cancellation", { phase: "search", status: "cancelled", primary: 3, secondary: 7, detail: "Cancelled.", active: "cancelled" }, { type: "research_failed", session_id: "specimen-research", research_id: "research-004", status: "cancelled", error: "Cancelled by user" }),
      ] },
      { id: "refuted", index: "C", title: "零证据报告", description: "全部主张被淘汰", lesson: "0 条可信主张仍会进入合成，但合成器拿到的是空 survivors。合法输出只能明确说明证据不足，不能把被淘汰的 findings 偷渡回结论。", frames: [
        frame("候选主张", "ranker", "8 claims queued", { phase: "scope", status: "running", primary: 8, secondary: 0, detail: "candidates ready", active: "scope" }, { type: "research_started", session_id: "specimen-research", research_id: "research-004", question: "某未经证实的新模型是否已发布？" }),
        frame("反驳投票", "verification voters", "strict majority rejected all", { phase: "verify", status: "running", primary: 8, secondary: 0, detail: "0 claims survived", active: "verify" }, { type: "research_progress", session_id: "specimen-research", research_id: "research-004", phase: "verify", detail: "0 claims survived", counts: { candidates: 8, verified: 0 } }),
        frame("空输入合成", "reason model", "survivors=[]", { phase: "synthesize", status: "running", primary: 0, secondary: 0, detail: "writing an explicit no-evidence report", active: "synthesize" }, { type: "research_progress", session_id: "specimen-research", research_id: "research-004", phase: "synthesize", detail: "0 verified claims", counts: { verified: 0, sources: 0 } }),
        frame("交付证据边界", "session manager", "no rejected finding re-enters", { phase: "done", status: "completed", primary: 0, secondary: 0, detail: "no-evidence report delivered", active: "done" }, { type: "research_completed", session_id: "specimen-research", research_id: "research-004", verified: 0, sources: [] }),
      ] },
    ],
    principles: [
      { kicker: "SCOPE FIRST", title: "先把开放问题变成有界问题", body: "规划器最多产生五个互不重叠的角度。角度数、每角度 findings、候选 claims 都有硬上限，因此成本和等待时间可以推理。", law: "max_angles=5 · findings/angle=6 · claims=12", consequence: "边界是产品语义，不是性能优化。" },
      { kicker: "TOOL-SCOPED LEAVES", title: "搜索叶子只被允许取证", body: "每个叶子有自己的查询范围、超时和工具权限；它们不继承长期记忆，也不能直接改写主会话。并行度由 semaphore 控制。", law: "concurrency=4 · leaf_timeout=150s", consequence: "并行扩大覆盖面，不扩大副作用面。" },
      { kicker: "ADVERSARIAL VERIFY", title: "生成者不能给自己判满分", body: "候选主张交给独立验证票。只要严格多数票认为证据反驳该主张，它就从合成输入中消失。", law: "refute_votes > votes / 2 → reject", consequence: "验证是数据流关卡，不是报告后的免责声明。" },
      { kicker: "SEPARATE PLANES", title: "进度卡与最终报告走不同平面", body: "WebSocket 更新 scope/search/verify/synthesize；完成后的正式报告则作为普通消息进入会话，能够持久化、回放和引用。", law: "progress event ≠ report message", consequence: "刷新页面不会把瞬时进度误当成果。" },
      { kicker: "HONEST FAILURE", title: "零条可信主张也是合法结果", body: "失败、取消和中断具有不同终态。即使 0 条主张通过验证，当前实现仍会用空 survivors 合成一份明确的无证据报告；被淘汰的 findings 不得重新进入答案。", law: "synthesis_input = verified survivors only", consequence: "零证据是可审计的输出，不是拿未验证材料补空白的借口。" },
    ],
  },
  fork: {
    id: "fork", number: "005", title: "会话 Fork / Rewind 解剖", study: "CONVERSATION BRANCHING & SIDE EFFECTS",
    headline: ["会话分叉与", "副作用补偿"], intro: "分叉不只是复制聊天记录：它要定位消息序号、复制或回放模型上下文、审计已经发生的工具副作用，并在可证明安全时才允许还原文件。",
    proofs: [["2", "种分叉语义"], ["3", "类副作用"], ["1", "个安全还原闸门"]], prev: "/deep-research.html", next: "/agent-memory.html",
    scenarios: [
      { id: "rewind", index: "A", title: "安全 Rewind", description: "从旧消息重做", lesson: "Rewind 创建子会话并归档父会话；文件还原只有在 git 锚点和工作树都通过预检时才可选。", frames: [
        frame("选择消息 #12", "user", "human-authored prompt only", { phase: "select", status: "ready", primary: 12, secondary: 0, detail: "重做登录页实现", active: "parent" }, { type: "fork_preview_requested", rewind_to_msg_seq: 12 }),
        frame("捕获分叉点", "session manager", "resolve turn-start git anchor", { phase: "checkpoint", status: "running", primary: 12, secondary: 1, detail: "anchor 91ac42f", active: "checkpoint" }, { type: "fork_checkpoint", git_anchor: "91ac42f" }),
        frame("审计副作用", "fork helper", "2 files, 1 bg task, 1 webhook", { phase: "audit", status: "running", primary: 2, secondary: 2, detail: "revert preflight passed", active: "audit" }, { type: "fork_side_effects", file_edits: 2, bg_tasks: 1, other_tools: 1 }),
        frame("建立子会话", "fork saga", "native transcript copied", { phase: "branch", status: "running", primary: 1, secondary: 1, detail: "parent → child", active: "child" }, { type: "fork_created", strategy: "native_copy", child_id: "child-rewind" }),
        frame("安全还原文件", "git", "restore to fork-point state", { phase: "revert", status: "completed", primary: 2, secondary: 0, detail: "files restored; external effects disclosed", active: "child" }, { type: "fork_reverted", files: ["web/Login.tsx", "web/login.css"] }),
      ] },
      { id: "dirty", index: "B", title: "脏树拒绝", description: "不确定就不还原", lesson: "副作用清单仍然展示，但 revert checkbox 被禁用。保守拒绝比误删用户未提交工作更正确。", frames: [
        frame("请求预览", "user", "rewind at #18", { phase: "select", status: "ready", primary: 18, secondary: 0, detail: "preview requested", active: "parent" }, { type: "fork_preview_requested", rewind_to_msg_seq: 18 }),
        frame("发现脏工作树", "git preflight", "uncommitted changes overlap", { phase: "audit", status: "blocked", primary: 3, secondary: 1, detail: "working tree changed since checkpoint", active: "audit" }, { type: "fork_revert_refused", reason: "Working tree has changes after the fork point" }),
        frame("只建立分支", "fork saga", "no filesystem rewind", { phase: "branch", status: "completed", primary: 1, secondary: 0, detail: "child created; files untouched", active: "child" }, { type: "fork_created", strategy: "history_replay", revert: false }),
      ] },
      { id: "copy", index: "C", title: "完整 Fork", description: "父子同时保留", lesson: "Fork 不归档父会话、不还原文件。Claude 复制原生 transcript；Codex 可用历史 replay，二者最后都形成普通子会话。", frames: [
        frame("复制当前会话", "user", "full fork", { phase: "select", status: "ready", primary: 24, secondary: 0, detail: "keep parent alive", active: "parent" }, { type: "fork_requested", mode: "copy" }),
        frame("选择后端策略", "runtime profile", "Codex history replay", { phase: "strategy", status: "running", primary: 24, secondary: 1, detail: "codec=history_replay", active: "checkpoint" }, { type: "fork_strategy", backend: "codex", strategy: "history_replay" }),
        frame("子会话就绪", "session manager", "lineage persisted", { phase: "branch", status: "completed", primary: 2, secondary: 0, detail: "parent and child both active", active: "both" }, { type: "fork_created", parent_id: "parent", child_id: "child-copy" }),
      ] },
    ],
    principles: [
      { kicker: "MESSAGE IDENTITY", title: "定位靠 server seq，不靠屏幕位置", body: "自动注入的 bg-task-result 和 agent-reply 虽然以 user role 持久化，却不是用户可重做的提示，因此选择器会主动过滤。", law: "human prompt seq → fork point", consequence: "重连和分页不会改变分叉语义。" },
      { kicker: "TWO SEMANTICS", title: "Rewind 与 Fork 不是同义词", body: "Rewind 从旧点重做并归档父分支；Fork 完整复制当前状态，父会话继续存在。两者都产生有 lineage 的普通子会话。", law: "rewind: replace · fork: coexist", consequence: "产品行为先明确，再谈实现策略。" },
      { kicker: "SIDE-EFFECT LEDGER", title: "对话可复制，外部世界只能披露", body: "文件编辑、后台任务、连接器/其他工具分开统计。只读调用被排除；Bash 写入只是保守推断，所以清单明确标注 best-effort。", law: "files · bg tasks · irreversible tools", consequence: "用户在确认前看见无法撤销的现实。" },
      { kicker: "SAFE REVERT", title: "还原权限来自预检，不来自按钮", body: "turn 开始时记录 git anchor；只有仓库身份、锚点和工作树都满足条件时，revert 才可用。", law: "preflight false → checkbox disabled", consequence: "不能证明安全，就不碰文件。" },
      { kicker: "BACKEND CODEC", title: "分叉协议统一，转录策略可替换", body: "Claude 可以复制原生 transcript；Codex 可以重放中立历史。能力由 RuntimeProfile 声明，session manager 不写后端分支。", law: "one fork contract · multiple codecs", consequence: "后端差异被压在适配边界内。" },
    ],
  },
  memory: {
    id: "memory", number: "006", title: "Agent 长期记忆解剖", study: "DURABLE IDENTITY ACROSS SESSIONS",
    headline: ["Agent 记忆的", "文件与身份模型"], intro: "长期记忆不是无限追加的聊天摘要，而是一组可审计的 Markdown 事实文件，加上一张很短的索引；新的会话和不同后端都读取同一个身份目录。",
    proofs: [["1", "个 Agent 身份目录"], ["4", "类持久事实"], ["0", "研究叶子写权限"]], prev: "/session-fork-rewind.html", next: "/harness-recovery.html",
    scenarios: [
      { id: "remember", index: "A", title: "记住偏好", description: "写事实并更新索引", lesson: "事实内容进入独立文件，MEMORY.md 只保留一行指针。下一次启动先读索引，再按任务选择相关事实。", frames: [
        frame("收到 /remember", "chat command", "normal instruction, not hidden magic", { phase: "request", status: "running", primary: 0, secondary: 0, detail: "用户偏好中文技术说明", active: "session-a" }, { type: "memory_instruction", fact: "prefers Chinese technical explanations" }),
        frame("分类事实", "agent", "metadata.type=user", { phase: "classify", status: "running", primary: 1, secondary: 0, detail: "durable user preference", active: "fact" }, { type: "memory_classified", metadata_type: "user" }),
        frame("写 focused file", "filesystem", "preferences.md", { phase: "write", status: "running", primary: 1, secondary: 1, detail: "YAML frontmatter + prose", active: "file" }, { type: "memory_file_written", path: "memory/preferences.md" }),
        frame("更新短索引", "filesystem", "one-line pointer", { phase: "index", status: "completed", primary: 1, secondary: 1, detail: "MEMORY.md → preferences.md", active: "index" }, { type: "memory_index_updated", path: "memory/MEMORY.md" }),
      ] },
      { id: "dedupe", index: "B", title: "去重更新", description: "新事实覆盖旧事实", lesson: "相同主题不会生成 preferences-2.md。Agent 找到原文件并更新，再修正索引摘要，避免记忆随时间腐烂。", frames: [
        frame("发现相关索引", "agent", "preferences.md already exists", { phase: "read", status: "running", primary: 1, secondary: 1, detail: "existing fact found", active: "index" }, { type: "memory_match", path: "preferences.md" }),
        frame("原地更新", "filesystem", "Chinese + concise", { phase: "write", status: "running", primary: 1, secondary: 1, detail: "update, do not append duplicate", active: "file" }, { type: "memory_file_updated", path: "preferences.md" }),
        frame("索引同步", "filesystem", "summary refreshed", { phase: "index", status: "completed", primary: 1, secondary: 1, detail: "deduplicated", active: "index" }, { type: "memory_index_updated", entries: 1 }),
      ] },
      { id: "lifecycle", index: "C", title: "跨后端生存", description: "归档保留，硬删除清除", lesson: "Claude 和 Codex 用不同注入方式指向同一个绝对目录。归档会话不动记忆；硬删除 Agent 才删除身份目录。", frames: [
        frame("Claude 会话启动", "assembly", "path override injected", { phase: "attach", status: "running", primary: 1, secondary: 1, detail: "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE", active: "session-a" }, { type: "memory_attached", backend: "claude" }),
        frame("切换到 Codex", "assembly", "developer instruction names absolute path", { phase: "attach", status: "running", primary: 2, secondary: 1, detail: "same canonical directory", active: "session-b" }, { type: "memory_attached", backend: "codex" }),
        frame("归档旧会话", "session manager", "memory untouched", { phase: "archive", status: "running", primary: 1, secondary: 1, detail: "session-a archived", active: "archive" }, { type: "session_archived", memory_retained: true }),
        frame("硬删除 Agent", "agent manager", "identity directory removed", { phase: "delete", status: "completed", primary: 0, secondary: 0, detail: "memory directory removed", active: "delete" }, { type: "agent_deleted", memory_retained: false }),
      ] },
    ],
    principles: [
      { kicker: "FILES, NOT VIBES", title: "记忆必须可读、可改、可删除", body: "每条主题事实是普通 Markdown，YAML frontmatter 标明名称、说明和类型。人类无需专用数据库工具就能审计。", law: "focused .md + YAML metadata", consequence: "可解释性来自存储格式本身。" },
      { kicker: "SHORT INDEX", title: "启动时读目录，不吞下全部历史", body: "MEMORY.md 只放一行式指针。Agent 先读索引，再按当前任务打开少量相关文件。", law: "index first · selective detail", consequence: "记忆增长不会线性挤占上下文。" },
      { kicker: "DURABILITY FILTER", title: "只记住跨会话仍然有用的事实", body: "user、feedback、project、reference 四类用于长期事实。秘密、一次性状态和本轮临时细节明确禁止进入记忆。", law: "durable + non-secret + actionable", consequence: "记得少一点，身份反而更可靠。" },
      { kicker: "BACKEND NEUTRAL", title: "同一个目录，两种注入方式", body: "Claude 通过官方 memory path override；Codex 通过开发者指令和文件工具。认证目录和原生 session 目录保持原样。", law: "canonical agent memory dir", consequence: "换模型不等于换人格。" },
      { kicker: "SIDE-EFFECT CONTAINMENT", title: "研究叶子没有长期记忆写权限", body: "临时研究叶子 memory_dir=None；只有拥有 Agent 身份的主会话能读写长期记忆。", law: "leaf session → no memory dir", consequence: "未经验证的网页内容不能沉淀成身份事实。" },
    ],
  },
  harness: {
    id: "harness", number: "007", title: "Harness 与故障恢复解剖", study: "ONE RUN ENGINE, MANY RUNTIMES",
    headline: ["统一 Harness 的", "运行与恢复"], intro: "Claude、Codex 和未来运行时都被压缩成 RuntimeProfile 数据；同一套 turn 引擎负责组装、启动、解析、分类和有界恢复。",
    proofs: [["1", "套运行引擎"], ["5", "类故障出口"], ["1", "次工具后恢复上限"]], prev: "/agent-memory.html", next: "/automation-pipeline.html",
    scenarios: [
      { id: "transient", index: "A", title: "瞬时故障恢复", description: "安全时退避重试", lesson: "只有尚未产生用户可见输出、且错误匹配 transient patterns 时，才重放同一 prompt；重试次数有界。", frames: [
        frame("组装 TurnContext", "assembly", "neutral prompt + tools + credential", { phase: "assemble", status: "running", primary: 1, secondary: 0, detail: "backend-neutral context", active: "context" }, { type: "turn_assembled", backend: "codex" }),
        frame("RuntimeProfile 渲染 argv", "profile", "codex exec ...", { phase: "render", status: "running", primary: 1, secondary: 0, detail: "profile data selected", active: "profile" }, { type: "argv_rendered", backend: "codex" }),
        frame("Provider 503", "parser", "no output emitted", { phase: "classify", status: "transient", primary: 1, secondary: 0, detail: "safe_to_retry=true", active: "router" }, { type: "runtime_error", kind: "transient", status: 503 }),
        frame("指数退避", "run engine", "bounded retry #1", { phase: "retry", status: "running", primary: 1, secondary: 1, detail: "same prompt, resume id ignored", active: "retry" }, { type: "turn_retry", attempt: 1 }),
        frame("成功收口", "parser", "result normalized", { phase: "result", status: "completed", primary: 1, secondary: 1, detail: "one neutral result", active: "result" }, { type: "turn_result", ok: true }),
      ] },
      { id: "auth-limit", index: "B", title: "认证与限额", description: "相似提示，不同出口", lesson: "401 直接标记 credential 需要重连；用户套餐限额则持久 park 到 reset_at，再自动续跑。两者绝不共用重试按钮。", frames: [
        frame("解析错误", "profile classifier", "auth signature matched", { phase: "classify", status: "auth_expired", primary: 0, secondary: 0, detail: "credential needs reconnect", active: "auth" }, { type: "runtime_error", kind: "auth", status: 401 }),
        frame("停止，不重试", "run engine", "prevent retry storm", { phase: "stop", status: "blocked", primary: 0, secondary: 0, detail: "user action required", active: "auth" }, { type: "credential_flagged", needs_reconnect: true }),
        frame("另一次命中套餐限额", "limit classifier", "reset time extracted", { phase: "classify", status: "limit_paused", primary: 1, secondary: 0, detail: "resets 21:20 Asia/Shanghai", active: "limit" }, { type: "usage_limit", reset_at: "21:20" }),
        frame("持久停放", "session manager", "prompt retained", { phase: "park", status: "waiting", primary: 1, secondary: 1, detail: "auto resume scheduled", active: "limit" }, { type: "turn_parked", durable: true }),
      ] },
      { id: "watchdog", index: "C", title: "工具后早退与看门狗", description: "恢复与超时分道", lesson: "工具调用后 CLI 意外退出只允许一次 continue 恢复；idle/overall timeout 是终态，不会被误判为可恢复早退。", frames: [
        frame("工具调用完成", "parser", "tool result observed", { phase: "tool", status: "running", primary: 1, secondary: 0, detail: "awaiting assistant continuation", active: "process" }, { type: "tool_result", ok: true }),
        frame("CLI 提前退出", "watcher", "no clean result", { phase: "classify", status: "premature_exit", primary: 1, secondary: 0, detail: "eligible for one recovery", active: "router" }, { type: "premature_exit", after_tool: true }),
        frame("恢复 continue", "run engine", "bounded recovery 1/1", { phase: "recover", status: "running", primary: 1, secondary: 1, detail: "resume with continue", active: "retry" }, { type: "turn_recovered", attempt: 1 }),
        frame("整体时限触发", "overall watchdog", "steady events do not evade cap", { phase: "watchdog", status: "failed", primary: 1, secondary: 1, detail: "terminal timeout", active: "watchdog" }, { type: "turn_timeout", kind: "overall" }),
      ] },
    ],
    principles: [
      { kicker: "PROFILE AS DATA", title: "不要为每个 CLI 复制一个 Harness", body: "binary、argv renderer、stream parser、login、transcript codec、错误模式和能力都放进 RuntimeProfile。运行引擎只消费接口。", law: "one Harness + RuntimeProfile values", consequence: "新增后端变成配置边界，而不是复制控制流。" },
      { kicker: "NEUTRAL CONTEXT", title: "先组装中立 turn，再翻译成 argv", body: "prompt、cwd、resume id、系统提示、模型、工具、MCP、凭证和 memory dir 统一进入 TurnContext。", law: "assembly → profile renderer → subprocess", consequence: "产品功能不依赖某个 CLI 的参数形状。" },
      { kicker: "DISJOINT CLASSIFIERS", title: "认证、限额、瞬时故障必须互斥", body: "认证要求用户重连；套餐限额要求 park；瞬时 provider 故障才可能退避重试。分类顺序防止错误落进宽泛兜底。", law: "auth ≠ limit ≠ transient", consequence: "恢复动作与真实原因一致。" },
      { kicker: "SAFE RETRY", title: "产生输出之后，重放同一 prompt 可能重复副作用", body: "瞬时重试只在安全谓词成立时执行；工具后早退使用 continue，并严格限制一次。", law: "retry iff bounded ∧ side-effect-safe", consequence: "恢复能力不会变成重复执行器。" },
      { kicker: "TWO WATCHDOGS", title: "静默超时和总体上限防不同故障", body: "idle watchdog 捕获无事件卡死；overall cap 即使持续有心跳也会终止。timeout 是明确终态，不参与早退恢复。", law: "idle timer ∥ overall cap", consequence: "既防死锁，也防永不结束的活锁。" },
    ],
  },
  automation: {
    id: "automation", number: "008", title: "调度与通知解剖", study: "TIME-TRIGGERED AGENT EXECUTION",
    headline: ["自动化任务的", "调度与交付"], intro: "一句自然语言先变成可验证 recurrence；时钟触发后，仍存活的原会话会接回任务，否则创建临时会话执行并归档。两条路径都沿正常消息链运行，通知则在结果事务之外并行分发。",
    proofs: [["60s", "最短 interval"], ["1", "次性任务自动删除"], ["N", "个通知器并行隔离"]], prev: "/harness-recovery.html",
    scenarios: [
      { id: "interval", index: "A", title: "确定性 Interval", description: "快路径不调用模型", lesson: "“每 10 分钟 检查构建”符合刚性语法，直接得到 interval recurrence；不为能确定解析的输入支付模型成本。", frames: [
        frame("接收自然句", "schedule API", "每 10 分钟 检查构建", { phase: "parse", status: "running", primary: 10, secondary: 0, detail: "rigid fast path matched", active: "parser" }, { type: "schedule_parse", input: "每 10 分钟 检查构建" }),
        frame("验证 recurrence", "validator", "interval >= 60 seconds", { phase: "validate", status: "running", primary: 600, secondary: 0, detail: "interval_seconds=600", active: "schedule" }, { type: "schedule_validated", kind: "interval", seconds: 600 }),
        frame("到点触发", "APScheduler", "no live origin session", { phase: "trigger", status: "running", primary: 1, secondary: 1, detail: "origin_session_id=null", active: "clock" }, { type: "schedule_fired", schedule_id: "sched-008", origin_session_id: null }),
        frame("正常会话执行", "session manager", "agent identity + memory + connectors", { phase: "run", status: "running", primary: 1, secondary: 1, detail: "same start_message path", active: "session" }, { type: "scheduled_turn_started" }),
        frame("自动归档", "scheduler", "result retained, sidebar kept clean", { phase: "archive", status: "completed", primary: 1, secondary: 1, detail: "session archived", active: "archive" }, { type: "scheduled_session_archived" }),
        frame("通知并行分发", "notifier manager", "webhook + audit sink", { phase: "notify", status: "completed", primary: 2, secondary: 2, detail: "2/2 delivered", active: "notify" }, { type: "notifiers_fired", delivered: 2 }),
      ] },
      { id: "cron", index: "B", title: "自然语言 Cron", description: "模型解析后严格校验", lesson: "复杂表达才进入一次性结构化解析。输出必须是 once/cron/interval 之一，timezone 必须是 IANA 名称，否则回退 UTC。", frames: [
        frame("复杂自然语言", "schedule API", "工作日上海时间 9 点", { phase: "parse", status: "running", primary: 0, secondary: 0, detail: "fast path miss", active: "parser" }, { type: "schedule_parse", fast_path: false }),
        frame("一次性模型解析", "oneshot harness", "structured output only", { phase: "model", status: "running", primary: 1, secondary: 0, detail: "cron 0 9 * * 1-5", active: "model" }, { type: "schedule_model_parse", kind: "cron" }),
        frame("时区规范化", "validator", "Asia/Shanghai accepted", { phase: "validate", status: "running", primary: 1, secondary: 1, detail: "IANA timezone valid", active: "schedule" }, { type: "timezone_normalized", timezone: "Asia/Shanghai" }),
        frame("计划持久化", "database", "next_run_at calculated", { phase: "persist", status: "completed", primary: 1, secondary: 1, detail: "weekday cron ready", active: "schedule" }, { type: "schedule_created" }),
      ] },
      { id: "partial", index: "C", title: "一次性 + 局部通知失败", description: "一次执行，互不连坐", lesson: "one-shot 在触发后删除；通知器用 gather 并行，某个 webhook 抛错只记录自身失败，不会让其他目标或任务结果回滚。", frames: [
        frame("one-shot 到点", "APScheduler", "run_at reached", { phase: "trigger", status: "running", primary: 1, secondary: 0, detail: "fresh session created", active: "clock" }, { type: "once_fired" }),
        frame("会话完成并归档", "session manager", "temporary result durable", { phase: "archive", status: "running", primary: 1, secondary: 1, detail: "scheduled session archived", active: "archive" }, { type: "scheduled_session_archived" }),
        frame("删除计划", "scheduler finally", "consumed even when execution fails", { phase: "delete", status: "running", primary: 0, secondary: 1, detail: "one-shot row removed", active: "schedule" }, { type: "once_deleted" }),
        frame("并行通知", "notifier manager", "webhook A ok; webhook B timeout", { phase: "notify", status: "partial", primary: 2, secondary: 1, detail: "1 delivered · 1 isolated failure", active: "notify" }, { type: "notifiers_fired", delivered: 1, failed: 1 }),
        frame("任务仍成功", "notifier manager", "delivery failure is not turn failure", { phase: "done", status: "completed", primary: 1, secondary: 1, detail: "automation completed", active: "done" }, { type: "automation_completed" }),
      ] },
    ],
    principles: [
      { kicker: "DETERMINISTIC FIRST", title: "刚性语法能解析，就不要调用模型", body: "interval 快路径先匹配并验证最小 60 秒。只有真正自然语言表达才进入一次性 harness。", law: "fast parse → validate → persist", consequence: "降低成本，也减少不可重复的解析差异。" },
      { kicker: "STRUCTURED RECURRENCE", title: "模型输出不是计划，校验后的结构才是", body: "once 必须有 run_at；cron 必须有表达式和时区；interval 必须有秒数。未知时区规范化为 UTC。", law: "once | cron | interval", consequence: "调度器永远不直接消费自由文本。" },
      { kicker: "DELIVERY MODE", title: "原会话存活就排队，否则创建临时会话", body: "有 live origin_session_id 时，调度器直接调用该会话的 start_message，并排在当前 turn 之后；原会话不存在时才创建 origin=schedule 的临时会话。", law: "live origin → enqueue · missing origin → fresh session", consequence: "自动化复用普通 turn 语义，但不会假装两条生命周期完全相同。" },
      { kicker: "LIFECYCLE HYGIENE", title: "计划与会话各自收口，不能混成一个事务", body: "last_run_at 始终更新；one-shot 在外层 finally 删除，因此执行失败也不会重复触发。只有 scheduler 新建的临时会话在结束后自动归档，原会话路径保持原生命周期。", law: "one-shot → delete in finally · temporary session → archive", consequence: "日历意图只消费一次，临时执行可追溯又不淹没侧栏。" },
      { kicker: "FAILURE ISOLATION", title: "通知目标之间不能形成故障链", body: "所有启用 notifier 并行 fire，每个实现自行捕获异常。一个超时 webhook 不会阻断其他目标，也不会改变 Agent turn 的结果。", law: "gather(all) + per-target catch", consequence: "通知是结果的观察者，不是结果的事务参与者。" },
    ],
  },
};
