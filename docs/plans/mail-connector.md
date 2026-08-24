# 通用邮箱连接器(IMAP/SMTP,静态授权码)

状态:已定案,待派工。
分支:`docs/mail-connector`。

## 1. 目标

新增一个内置连接器 kind `mail`:用户在浏览器里填「邮箱地址 + 授权码
(+ 服务器预设)」即可把一个 IMAP/SMTP 邮箱接给 agent,agent 获得
读信、搜信、发信能力。首要目标是 QQ 邮箱,但 kind 本身通用——
163、Outlook(应用密码)等一切「授权码式」邮箱走同一条路。

## 2. 动机

- 用户明确需要 agent 双向操作 QQ 邮箱(读 + 发)。
- QQ 个人邮箱没有 OAuth REST API,只有 IMAP/SMTP + 授权码;现有
  connectors 框架(`server/connectors/`)全部假设 OAuth
  (`ConnectorBase.oauth` 必填,安装流 = authorize → exchange →
  refresh),custom connector 路径同样是 OAuth2-only。所以这不是
  「再写一个 gmail.py」,而是给框架凿一条**静态凭据安装路径**。
- 这条路径凿通后是框架级资产:今后任何 API-key/静态密码型服务
  (非 OAuth)都能沿用,不是 QQ 一家的一次性投入。

## 3. 方案取舍(已裁决)

- **通用 `mail` kind + 前端预设** ✅ —— QQ/163/Outlook 只是安装表单里
  的 host/port 预填项,DB 和后端只有一个 kind。
- QQ 专属 kind ❌ —— 静态凭据路径的工程量两个方案都省不掉,专属
  kind 只省了表单里几个可编辑字段,却让下一个邮箱变成复制粘贴。
- 等 QQ 出 OAuth 邮件 API ❌ —— 不存在这个东西,个人邮箱永远是
  授权码模式。

## 4. 设计要点

### 4.1 框架:静态凭据安装路径

- `ConnectorBase` 引入 auth 模式分叉(如 `auth_mode: 'oauth' |
  'static'`,具体形态由执行者定):static 连接器声明一个凭据表单
  schema(字段名、是否 secret、预设列表),不携带 OAuth provider。
- 新安装端点(如 `POST /api/connectors/{kind}/install-static`):
  **先验证后落库**——真实执行一次 IMAP LOGIN + SMTP AUTH,任一失败
  则整个安装失败并把服务器原话带回给前端;成功才写入
  `connector_installations` + `connector_installation_secrets`
  (两张表现成,不需要新表)。
- `external_account_id` = 邮箱地址,复用现有的
  `(kind, external_account_id)` 唯一索引做去重;`allows_multiple
  = True`(用户可接多个邮箱)。
- health check = IMAP 登录探活;失败置 `needs_reconnect`,复用
  OAuth 连接器的重连 UX(用户重填授权码)。
- 秘密向 MCP server 的传递**复用现有 token 回调通道**
  (`mcp_servers/connectors/_shared.py` → 127.0.0.1 token 端点,
  `trust_env=False`):对 static 连接器,该端点返回的「token」就是
  授权码。授权码绝不进 MCP config/env/日志。

### 4.2 MCP server(`server/mcp_servers/connectors/mail.py`)

工具面(动词短、snake_case,受 60 字符工具名上限约束):

- 读:近期邮件列表、按条件搜信、读单封信(正文渲染成纯文本,
  HTML 剥离;有大小上限,超限截断并说明;附件列元数据,按需
  下载到会话工作目录)。
- 发:`send`(to/cc/subject/正文纯文本,可选本地文件路径作附件)、
  回复某封信。纯文本发送,不做富文本撰写。
- system-prompt blurb 必须声明:发信是对外动作,发送前先向用户
  确认——与 github 连接器写操作的警示同款。

### 4.3 前端

- catalog picker 新增条目;安装走静态表单对话框(预设下拉:QQ /
  163 / Outlook / 自定义,选预设自动填 host/port,始终可改),
  不走 OAuth 跳转。
- 表单里放一行帮助文案 + 链接:授权码不是 QQ 密码,在 QQ 邮箱
  「设置 → 账户 → 开启 IMAP/SMTP」处生成。这是用户最容易栽的坑,
  值得一行 UI 文案。
- 已装实例的展示/改名/删除/needs_reconnect 全部复用现有 UI 路径。

### 4.4 已知暗坑(执行者必读)

- **QQ/163 的 IMAP 要求登录后发 RFC 2971 `ID` 命令**,否则后续命令
  被拒(163 报 "Unsafe Login" 最典型)。Python `imaplib` 不内置,
  需手动发。这是国内邮箱接入最经典的翻车点。
- 授权码 ≠ 账户密码;用密码登录 IMAP 必失败,报错信息要能引导
  用户去生成授权码。
- 端口形态:QQ IMAP 993(SSL)、SMTP 465(SSL)优先;587 STARTTLS
  作为预设外的可选项。
- 中文邮件解析:RFC 2047 头编码 + GB18030/GBK 正文是常态,解析
  统一走 `email` stdlib 的 `policy=default`,并准备 GB18030 解码
  兜底。
- IMAP `SEARCH` 对非 ASCII 关键词的 CHARSET 支持在 QQ 上不可靠;
  搜索工具的契约不要过度承诺服务器端全文搜索,必要时客户端过滤
  兜底,并在工具描述里说清语义。
- 代理无碍:IMAP/SMTP 是裸 TCP/SSL,不走 `http_proxy`,Clash 机上
  不需要 no_proxy 处理。

### 4.5 测试

按仓库四门纪律。要点:

- 后端单测用本地 fake IMAP/SMTP 服务器(SMTP 可用 aiosmtpd;IMAP
  用脚本化 fake),覆盖:安装验证失败不落库、ID 命令已发、
  needs_reconnect 置位、中文解析、发信 MIME 正确性。
- e2e:静态安装没有 OAuth 舞步,比 gmail 好测——表单填入指向
  fake 服务器的自定义预设,走完安装→agent 勾选→会话内工具可见。
- 不在 CI 里碰真 QQ;真邮箱冒烟是交付后的手动步骤。

## 5. 不做

- **不做 OAuth 接 QQ**:不存在对应 API。
- **不做删信/移信/标记等破坏性动词**:误删不可逆,当前没有需求;
  工具面 = 读 + 搜 + 发,句号。
- **不做本地邮件同步/索引/IDLE 推送**:每次工具调用现查现取。
  「新邮件提醒」类需求属于 schedules(定时轮询)的组合用法,不进
  连接器本体。
- **不做富文本/HTML 撰写**:发送纯文本(附件除外)。
- **不做 OAuth 型邮箱(Gmail)并入此 kind**:Gmail 连接器已存在
  且走 REST API,不回头合并。
