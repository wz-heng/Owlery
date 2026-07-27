import type { LaterSpecimenId } from "./laterSpecimens";

export interface PrincipleDepth {
  question: string;
  mechanism: string[];
  failure: string;
  tradeoff: string;
  code: string[];
}

export interface PrincipleOverview {
  thesis: string;
  boundary: string;
  codeRoots: string[];
}

export const LATER_PRINCIPLE_OVERVIEWS: Record<LaterSpecimenId, PrincipleOverview> = {
  research: {
    thesis: "可信研究的核心不是搜索更多，而是让未经验证的主张无法进入最终报告。",
    boundary: "先看预算如何封顶，再看叶子的权限隔离和反驳投票；最后核对作业终态与报告交付为什么必须分开。",
    codeRoots: ["server/research/orchestrator.py", "server/research/leaf.py", "server/research/manager.py"],
  },
  fork: {
    thesis: "分叉只复制对话因果；已经发生在外部世界的副作用必须另行审计。",
    boundary: "先确定分叉点和 Fork/Rewind 语义，再审计副作用；文件还原是最后一道、可拒绝的补偿操作。",
    codeRoots: ["server/session_manager.py", "server/fork_helpers.py", "server/harness/fork.py"],
  },
  memory: {
    thesis: "长期记忆不是聊天记录仓库，而是 Agent 身份下少量、可审计、按需读取的事实。",
    boundary: "重点不是 Markdown 本身，而是谁能写、何时读、跨后端如何指向同一目录，以及哪些临时内容绝不能沉淀。",
    codeRoots: ["server/agent_memory.py", "server/harness/assembly.py", "server/session_manager.py"],
  },
  harness: {
    thesis: "统一运行引擎的价值，是把后端差异压进 Profile，同时让失败原因仍保持互斥。",
    boundary: "先看中立 TurnContext 如何被渲染，再看 auth、limit、transient、premature exit 与 timeout 为什么走不同出口。",
    codeRoots: ["server/harness/profile.py", "server/harness/run.py", "server/session_manager.py"],
  },
  automation: {
    thesis: "调度器只负责可靠地产生一轮普通 Agent 工作；它不拥有第二套执行语义。",
    boundary: "解析结果必须先结构化校验；触发时优先排入仍存活的原会话，否则才创建临时会话。通知始终在结果事务之外。",
    codeRoots: ["server/schedule_ai.py", "server/scheduler.py", "server/notifiers/manager.py"],
  },
};

