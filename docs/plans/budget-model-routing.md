# 预算与模型路由(Budget & Model Routing)

状态:设计定稿(Albus,2026-07-28)。实现拆分为 Task Board 子任务 A/B/C,
复核 D,验收 E。本文是总纲;各子任务书自包含,细节冲突时以本文为准。

## 1. 目标

1. **预算**:给 Claude 支出装一个自设的、前置的闸门——按日/周/月给
   全局和单个 agent 设 USD 限额,turn 开跑前检查,软阈值警告、
   硬阈值拦停。
2. **模型路由**:把「用哪个模型」从 agent 级死配置解放成可在
   session 创建时显式指定——委派方、Task Board 任务、用户开新会话
   都能按工作性质选模型,并在 API 层校验 model↔backend 兼容性。

## 2. 动机

- 07-09 / 07-15 两轮 token 消耗诊断:马拉松 session 的 cache 重读与
  冷重写唤醒税单日可烧 $50+。`usage-tracking` 交付的 `turn_usage`
  记账明确定位为「后续限额功能的数据地基」(usage-tracking.md §0),
  地基铺了,楼一直没盖。预算是从「事后看账」到「事前设闸」的一步。
- 模型目前只能配在 `agents.model`(server/database.py 的 agents 表),
  `SessionManager.create_session` 无 model 参数,委派与 Task Board
  子任务全部只能继承受派 agent 的配置。想让同一个 agent 用便宜模型
  跑机械任务、用旗舰模型跑难任务,今天做不到。
- 存量 bug「codex 会话带 claude 模型名→静默空 turn」有两层病因:
  backend/model 不联动、空结果不报错。模型路由的校验层顺手根治
  这两层——这不是附带福利,是路由功能自身的正确性要求。

## 3. 设计要点 — 预算

### 3.1 数据与配置

- 新表 `budgets`:`id`、`scope`(`'global'` | `'agent'`)、`agent_id`
  (scope=agent 时非空,unique;global 行为 NULL)、`window`
  (`'daily'` | `'weekly'` | `'monthly'`,自然日历窗口,周从周一起)、
  `limit_usd REAL`、`soft_pct REAL` 默认 0.8、`enabled` 布尔。
  全局与 per-agent 预算可并存,谁先触线谁生效(取更紧的约束)。
- 支出口径:窗口内 `turn_usage.cost` 求和(含 `origin='research'`),
  Codex turn 的 cost 为 NULL,计 0。**预算只计 USD、只治 Claude**
  ——痛点在此,见 §10。
- 窗口边界按服务器本地时区算出后转 UTC 与 `created_at` 比较。
  注意存量陷阱:`created_at` 是带 `T` 的 ISO 串,和
  `datetime('now')` 的空格格式直接比较会失灵,过滤要用
  `strftime('%Y-%m-%dT%H:%M:%S', ...)` 族的格式对齐。

### 3.2 执行语义

- **检查点唯一**:session_manager 的 turn 启动路径(harness run
  发起之前)加一次前置检查。中途不打断——已在飞的 turn 跑完不管。
- **软阈值**(spent ≥ limit × soft_pct):turn 照跑,向该 session
  注入一次性警告(同一窗口内不重复轰炸),前端展示水位。
- **硬阈值**(spent ≥ limit):turn 快速失败,返回结构化错误
  (含 scope、窗口、限额、已花),session 本身保持健康可续。
  **一切来源的 turn 一视同仁**——交互、schedule、delegation、
  Task Board、bridge 全拦;只拦交互 turn 的预算是装饰品,因为
  烧钱大头是自主运行的 turn。委派方收到 agent-error,Task Board
  run 按既有失败语义处理,Feishu 收到说明消息。
- 解除方式只有用户改配置(调高/禁用),没有代码层「特批」通道。
- 与 limit-auto-resume 的关系:互不相干的两层。那是上游订阅限流
  的事后 park/resume,这是自设额度的事前拦截;预算检查在前,
  park 逻辑不动。

### 3.3 API 与前端

- `budgets` CRUD:`/api/budgets`(list/create/patch/delete)。
- 状态查询:`GET /api/budgets/status` → 每条启用预算的
  `{scope, agent_id, window, limit_usd, spent_usd}`,水位由前端算。
- 前端:UsageDialog 增预算区(全局配置 + 状态水位条);
  AgentSettings 增该 agent 的预算配置;软阈值在会话界面出横幅;
  硬拦截的错误消息可读、含下一步指引(改预算/换 agent)。

## 4. 设计要点 — 模型路由

### 4.1 机制:session 级覆盖 + 单点解析

- `sessions` 表加 `model TEXT NULL` 列(沿用 database.py 既有
  migration 模式)。
