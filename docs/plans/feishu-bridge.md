# Feishu Bridge — 用飞书彻底替换 Telegram

状态:方案 v2,经 Aberforth 评审后修订,待执行。
决策记录:用户不用 Telegram,选择**彻底替换**(删 telegram,不并存)。
评审记录:v1 的四个 P0(transport 未闭合、裸 ws.Client 生命周期、
webhook 默认放行、审批只带 tool_use_id)全部成立,已吸收;本版为定稿。

## 1. 目标

手机上通过飞书驱动 Owlery 的 agent:单聊或群里 @机器人 发消息,
走现有的 chat→agent 绑定 + sticky session 机制;收到 agent 回复、
错误、工具审批卡片(带 允许/拒绝 按钮)、`/sessions` 会话选择卡片。
Telegram bridge 及其全部测试、配置、文档引用一并删除。

## 2. 动机

- 用户日常在飞书,不在 Telegram;Telegram 还要翻墙,家用 Mac 上
  proxy 已经造成过一串事故(见 delegation-502 复盘)。飞书直连。
- bridge 抽象层(`Bridge` 基类、`BridgeManager`、`bridge_mappings`
  表的 `platform` 列)本来就是平台无关的。注意:替换**不是**纯叶子
  节点——审批路由的正确性修复(§4.4)要动 `BridgeManager` 接口。

## 3. 架构决策

### 3.1 传输方式:显式 transport 配置

飞书的「事件订阅」和「回调订阅」在开放平台后台**各自**选择投递
方式(长连接或 webhook),这不是代码层能悄悄并存的东西。配置:

- `feishu_transport = "ws" | "webhook"`,生产默认 `ws`(长连接,
  官方为无公网自部署场景提供,正是家用 Mac),e2e 固定 `webhook`。
- 两种 transport 的事件汇入同一个业务处理器;webhook 路由在
  `ws` 模式下也不注册(见 §4.2 fail-closed)。
- 这不是"平台开关"式的过度设计——它是飞书官方本来就要求的选择,
  代码只是如实映射。

### 3.2 SDK:采用官方 `lark-channel-sdk`,strict 安全模式

v1 的「lark-oapi 只用 ws.Client + 出站裸 httpx」被否,理由成立:
`ws.Client` 的 `start()` 阻塞、依赖模块级 event loop、无可靠公开
关闭接口,塞进 FastAPI lifespan 会把启动/重连/shutdown 全变成对
私有实现的依赖。官方已把机器人能力拆成独立的 `lark-channel-sdk`
(长连接/webhook 生命周期、事件去重、token 管理、出站、卡片回调、
FastAPI webhook 适配一体),`lark_oapi.channel` 已进迁移窗口。

- 依赖 `lark-channel-sdk`,**写死具体版本号**(执行时取当时最新
  稳定版;评审时仓库显示 1.0.0,勿凭记忆写)。
- `SecurityConfig(mode="strict")` 显式启用——SDK 默认 compat 不够。
- Owlery 侧只写薄适配层:SDK 事件 → `BridgeManager.handle_incoming`
  / 卡片回调 → 审批处理;卡片 JSON 仍自己构造。
- **执行前置 spike(半天内)**:验证两件事——(a) SDK 出站 base
  URL / domain 可指向 e2e fake 服务器;(b) 在 FastAPI lifespan 里
  能干净 start/stop。两条都过才继续;任一不过,回退方案为半 SDK
  形态,但必须:官方 `EventDispatcherHandler` 统一处理解密/签名/
  challenge、WS 跑专用线程、有界队列投递回 FastAPI loop、写
  shutdown/reconnect 测试。回退是逃生舱,不是默认路径。

### 3.3 域名:统一 `feishu_domain`

v1 的 `feishu_api_base_url` 只管 REST,WS 仍会连死
`open.feishu.cn`,并不真正支持 Lark。改为统一的
`feishu_domain`(默认 `https://open.feishu.cn`,国际版
`https://open.larksuite.com`,e2e 指 fake 服务器),REST 路径在其
上追加 `/open-apis`,WS/webhook 相关端点同源派生。

## 4. 设计要点

### 4.1 配置(替换 `telegram_*` 三项)

- `feishu_app_id` / `feishu_app_secret` — 二者齐备才启用;**只配一
  个 = 启动失败并报明确错误**,不静默禁用。
- `feishu_transport` — 见 §3.1。
- `feishu_verification_token` / `feishu_encrypt_key` — webhook 模式
  必填 verification token(见 §4.2)。
- `feishu_domain` — 见 §3.3。
- 授权白名单,**默认 fail-closed**(见 §4.2)。

### 4.2 安全:默认拒绝,不照搬 Telegram 的宽松

Owlery 背后是能执行工具的本机 agent,uvicorn 还常以 `0.0.0.0` 监
听、可能挂 tunnel。「自建应用只在自己租户所以空白名单合理」不成
立——租户里可能有别人,群成员都能点审批按钮。

