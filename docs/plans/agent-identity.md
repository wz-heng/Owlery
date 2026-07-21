# Agent Identity — 退役 Octo 与火漆印头像

一份任务书,两个动作,一个主题:agent 的身份。第一,退役「Octo」——
移除「受保护的默认 agent」这个概念,让它变成一个普通 agent,可归档、
可删除。第二,agent 头像从 emoji 换成火漆印字标——round-3 Seal &
Letterhead 语法的 round-4 修订,`<Seal>` 成为全应用唯一的头像原语。

执行风格提醒:本文写「要什么、为什么、边界在哪」,不写实现步骤。
文中列出的文件行号是勘察时的锚点,以实际代码为准。

## 1. 动机

**Octo 是 Octopus 时代的遗产。** 名字本身就是旧的(应用已更名
Owlery),它由迁移逻辑自动创建(`server/database.py` §2,约 683 行:
无 `is_system=1` 的 agent 则 INSERT 'Octo'),并被
`server/agent_manager.py` 的两道 guard 保护为不可删、不可归档。这个
「必须永远存在一个不可删的默认 agent」的保证,设计初衷是让全新用户
开箱即聊;但在一个已有自建 agent 阵容的实例上,这个保证没有受益人,
只剩一个删不掉的占位符钉在侧栏第一位(`ORDER BY is_system DESC`)。

**emoji 头像与形状语法割裂。** 聊天气泡里 agent 的身份已经是火漆印
+ monogram(round-3),但侧栏 AgentList、AgentSettings、
SchedulesDialog、ArchivedSessionsDialog、UsageDialog、ChatView 头部
这六处 render site 还在用 emoji(`web/src/lib/agentAvatar.ts`)。
两套身份系统并存;且 emoji 的字形随平台漂移,视觉上无法收编进
黄铜印章的品牌世界——这是「丑」的根源,不是挑哪个 emoji 的问题。

## 2. Part A — 退役 Octo

### 目标状态

不存在「系统 agent」概念。所有 agent 一律平等:有 session 的先归档
(历史保留),空的可直接删除——既有规则,不新增。用户可以把 Octo
归档掉,它不再复活。

### 设计要点

- **迁移播种改为「空表播种」**:`database.py` 迁移 §2 的条件从
  「无 is_system agent」改为「agents 表完全为空」,播种的 agent 是
  一个**普通** agent(`is_system=0` 语义,可删),名字不再叫 Octo
  ——建议 `Owl`,与应用同一世界观。全新安装的开箱即聊体验保住了;
  已有 agent 的老库(用户的实例)什么都不会被播种,Octo 从此只是
  一个普通 agent。
- **guard 移除**:`agent_manager.py` 的 archive/delete 两道
  `is_system` guard 删掉。「有 session 只能归档不能删」的既有规则
  继续兜底,防止 FK CASCADE 误删历史。
- **`is_system` 彻底退场**:不是「留着列但废弃」。代码层全部引用
  移除(`database.py` 的排序改 `created_at`、`agents.py` router、
  `contracts.ts` 的字段、前端两处 fallback),DDL 里的列用
  SQLite `DROP COLUMN`(3.35+)在迁移里移除。半退役是给未来留债。
- **前端 fallback**:`App.tsx:138` 与 `AgentList.tsx:79` 的
  `find(a => a.is_system) ?? agents[0]` 收敛为「第一个 agent」
  (created_at 序)。空 agent 列表的分支既有(new-agent draft),
  不需要新逻辑。
- **bridge 首次绑定**:`bridges/manager.py::_ensure_bound` 目前绑
  `get_system_agent()`。改为绑第一个未归档 agent;一个 agent 都
  没有时回复一条「尚无 agent 可用」的礼貌消息而非静默失败。既有
  绑定(chat → 已归档 agent)的行为要在实现时验证并给出明确结果
  ——要么继续可用,要么提示重绑,不允许静默黑洞。
- **测试**:现有 pytest 断言了 guard 与自动重建行为,需同步改写。
  新增断言:空表播种、非空表不播种、Octo 可归档。

### Octo 名下的历史