export const LATER_PRINCIPLE_DEPTH: Record<LaterSpecimenId, PrincipleDepth[]> = {
  research: [
    {
      question: "开放问题如何变成一个能估算成本、时延和最坏并发量的作业？",
      mechanism: [
        "scope reasoning leaf 最多返回 5 个角度；parse_angles 对格式和数量再次收口。",
        "每角度最多 6 条 finding，纯函数 dedup_and_rank 再把候选压到 12 条 claim。",
        "单作业内用 concurrency=4 的 semaphore；Manager 外层另有限制并发作业数和整作业硬超时。",
      ],
      failure: "角度解析不稳定会退化为有限输入，而不是无限扩张；任一预算只在一处生效会留下另一层进程风暴。",
      tradeoff: "硬上限换来可预测性，也意味着长尾角度可能被截断；产品应把它呈现为覆盖边界，而不是暗示穷尽。",
      code: ["ResearchLimits", "run_research() / _bounded()", "dedup_and_rank()"],
    },
    {
      question: "并行搜索为什么不会继承主 Agent 的文件、连接器和长期记忆权限？",
      mechanism: [
        "每个 web leaf 在 research/<job>/cwd 空目录启动，不使用用户仓库作为 cwd。",
        "RunConfig 显式设置 mcp_servers=[]、connectors=[]、memory_dir=None，并启用 web_research 限权模式。",
        "叶子 finally 调用 run.stop()；超时记录为 LeafResult.error，取消则继续抛出以回收整棵任务。",
      ],
      failure: "如果把 None 当成‘不传配置’，默认 MCP 可能重新注入；这里必须用空数组表达‘明确没有服务器’。",
      tradeoff: "隔离让叶子无法利用项目私有上下文；需要仓库事实的问题应走普通 Agent 任务，而不是网页研究叶子。",
      code: ["server/research/leaf.py: run_web_leaf", "RunConfig(web_research=True)", "server/research/manager.py: scratch cwd"],
    },
    {
      question: "‘独立验证’究竟改变了哪一段数据流，而不只是一句免责声明？",
      mechanism: [
        "search findings 先去重排序，验证器只接收有限 claim、来源 URL 和原问题。",
        "默认每条 claim 发出 2 个验证叶子；kill_threshold=2，只有两票都明确反驳才淘汰。",
        "synthesize_prompt 只接收 survivors，已淘汰 claim 从合成输入中物理消失。",
      ],
      failure: "验证叶子报错不会被算作反驳票，因此系统偏向保留而非误杀；这也意味着网络故障可能降低过滤强度。",
      tradeoff: "更严格的淘汰阈值减少误杀，却增加漏过弱主张的概率；票数和阈值应随风险等级配置。",
      code: ["run_research(): _verify", "parse_verdict()", "synthesize_prompt(question, survivors)"],
    },
    {
      question: "为什么进度完成了，报告仍可能没有进入会话？",
      mechanism: [
        "progress 只更新 research_jobs.phase 并广播轻量 research_progress，供 ResearchCard 展示。",
        "报告先写 Markdown 文件、作业标记 completed，再广播 research_completed。",
        "最后通过 start_message 注入普通 turn；injection_status 单独记录 delivered/failed，并用它保证幂等。",
      ],
      failure: "父会话消失或繁忙会导致交付失败，但不能反写成‘研究失败’；作业完成与消费交付是两个可独立修复的状态。",
      tradeoff: "双状态增加数据库和恢复逻辑，却避免瞬时 UI 事件冒充可持久引用的研究成果。",
      code: ["ResearchManager._run_job", "ResearchManager._inject_report", "research_jobs.injection_status"],
    },
    {
      question: "取消、超时、零证据和普通叶子失败分别应该留下什么结果？",
      mechanism: [
        "cancel 先把数据库状态写成 cancelled 并广播，再 task.cancel()；异常路径的 finalize 是幂等的。",
        "单叶子失败降级为 error 结果，其他角度可以继续；整作业 timeout 才统一收口为 failed。",
        "报告只基于 survivors；成本与 token 统计只覆盖后端实际报告的叶子，明确是下界而非完整账单。",
      ],
      failure: "当前 orchestrator 仍会调用 synthesis，即使 survivors 为空；界面不得宣称‘零证据必然不合成’，只能如实展示 verified=0 的报告边界。",
      tradeoff: "容忍局部叶子失败提高可用性，但最终报告必须披露覆盖缺口，否则降级会被误读为完整研究。",
      code: ["ResearchManager.cancel / _finalize_failed", "LeafResult.error", "ResearchReport.cost / usage"],
    },
  ],
  fork: [
    {
      question: "为什么分叉点必须是服务端消息序号，而不是当前屏幕上的第 N 条？",
      mechanism: [
        "fork preview 从持久化 messages 中按 seq 解析人类输入；分页、重连和渲染分组不会改变序号。",
        "自动注入的 bg-task-result、agent-reply 等虽然可能以 user role 保存，但按来源语义排除。",
        "选中的 seq 同时成为历史截断点、副作用扫描起点和 lineage 元数据。",
      ],
      failure: "只看 role 或数组下标会把机器回流误当成人类提示；重连后数组位置变化还会把分支切到另一轮。",
      tradeoff: "稳定身份要求服务端为所有持久事件分配顺序，并让前端保留来源类型，数据模型会比纯聊天数组更严格。",
      code: ["server/session_manager.py: fork preview", "server/fork_helpers.py: classify_side_effects", "messages.seq"],
    },
    {
      question: "Fork 与 Rewind 在会话生命周期上到底差在哪里？",
      mechanism: [
        "Fork 复制当前可恢复上下文，父会话继续存在，父子通过 lineage 并列。",
        "Rewind 从指定人类提示重新开始，并把旧分支视为被替代路径；它仍创建普通子会话而非原地改写历史。",
        "创建过程是 saga：数据库会话、工作目录、后端转录物任一步失败都走 cleanup。",
      ],
      failure: "把 Rewind 实现为删除后半段消息会破坏审计；把 Fork 当成 replay 又可能重复工具副作用。",
      tradeoff: "永不改写历史会多出会话与存储，但换来可追溯 lineage 和失败后的可恢复性。",
      code: ["SessionManager.fork_session", "server/harness/fork.py", "RuntimeProfile.fork_copy / fork_cleanup"],
    },
    {
      question: "系统如何知道分叉点之后哪些事情已经改变了外部世界？",
      mechanism: [
        "扫描 seq >= fork point 的 tool_use/tool_result，并把文件、后台任务和其他工具分箱。",
        "Edit/Write 可精确取路径；Bash 只对常见写入语法做保守目标提取；连接器默认按不可逆处理。",
        "后台任务通过 tool_use → tool_result → bg_tasks 联接，补出 task_id、命令和当前终态。",
      ],
      failure: "任意 shell、远端 API 和工具内部副作用无法被完全推断，因此清单是 best-effort 账本，不是事务日志。",
      tradeoff: "保守分类会产生误报，但漏报会让用户误以为世界已回滚；这里宁可多披露，不能伪造可逆性。",
      code: ["classify_side_effects()", "_bash_write_targets()", "_READONLY_TOOLS / _INTERNAL_MCP_PREFIXES"],
    },
    {
      question: "文件还原为什么必须先通过四项预检，而不是点击后尽力执行？",
      mechanism: [
        "确认工作目录仍是 git 仓库，且分叉点记录的是 clean working tree。",
        "当前 HEAD 必须仍等于 fork_head；否则提交历史已移动，旧锚点失效。",
        "当前 dirty paths 只能来自 Agent 已触碰集合；随后用带路径范围的 git stash -u 保存并还原。",
      ],
      failure: "发现任何用户额外修改就拒绝 revert，但分叉本身仍可创建；还原失败记录 durable fork_revert_record，不回滚整个 fork。",
      tradeoff: "严格拒绝会让一部分可安全手工恢复的情况无法一键完成，但优先避免覆盖用户未提交工作。",
      code: ["safe_revert_preflight()", "safe_revert_files()", "sessions.fork_revert_record"],
    },
    {
      question: "不同 CLI 的原生会话格式如何被限制在适配层，而不污染 SessionManager？",
      mechanism: [
        "RuntimeProfile 声明 can_fork、fork_prepare、fork_copy 和 fork_cleanup 协作者。",
        "完整 Fork 优先复制 Claude transcript 或 Codex rollout 到新 resume id；没有原生材料时才降级 history replay。",
        "Rewind 用截断历史包装首轮 prompt，后续仍在子会话自己的原生 id 上继续。",
      ],
      failure: "复制到一半的转录物必须按 fork_id 清理；否则数据库失败会留下能被误恢复的孤儿文件。",
      tradeoff: "原生复制保真但绑定 CLI 文件格式；历史 replay 可移植，却增加首轮 token 并需要明确标注旧工具结果不可重放。",
      code: ["server/harness/claude_code.py: _fork_copy", "server/harness/codex.py: _fork_copy", "wrap_for_fork_replay()"],
    },
  ],
  memory: [
    {
      question: "为什么选择普通文件，而不是把记忆藏进向量库或会话数据库？",
      mechanism: [
        "每个 Agent 在 agents/<id>/memory 下拥有一个规范目录，主题事实拆成独立 Markdown。",
        "YAML frontmatter 保存 name、description、metadata.type；正文保存可由人直接修改的事实。",
        "目录由 ensure_agent_dirs 幂等创建；硬删除 Agent 才递归删除整个身份树。",
      ],
      failure: "文件格式可审计但没有数据库事务；半写入、重复主题和错误 frontmatter 需要 Agent 写入纪律与测试约束。",
      tradeoff: "放弃强查询能力换来可移植、可 diff、可手工修复；这适合少量身份事实，不适合海量检索语料。",
      code: ["server/agent_memory.py", "agent_memory_dir()", "remove_agent_dir()"],
    },
    {
      question: "记忆增长后，为什么不会在每轮把所有事实重新塞进上下文？",
      mechanism: [
        "MEMORY.md 只保存标题、文件链接和一行用途说明，充当目录而非汇总全文。",
        "任务开始先读索引，再根据当前问题打开少量 focused files；未命中的主题不进入上下文。",
        "同主题事实更新原文件和索引行，而不是持续追加近义副本。",
      ],
      failure: "索引摘要过宽会导致无关文件被频繁读取；索引漏更新则让仍存在的事实变成不可发现孤儿。",
      tradeoff: "渐进披露节省 token，但相关性选择由模型执行，不具备传统检索器的确定召回率。",
      code: ["server/harness/assembly.py: render_memory_blurb", "memory/MEMORY.md contract", "focused <slug>.md"],
    },
    {
      question: "什么值得跨会话保存，谁负责阻止秘密和临时状态进入记忆？",
      mechanism: [
        "开发者指令只允许 user、feedback、project、reference 四类稳定事实。",
        "写入前要求 durable、non-secret、actionable，并优先更新或删除已失真的旧文件。",
        "这是一条模型执行的治理协议；文件的可读性让用户和审计者能发现违规。",
      ],
      failure: "当前没有强制 schema validator 或秘密扫描器，错误写入仍可能发生；不能把提示词政策描述成数据库级安全保证。",
      tradeoff: "模型自治能理解语义与上下文，却弱于硬权限；敏感部署需要额外 DLP、审阅或只读策略。",
      code: ["render_memory_blurb()", "metadata.type policy", "human-readable Markdown audit"],
    },
    {
      question: "Claude 与 Codex 为什么能共享身份记忆，却不共享认证和原生会话目录？",
      mechanism: [
        "SessionManager 从 session.agent_id 解析唯一 memory_dir，并放入中立 RunConfig/TurnContext。",
        "Claude 通过 CLAUDE_COWORK_MEMORY_PATH_OVERRIDE 指向该目录，不改 CLAUDE_CONFIG_DIR。",
        "Codex 保持 CODEX_HOME 不变，只在 system/developer instructions 中获得路径和文件操作协议。",
      ],
      failure: "若把整个配置目录重定向，会同时搬走 auth 与 resume 数据；切换后端时就会表现为重新登录或丢会话。",
      tradeoff: "统一事实目录不等于统一后端记忆实现；两边行为仍需契约测试防止注入方式漂移。",
      code: ["SessionManager._run_backend: memory_dir", "claude_code.py: memory env", "RuntimeProfile.injects_memory_prompt"],
    },
    {
      question: "为什么研究叶子和无身份临时运行不能读写长期记忆？",
      mechanism: [
        "只有绑定 agent_id 的主会话才解析 canonical memory_dir。",
        "研究叶子构造 RunConfig(memory_dir=None)，同时清空 MCP 与 connectors。",
        "叶子使用最小研究 persona 和独立 scratch cwd，其输出必须回到验证管线后才可能影响主会话。",
      ],
      failure: "如果叶子继承 memory_dir，网页提示注入可把未经验证内容写成长期身份事实，污染会跨所有后续会话扩散。",
      tradeoff: "隔离牺牲了研究叶子对用户偏好的直接访问；必要偏好应由编排器显式写进问题，而不是开放整个记忆目录。",
      code: ["server/research/leaf.py", "RunConfig(memory_dir=None)", "server/research/manager.py: scratch cwd"],
    },
  ],
  harness: [
    {
      question: "新增一个 CLI 后端时，哪些差异必须是数据，哪些控制流必须保持唯一？",
      mechanism: [
        "RuntimeProfile 集中声明 binary、argv renderer、event parser、oneshot、认证、限额、Web 与 fork 能力。",
        "Harness 暴露统一 start/stream/stop；SessionManager 只消费规范事件和能力，不判断 CLI 名称。",
        "profile module 负责把原生输出归一为 HarnessEvent，并保留后端独有解析证据。",
      ],
      failure: "把 backend if/else 放进运行循环会让重试、取消和记账出现多份分叉逻辑，后端行为逐步漂移。",
      tradeoff: "Profile 接口会变宽，但差异集中可测试；真正不共形的能力应显式标记不可用，不能硬塞进最小公分母。",
      code: ["server/harness/profile.py: RuntimeProfile", "server/harness/harness.py", "server/harness/registry.py"],
    },
    {
      question: "一轮对话在变成 Claude/Codex argv 之前，如何保持后端中立？",
      mechanism: [
        "HarnessRun 先把 cwd 绝对化，并选择 MCP、connectors、callback env 和记忆注入。",
        "prompt、resume_id、system_prompt、model、tools、credential、memory_dir 组成 TurnContext。",
        "最后一步才调用 profile.build_turn_argv，把同一语义翻译成不同命令和环境变量。",
      ],
      failure: "相对 cwd 会被 MCP 子进程二次解析；在 renderer 里重新拼系统提示则会造成不同后端拿到不同安全约束。",
      tradeoff: "中立上下文降低耦合，但新增跨后端能力需要先扩展 DTO，再逐个实现 renderer，不能偷走捷径。",
      code: ["HarnessRun._make_context()", "TurnContext", "assembly.select_mcp_servers / compose_system_prompt"],
    },
    {
      question: "同样出现 401、429 或连接中断，为什么恢复动作不能共用一个重试分支？",
      mechanism: [
        "auth pattern 表示 credential 失效，标记 needs_reconnect 并停止。",
        "用户套餐限额由结构化 stream state 判定，持久 park 到 reset_at；缺失时间可单独查 rollout。",
        "transient pattern 只覆盖 provider 过载、5xx、连接故障，并在前两类都未命中后考虑退避重试。",
      ],
      failure: "HTTP 429 文本同时可能表示服务器节流和用户额度；只匹配字符串会把长时间限额误做秒级重试风暴。",
      tradeoff: "互斥分类需要维护真实 CLI 样本和顺序测试，但它让用户动作、自动恢复和成本语义保持正确。",
      code: ["TurnFailure / UsageLimitHit", "Harness.classify_usage_limit", "SessionManager._run_backend classifiers"],
    },
    {
      question: "什么时候可以重放 prompt，什么时候只能沿原进程 continue？",
      mechanism: [
        "transient retry 先检查是否已有用户可见输出或工具副作用，并受 attempts/backoff 上限约束。",
        "Claude 工具后 premature exit 不重放 prompt，而以 continue 恢复原 resume 上下文，最多一次。",
        "timeout、auth、usage limit 都是其他终态或停放状态，不进入 premature-exit 兜底。",
      ],
      failure: "在工具已经执行后重放同一 prompt，可能重复写文件、发消息或启动任务；恢复成功反而制造双重副作用。",
      tradeoff: "保守停止会放弃部分可恢复 turn，但比静默重复外部动作更诚实；未来需要幂等工具协议才能放宽。",
      code: ["RuntimeProfile.premature_exit_recovery", "session_manager.py: recovery budget", "HarnessRun.stop()"],
    },
    {
      question: "为什么需要 idle 与 overall 两个看门狗？",
      mechanism: [
        "idle timer 以最近事件时间为基准，捕获无输出、无心跳的卡死进程。",
        "overall cap 以 turn started 为基准，即使持续吐心跳也不能无限运行。",
        "任一触发后停止后端并产生明确 timeout kind；配置为 <=0 才表示关闭对应限制。",
      ],
      failure: "只有 idle 会漏掉活锁，只有 overall 会把正常但安静的长工具误杀；两个时钟解决的是不同故障。",
      tradeoff: "固定阈值无法适配所有任务；桥接和调度场景更需要上限，而交互任务可能需要按工具类型调整预算。",
      code: ["SessionManager._turn_watchdog", "turn_idle_timeout_seconds", "turn_max_seconds"],
    },
  ],
  automation: [
    {
      question: "哪些时间表达式应由确定性代码解析，什么时候才值得调用模型？",
      mechanism: [
        "显式 interval 先走 fast path，单位换算后校验最短 60 秒并拆出任务 prompt。",
        "只有快路径不匹配的自然语言才进入一次性 tool-free harness，并要求 JSON 结构输出。",
        "解析结果统一生成 ScheduleParseResult，后续 API 不区分它来自正则还是模型。",
      ],
      failure: "模型若用于所有输入，会把相同命令解析成不同计划；快路径若过宽，又可能误吃本应由语义模型理解的句子。",
      tradeoff: "确定性优先牺牲部分自然语言灵活性，换来低成本、可测试和可复现；语法边界必须向用户明确。",
      code: ["server/schedule_ai.py: parse_schedule_text", "explicit interval parser", "run_oneshot fallback"],
    },
    {
      question: "为什么模型返回的 JSON 还不能直接交给 APScheduler？",
      mechanism: [
        "recurrence 是 once、cron、interval 的判别联合，每种分支只允许自己的必填字段。",
        "interval 验证 seconds；once 解析带时区 run_at；cron 由 CronTrigger.from_crontab 预验证。",
        "展示 label 与执行字段分开保存，旧记录缺 label 时可从结构化字段重建。",
      ],
      failure: "未知时区、过去时间、非法 cron 或缺字段必须在持久化前拒绝；调度器不能消费自由文本再猜一次。",
      tradeoff: "严格 schema 会拒绝部分模糊但人类可理解的表达，代价远小于悄悄在错误时间执行。",
      code: ["ScheduleParseResult", "recurrence_label_for()", "CronTrigger.from_crontab"],
    },
    {
      question: "到点后任务究竟进入旧会话，还是新建临时会话？",
      mechanism: [
        "如果 schedule 记录了 origin_session_id 且会话仍存活，调用 start_message 排到该会话当前 turn 之后。",
        "若原会话不存在或已归档，才 create_session(origin='schedule')，继承 Agent 配置执行普通 send_message。",
        "两条路径都走 SessionManager 的标准消息入口，不维护第二套模型执行器。",
      ],
      failure: "直接向忙碌会话并发写入会破坏消息顺序；无条件新建则让用户在原对话里找不到周期任务结果。",
      tradeoff: "双模式改善连续性，但同一计划的结果位置会随原会话生命周期变化；管理页必须显示 origin 和最近运行位置。",
      code: ["server/scheduler.py: ScheduleRunner._fire", "start_message(origin_session_id)", "create_session(origin='schedule')"],
    },
    {
      question: "一次触发之后，计划、运行记录和临时会话各自如何收口？",
      mechanism: [
        "成功执行后更新 last_run_at；跳过或异常分别记录日志，不伪造成功时间。",
        "fresh schedule session 在 finally 中尝试自动归档；回到 live origin 的路径不归档原会话。",
        "one-shot 的 run_at 记录在最外层 finally 删除，无论执行成功、跳过还是失败都不再次触发。",
      ],
      failure: "把 one-shot 删除放在成功分支会导致失败后意外重跑；无条件归档则可能把用户仍在使用的 origin session 隐藏。",
      tradeoff: "失败的一次性任务默认不自动重试，避免未知副作用重复；需要重试时应创建显式的新计划。",
      code: ["ScheduleRunner._fire try/finally", "auto_archive_scheduled_session", "db.update_schedule(last_run_at)"],
    },
    {
      question: "为什么通知失败不能改变 Agent turn 的成功状态？",
      mechanism: [
        "NotifierManager 把规范化 NotifierEvent 并行发送给所有已启用目标。",
        "每个 _safe_send 自己捕获并记录异常，因此 gather 等到所有目标，但不会向调用者抛单目标错误。",
        "session idle、limit park 等触发点把通知视为观察者；数据库中的 turn 终态先于外部 webhook。",
      ],
      failure: "当前隔离是 best-effort 日志，不等于持久投递队列；服务退出或持续超时可能丢通知，也没有自动重放保证。",
      tradeoff: "轻量并行 fan-out 不阻塞任务语义，但牺牲 exactly-once 与可重试交付；高可靠通知应升级为 outbox/queue。",
      code: ["server/notifiers/manager.py: fire / _safe_send", "NotifierEvent", "SessionManager._fire_idle_notifier"],
    },
  ],
};
