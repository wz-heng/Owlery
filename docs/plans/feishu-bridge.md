# Feishu Bridge — 用飞书彻底替换 Telegram

状态:方案定稿,待执行。
决策记录:用户不用 Telegram,选择**彻底替换**(删 telegram,不并存)。

## 1. 目标

手机上通过飞书驱动 Owlery 的 agent:单聊或群里 @机器人 发消息,
走现有的 chat→agent 绑定 + sticky session 机制;收到 agent 回复、
错误、工具审批卡片(带 允许/拒绝 按钮)、`/sessions` 会话选择卡片。
Telegram bridge 及其全部测试、配置、文档引用一并删除。

## 2. 动机

- 用户日常在飞书,不在 Telegram;Telegram 还要翻墙,家用 Mac 上
  proxy 已经造成过一串事故(见 delegation-502 复盘)。飞书直连。
- bridge 抽象层(`Bridge` 基类、`BridgeManager`、`bridge_mappings`
  表的 `platform` 列)本来就是平台无关的,替换只动叶子节点。

## 3. 入站通道选型(本方案唯一的架构决策)

飞书事件订阅有两种投递方式,与 Telegram 的 long-polling 不同,
必须二选一或并存:

- **方案 A(推荐):长连接为主 + webhook 为辅,双入口喂同一个处理器。**
  长连接(WebSocket,官方 `lark-oapi` SDK 的 `ws.Client`)是飞书为
  "无公网地址的自部署工具"设计的通道——正是家用 Mac 场景,等价于
  Telegram 的 long-polling。webhook 路由(FastAPI 下挂
  `/api/feishu/events`,处理 URL challenge + verification token +
  可选 AES 解密)是飞书官方支持的另一种订阅方式,同时天然成为 e2e
  的入站路径:fake 服务器无法伪造 protobuf 的 ws 协议,但可以直接
  POST webhook。两种入口收到的事件 JSON 同构,汇入同一个
  `handle_event_payload`,没有重复逻辑。
- 方案 B(仅 webhook):代码最少,但真实使用必须有公网 URL(依赖
  Cloudflare tunnel 常开)。家用场景硬伤,否。
- 方案 C(仅长连接):真实使用没问题,但 e2e 入站没有任何网络路径
  可走,bridge e2e 名存实亡,只剩单测级注入。违反本仓"测试是一等
  公民"的现实,否。

**出站不用 SDK**:发消息走裸 httpx 调 REST
(`/im/v1/messages`,tenant_access_token 自缓存自刷新),
base URL 可配置——这保持了与 Telegram 实现相同的可 fake 性
(e2e fake 服务器只需要模拟 token 端点 + 发消息端点),也让
Lark 国际版(`open.larksuite.com`)只是换个 URL 的事。
`lark-oapi` 依赖仅用于 ws 客户端。

## 4. 设计要点

**配置**(替换 `telegram_*` 三项):
- `feishu_app_id` / `feishu_app_secret` — 自建应用凭证,二者齐备
  即启用 bridge(等价于原 `telegram_bot_token` 的开关语义)。
- `feishu_verification_token` / `feishu_encrypt_key` — webhook 校验
  与解密;不配 encrypt_key 则只收明文事件。
- `feishu_allowed_chat_ids` — open_chat_id 白名单,**空 = 不限制**,
  与 Telegram 语义一致(自建应用本就只在自己租户内可见,默认放开
  合理)。
- `feishu_api_base_url` — 默认 `https://open.feishu.cn/open-apis`,
  e2e 指向 fake 服务器,Lark 用户指国际版。

**消息渲染**:飞书 `text` 类型不渲染 markdown,agent 回复里的代码
块会裸奔。assistant 文本、tool_use/tool_result 一律走 interactive
卡片的 markdown 元素(`lark_md`);纯状态行(Done/Error/切换确认)
可用 text。执行时确认单条消息实际长度上限并接到
`max_message_length`(TextBuffer 分片依赖它,别沿用 Telegram 的
4096 魔数)。飞书有按 chat 的限频(约 5 QPS/群),`flush_delay`
据此调,执行时以官方文档为准。

**交互按钮**(Telegram inline keyboard 的对应物):
- 工具审批:卡片两按钮,value 带 `{action, tool_use_id}`;回传事件
  (`card.action.trigger`)从长连接/webhook 同一通道进来,处理后更
  新原卡片使按钮失效(对应 Telegram 的 `_clear_keyboard`)。
- `/sessions`:每会话一按钮的卡片,沿用 `SESSION_LIST_LIMIT=30`。

**入站事件**:订阅 `im.message.receive_v1` + 卡片回传。单聊直收;
群里只认 @机器人 的消息。`chat_id` 用事件里的 open_chat_id,
`platform` 字符串为 `"feishu"`,`BridgeManager` 及以上一行不改。

**Telegram 拆除清单**:`server/bridges/telegram.py`、config 三项、
`main.py` 挂载段、`tests/test_bridge_telegram.py`、
`web/e2e/telegram-bridge.spec.ts` + `fake-telegram-server.mjs`、
`playwright.bridge.config.ts` 的 telegram 指向、README / CLAUDE.md /
`parked_turns.py` 注释里的 Telegram 引用。DB 迁移加一步:删除
`bridge_mappings` 中 `platform='telegram'` 的行(功能已移除,留着
是永远匹配不到 bridge 的死数据)。`base.py` 里两处 Telegram 措辞
(TextBuffer docstring、`max_message_length` 默认值注释)顺手改掉。

**测试**:单测重写为 `test_bridge_feishu.py`(mock 出站 httpx +
直接注入事件 dict,覆盖白名单、卡片回传、token 刷新);e2e 换
`fake-feishu-server.mjs`(记录出站调用)+ 入站走真 webhook 路由,
条数对齐原 6 条的覆盖面。**借重建之机修掉 CLAUDE.md 记录的 bridge
e2e 隔离欠账**:新 `playwright.bridge.config.ts` 必须钉死私有端口
和自己的 `OWLERY_AUTH_TOKEN`,不得再出现"401 于别的 token / 领养
别人正跑着的 :8000"两种失败模式。

**用户侧前置**(代码做不了,上线前用户自己在
open.feishu.cn 完成):创建自建应用 → 开机器人能力 → 申请
`im:message` 收发权限 → 事件订阅启用**长连接**方式并勾
`im.message.receive_v1` + 卡片回传 → 发布 → 把机器人拉进目标会话,
app_id/secret 填进 Owlery 配置。

## 5. 不做

- 不做 Telegram/飞书并存或任何"可切换平台"的配置开关——用户已拍板
  彻底替换;`Bridge` 抽象本身就是未来再加平台的位置。
- 不做富卡片花活(进度条、折叠、图片回传):首版对齐 Telegram 现有
  能力集,一条不多。
- 不做飞书侧多租户/商店应用发布:自建应用、单租户、自用。
- 不做语音/文件/图片入站:只收文本消息,其余类型回一句不支持。
- 不迁移既有 telegram 聊天绑定到飞书:平台不同 chat_id 无对应关系,
  没有可迁之物。