迁移曾把无主 session 全部回填给 Octo,它名下大概率有 session,
所以用户的实际操作路径是**归档**(历史进 ArchivedSessions,agent
memory 目录保留)。这是期望行为,不要为「彻底删除 Octo」开绿灯
绕过 session 检查。

## 3. Part B — 火漆印头像

### 目标状态

一个 agent 在全应用的视觉身份 = 一枚火漆印:monogram 印文 + 确定性
分配的蜡色。六处 emoji render site 全部换成 `<Seal>`。emoji 头像
机制退役。

### 设计要点

- **印文**:`monogram(name)` 既有函数,取名字首个字母/数字大写;
  取不出时压 owl mark(`<Seal mark />`)——规则已在
  `web/src/components/ui/seal.tsx` 写死,直接复用,不发明第二套。
- **蜡色盘**:monogram 解决不了同首字母撞脸(Dobby/Dumbledore),
  蜡色解决。新增一组 `--wax-*` token:一个 6 色左右的精选蜡盘
  (黄铜、墨、李子、青墨……真实火漆的颜色范围),**红色仍归
  destructive 独占,不入盘**。由 agent id hash 确定性分配——同一
  agent 永远同色,不提供逐 agent 选色器(色由系统分配才能保证
  全局和谐;这是决定,不是妥协)。色值需过 dataviz 对比度验证,
  白色 monogram 在每种蜡上可读。
- **尺寸**:六处场景跨度大(侧栏行 ~20px 到 ChatView 头部)。现有
  scale(sheet 26 / dialog 32 / chip 16)之外补 `--seal-avatar`
  (约 20px)供列表行用。列表行里的 seal 不 straddle(无边可骑),
  内联摆放,同 chip 的先例。
- **语法修订(round-4)**:现行语法有两条与本设计冲突的明文——
  「emoji 留在侧栏,那里没有东西被封缄」和「每面一印,控件只配
  dot」。修订为:**agent 身份即是印**,凡出现 agent 身份处即出现
  其火漆印;session 行、控件继续用 dot 回声,ornament budget 依然
  成立(一行一个身份,一个身份一枚印)。修订写进 `tokens.css` 的
  语法注释块与 `messenger-form.md`,不许代码与文档各说各话。
- **avatar 字段退役**:`agents.avatar` 列、`agentAvatar.ts`、
  AgentSettings 里的 emoji picker 全部移除(列同样 DROP COLUMN)。
  用户对某个 agent 想要的个性,由名字(印文)与系统分配的蜡色
  承载。理由:保留 emoji 覆盖会立刻破坏「印是压出来的,不是贴上
  去的」——彩色 emoji 压不进单色蜡,一旦允许覆盖,六处 render
  site 各自需要 emoji 分支,两套系统又回来了。**这是本方案最硬的
  一刀,用户已确认**(见 §5)。
- **测试**:seal.test.tsx 扩展蜡色分配的确定性断言;e2e agents
  rail 的选择器若依赖 emoji 文本需改;UsageDialog 里
  `${avatar} ${name}` 的字符串拼接改为纯名字 + seal。

## 4. 不做

- 不做图片 / URL 头像上传(`avatar` 列注释里的 URL 可能性一并
  埋葬)。
- 不做逐 agent 蜡色选择器。
- 不做 emoji 印文覆盖,也不做单字符印文覆盖(§5 已拍板)。
- 不动生产实例;`delegations.py` 注释里的 "Octo asks Vera" 只是
  示例人名,不用改。
- 不在本轮捎带 codex 模型错配等悬而未决的 blocker。

## 5. 已拍板

**avatar/emoji 机制彻底退役**(用户 2026-07-21 确认)。现存 agent
人为设置的 emoji 头像随之丢弃,不做单字符印文覆盖的退路。§3 的
「先与用户确认后再动工」条件已满足,照此执行。

## 6. 验收

四道门全绿(pytest / vitest / tsc / e2e)之外:全新 DB 首启播种一个
可删的 `Owl`;老库不播种;Octo 可归档且重启不复活;六处 render site
无一处残留 emoji 头像;明暗两主题下每种蜡色上的 monogram 可读。