- 解析优先级:**session.model > agent.model > backend 默认**。
  解析收敛为单一函数(如 `resolve_model(session, agent)`),替换
  session_manager 中现有的 `agent.get("model")` 取值点——这个单点
  就是未来任何路由策略的接缝,策略变化不再散落。
- `SessionManager.create_session` 加 `model: str | None = None`
  参数,REST 建会话接口透传,前端新建会话时可选填(自由文本 +
  按 backend 给建议列表,与 AgentSettings 现有输入风格一致,
  不做强校验的封闭下拉)。

### 4.2 路由入口:委派与 Task Board

- 委派:`ask_agent` 工具加可选 `model` 参数,穿透
  `server/delegations.py` 的 `create_session` 调用。委派方(通常
  是另一个 agent 或用户指令)最清楚子任务的难度档位,这是路由的
  第一现场。
- Task Board:`tasks` 表加 `model TEXT NULL`,`create`/`specify`
  接受该字段,`task_board/manager.py` 建 worker session 时透传。
  用例:机械型任务(批量迁移、跑测试修红)派低档模型。
- Feishu bridge 不加模型选择入口(见 §10)。

### 4.3 兼容性校验与空 turn 报错(根治存量 bug)

- **跨家族黑名单校验**:API 层(REST、ask_agent、Task Board)拒绝
  明显错配——codex backend 配 `claude-*`,claude-code backend 配
  `gpt-*`/`o*`/`codex-*`,422 报错。**不做白名单**:CLI 接受任意
  模型串且新模型频出,封闭列表会把合法新值挡在门外;只拦确定错
  的,不认证确定对的。校验同样作用于既有的 `agents.model` 写入口
  (AgentSettings 的 PATCH)。
- **空结果报错**:codex harness 解析层发现 turn 结束却无任何
  assistant 输出也无错误事件时,合成一个显式错误事件,不再静默
  空 turn。这补上错配 bug 的第二层。

### 4.4 预算与路由的交点

只有一处:硬拦截的错误文案提示用户可选项(调预算、换 agent/
backend)。**没有自动改道**——预算爆了不会静默换便宜模型,理由
见 §10 第一条。

## 5. 实现拆分

| 任务 | 内容 | 依赖 |
|---|---|---|
| A 模型路由后端 | §4 全部(sessions.model、resolve_model、create_session/REST/ask_agent/Task Board 透传、校验、空 turn 报错)+ 测试 | — |
| B 预算后端 | §3 数据/执行/API + 测试 | A(避免 session_manager 并行改动冲突;逻辑上亦可独立) |
| C 前端 | §3.3 + §4.1 前端部分 + 测试 | A、B |
| D 代码复核 | Snape 复核 A/B/C 三个交付分支 | C |
| E 最终验收 | Albus 按本文逐条验收 | D |

分支协作纪律:板子 delivery 模式为「留分支不自动并」,每个子任务
的 worktree 从 main 冻结基线。**后序任务开工第一步**:用
`mcp__tasks__show(task_id=前置任务id)` 查前置交付分支名,若尚未
并入 main,先 `git fetch origin <branch> && git merge` 进自己的
工作分支再动工。

## 6. 验收清单(任务 E 用)

1. 给测试 agent 设 daily $0.01 预算,跑一个 turn 后再发起 → 硬拦,
   错误结构化、session 仍可用;调高预算后恢复。
2. 委派与 Task Board 来源的 turn 同样被拦。
3. ask_agent 带 model 建委派 → 子 session 用指定模型(argv 可证);
   Task Board 任务带 model 同理。
4. codex backend + `claude-*` 模型在三个入口均 422。
5. 四道测试门全绿(pytest / vitest / tsc / e2e),新增行为有测试
   覆盖。

## 7. 不做清单

- **不做预算触发的自动降级/自动改道**。07-09 已否:消耗大头是
  cache 重读,由上下文长度决定,换便宜模型省不了大头;且静默降
  质比可见的停更伤信任。可见地停,让用户决定。
- **不做 per-turn 模型覆盖**。session 粒度已覆盖真实用例(开会话
  时就知道任务性质);turn 级要动 UI 和 JSONL 语义,无对应需求。
- **不做 token 计量预算**(Codex)。Codex 订阅无边际成本记账
  (cost=NULL),真实支出痛点全在 Claude;等 Codex 有成本口径
  再议。
- **不做规则引擎式智能路由**(任务分类→自动选模型)。显式指定
  已覆盖需求;`resolve_model` 单点即为未来接缝,现在建引擎是
  投机。
- **不做 mid-turn 中断**、**不做支出预测**(只看已发生支出,不估
  在飞 turn 的成本)、**不做图表/历史回填**(usage-tracking 已
  defer,维持)。
- **不做 Feishu 端模型选择**:聊天场景无此需求,入口越少校验面
  越小。