- webhook 模式:未配置 verification token → 路由直接不注册
  (404),绝不裸收 POST;strict 模式下签名校验、时间戳窗口由 SDK
  承担,任务书不自己发明「token + AES 就算完」。
- `feishu_allowed_open_ids` — 发送者与卡片 operator 的 open_id 白
  名单,**空 = 拒绝一切**(fail-closed),配置文档明确写出如何取
  自己的 open_id。
- 群聊:同时校验 chat_id(若配了 chat 白名单)与 operator open_id。
- 按 `header.event_id` 去重——飞书事件会重推,重复入站等于重复启动
  agent turn。卡片操作另做一次性消费(见 §4.4 nonce)。

### 4.3 消息渲染与出站

- 飞书 `text` 不渲染 markdown:agent 正文、tool_use/tool_result 走
  interactive 卡片 `lark_md`;纯状态行(Done/Error/切换确认)用 text。
- 分片上限:卡片限制是**序列化后 ~30KB**,不是字符数;中文与 JSON
  转义都吃额度。`max_message_length` 按 UTF-8 序列化结果估算留出
  裕量,不沿用 Telegram 的 4096 字符魔数。
- 出站错误处理:飞书限流常以 HTTP 400 + 响应体业务 `code` 表示,
  不能只看 429;按 code 分类,带抖动退避;tenant_access_token 失效
  → 清缓存重试一次,再失败即报错。
- 限频(约 5 QPS/chat)决定 `flush_delay`,执行时以官方文档为准。
- **代理(执行期裁决,Albus 批准)**:SDK 出站基于 `requests`,默认
  honor 环境代理(`trust_env=True`)。`domain` 为 loopback(e2e fake)时,
  bridge 显式传 `TransportConfig(trust_env_proxy=False)`——loopback 目标不
  走 env 代理,防本机 Clash 劫持回环请求(与 `server/harness/assembly.py`
  对 `127.0.0.1` 回调强制 `trust_env=False` 的既有约定一致);e2e harness
  另行 export `no_proxy=127.0.0.1,localhost` 作双保险。生产真实域名保持
  `trust_env_proxy=None`(honor env,即用户到公网的正常出口)。SDK 的
  `http_executor` 配置项经 spike 确认为 vestigial(定义了但 SDK 内部未
  引用),e2e 出站拦截一律靠 `domain=loopback`,不依赖它。
- **出站不再走 SDK `channel.send`(执行期发现,偏离 §3.2 主路径)**:集成后
  实测发现一个致命交互——**agent turn 会在事件循环上 spawn 子进程(claude
  CLI + 4 个 MCP server),这会破坏「紧随其后从同一进程发出的出站 HTTP 请求」
  的异步 socket I/O**:该请求(无论跑在 SDK 后台 loop 还是主 loop 的裸 httpx)
  永久挂起。诊断证据:turn 之前的出站(命令回执)秒回;turn 之后的出站(agent
  回复 flush)永挂;命令类 e2e 全过、消息往返/dedup 挂,病征完全吻合。老
  Telegram bridge 因全程跑在主 loop 上、且回执是内联发送(子进程活动前)而从未
  触发。更深一层:子进程还破坏了事件循环的**跨线程唤醒**——`asyncio.to_thread`
  也不行(同步活儿在线程里跑完了,但 `await` 永不被唤醒)。**修法**:出站改为
  「本 bridge 自持 REST + **直接阻塞** `requests`」——`_sync_request` 在事件循环上
  **直接同步调用** `requests`(阻塞 socket 不经事件循环 selector,免疫于该破坏;
  循环只为一次快速往返短暂阻塞,通常远小于 1s),自管 tenant_access_token
  (缓存 + 失效刷新重试)、按业务 code 分类退避(§4.3 语义由本 bridge 实现,
  不再委托 SDK)。**入站仍走 SDK**(webhook
  dispatcher 解密/验签/challenge + 事件路由),入站跨线程唤醒用一个 50ms
  keepalive tick 兜底。这是 §3.2 预留的「回退到半 SDK 形态」的出站落地。

### 4.4 卡片交互(审批 / 会话切换)

- 卡片回调**可以**走长连接(官方 WS 示例注册
  `register_p2_card_action_trigger`),但开放平台后台要把「事件订
  阅」「回调订阅」**分别**设为长连接——用户前置清单里是两步,不是
  一步。
- 回调必须 **3 秒内响应且失败不重推**:handler 立即返回(空响应或
  置灰卡片),业务处理(审批/切换)异步做;需要更新原卡片时在回调
  响应之后走 REST PATCH。
- **审批按钮 value 必须携带完整身份**:`{action, session_id,
  tool_use_id, nonce}`。现 `handle_tool_decision` 只按聊天的 sticky
  session 找目标——发卡后用户 `/switch` 再点旧卡,审批会投向错误会
  话,原会话永久挂起。这是 Telegram 实现的存量 bug,本次一并修:
  处理时校验 session 属于该聊天绑定的 agent、tool_use_id 确在待审
  批态、operator 已授权、nonce 未消费。`BridgeManager` 接口相应调
  整——不为「叶子纯洁」牺牲审批正确性。
- `/sessions` 会话卡片沿用 `SESSION_LIST_LIMIT=30`,按钮 value 同
  样带 session_id + nonce。

### 4.5 入站范围

- 订阅 `im.message.receive_v1` + 卡片回调。单聊直收;群里只认
  @机器人 的消息。
- `chat_id` 用 open_chat_id,`platform = "feishu"`。
- **话题群(topic/thread)首版明确拒绝**:检测到 thread 消息回一句
  不支持,不让不同 thread 悄悄共享一个 sticky session。把
  `thread_id` 纳入 conversation key 属于后续有真实需求再做的事。
- 非文本消息(语音/图片/文件)回一句不支持。

## 5. Telegram 拆除清单(完整版)

- `server/bridges/telegram.py`;`base.py` 里两处 Telegram 措辞
  (TextBuffer docstring、`max_message_length` 默认值注释)。
- config:`telegram_bot_token` / `telegram_allowed_chat_ids` /
  `telegram_api_base_url`;`main.py` 挂载段。
- 测试:`tests/test_bridge_telegram.py` 整删;
  `test_bridge_database.py`、`test_delegations.py`、
  `test_session_fork.py`、`test_session_manager.py` 中共 ~27 处
  `"telegram"` 平台字面量改为 `"feishu"` 或中性值;
  `test_migration_backfill.py` 改为**断言** telegram 行被迁移删除。
- e2e:`telegram-bridge.spec.ts`、`fake-telegram-server.mjs`、
  `playwright.bridge.config.ts` 重写;`playwright.config.ts` 的
  `testIgnore` 与 `OWLERY_TELEGRAM_BOT_TOKEN` env 两处。
- 文档:README(≥5 处)、CLAUDE.md(4 处)、
  `docs/architecture.md`(15 处)、`parked_turns.py` 注释。
- DB 迁移:删除 `bridge_mappings` 中 `platform='telegram'` 的行
  (功能已移除,留着是永远匹配不到 bridge 的死数据)。

## 6. 测试

### 6.1 bridge e2e 重建 = 还清全部隔离欠账

现 `playwright.bridge.config.ts` 的问题远不止 CLAUDE.md 记的
token/端口两条:`reuseExistingServer: true`、无临时 DB/home/agents
隔离、无 fake CLI——理论上会碰真实数据库、删真实 session、调真实模
型。新配置必须复用主 e2e 的整套护栏:

- 独立临时 DB、home、agents 目录;
- fake CLI 上 PATH + codex tripwire;
- `reuseExistingServer: false`;
- 私有端口 + 自己的 `OWLERY_AUTH_TOKEN`;
- `OWLERY_LEGACY_HOME_DIR=""`。

### 6.2 覆盖目标(按风险,不按条数)

「对齐原 6 条」是错误目标。新 e2e 至少覆盖:消息往返、真实卡片点
击切换会话、审批 允许/拒绝 一次性消费(重复点击无效)、签名/
verification 不合法被拒、重复 event_id 去重、未授权 operator 被
拒。单测 `test_bridge_feishu.py`:白名单语义(fail-closed)、卡片
value 校验矩阵(session 不属 agent / tool_use 不在待审批态 / nonce
已消费)、token 刷新与失效重试、出站业务错误码退避、transport 配
置矩阵(含只配一半凭证启动失败)。SDK spike 若走回退路径,补
shutdown/reconnect 测试。

## 7. 用户侧前置(代码做不了)

open.feishu.cn 创建自建应用 → 开机器人能力 → 申请权限:
`im:message.p2p_msg:readonly`(单聊收)、群聊 @机器人 消息接收、
`im:message:send_as_bot`(发)→ **「事件订阅」与「回调订阅」分别
设为长连接方式**,勾 `im.message.receive_v1` 与卡片回调 → 发布 →
把机器人拉进目标会话 → app_id/secret 填进 Owlery 配置,并把自己的
open_id 加进 `feishu_allowed_open_ids`。

## 8. 不做

- 不做 Telegram/飞书并存;`Bridge` 抽象本身就是未来再加平台的位置。
- 不做富卡片花活(进度条、折叠、图片回传):对齐现有能力集。
- 不做飞书侧多租户/商店应用发布:自建应用、单租户、自用。
- 不做语音/文件/图片入站;不做话题群 thread 支持(首版明确拒绝)。
- 不迁移既有 telegram 聊天绑定:平台不同 chat_id 无对应关系。
