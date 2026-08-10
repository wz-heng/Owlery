# 侧栏信息层级 —— 按「使用温度」分层

> **Status:** 设计任务书,实现待派。
>
> 起因是看板任务《侧栏的信息层级明确》:「Agents 是主路径,而日程、应用、
> 连接器、凭据是设置型能力;现在虽有分隔线,但功能继续增长后会显得拥挤。
> 建议把后四项收进可折叠的 Workspace/Integrations 区,或仅在配置过时显示。」
>
> 这份文档否掉原议题的一个隐含前提,给出替代结构。细节实现留给执行者。

## 1. 现状(先把事实钉死)

侧栏 `web/src/App.tsx` 的 `<nav>` 当前结构:

```
Owlery 品牌头
─ nav(可滚动)
  AgentList            ← 主路径:agents + 其会话,内联展开
  Task Board(按钮)
  ── hairline 分隔线 ──   ← 已经存在,把下面四项标记为 infrastructure
  SCHEDULES   (行 → SchedulesDialog)
  APPLICATIONS(行 → onAdd,当前是 () => {} 空操作)
  CONNECTORS  (行 → 自带 Dialog)
  CREDENTIALS (行 → 自带 Dialog)
账户页脚 AccountDropdown(设置 / agent 设置 / 归档会话 / 用量 / 登出)
```

两个必须先认清的事实,它们直接改变结论:

1. **后四项已经是单行 header,不是内联展开的长列表。** 每项约 32px,四项
   合计 ~128px,且已有分隔线隔开。所以「现在很拥挤」并不成立——原议题真正
   担心的是**未来生长**(更多连接器类型、更多凭据、将来的 MCP server /
   计费 / 集成…),届时这个扁平列表会摊开。这是一道**前瞻性 IA 决策**,
   不是救火。

2. **账户页脚 `AccountDropdown` 已经是"设置之家"**:用量、agent 设置、
   归档会话、登出都在那儿。也就是说 Owlery 已有一个成型的设置入口,不需要
   为「设置型能力」另造一个家。

## 2. 否掉原议题的前提:四项不是同温度

原议题把「日程 / 应用 / 连接器 / 凭据」当作一个同质的「设置型能力」桶,
建议整体折叠。**这个归类是错的**,也是这份文档的核心判断:

- **Schedules 是热的、运营型的活面。** 它是 Owlery 叙事的头牌之一——持久
  agent、离席仍在干活,靠的就是日程。它有一个**可一瞥的计数**(几条在跑),
  这种 glanceable state 属于工作面,不属于「配置一次就忘」的设置。把它和
  凭据一起埋进折叠区/设置菜单,是把产品的招牌功能降级。
- **Connectors / Credentials / Applications 是冷的、配置一次型的集成。**
  它们确实是「接好就走」,一瞥价值低,天然适合收纳。

把四项当一个桶整体折叠,会连 Schedules 一起埋掉——**用错误的同质假设做了
一次错误的收纳**。正确的切法是按使用温度分层,而不是按「像不像设置」分层。

## 3. 方案与取舍

三个候选,都在「rail = 工作面 / 冷集成收纳」的大方向内,差别在收纳的力度
与 Schedules 的去向。

**方案 A —— 整体折叠成一个 "Integrations" 可折叠组(推荐)**
- Schedules **升格**,离开 infra 区,挪到 Task Board 附近,保留计数徽标,
  作为一等工作面继续可见。
- Connectors + Credentials + Applications 收进**一个默认折叠**的
  "Integrations"(或 "Workspace")披露组,组头一行,展开才显三项;点击行为
  不变(仍开各自 Dialog)。
- 折叠状态持久化(localStorage,复用现有 `readStored` 迁移那套约定)。
- 取舍:多一次点击才够到冷集成——但它们本就低频,可接受;换来 rail 面积
  回归 Agents+会话,且未来新集成嵌进组内,rail 零增长。**可无限扩展**。

**方案 B —— 冷三项并入账户/设置菜单,rail 只剩 Agents + Task Board**
- 把 Connectors/Credentials/Applications 塞进 `AccountDropdown` 或
  `SettingsDialog` 的标签页;Schedules 同样升格到 rail。
- 层级最干净:rail = 你会切换主面的东西,页脚 = 一切配置。scaling 也好
  (新集成 = 新标签页)。
- 取舍:冷集成的可发现性掉一级(藏进菜单);且把「工作区集成」和「账户
  个人设置」两条正交的轴混进一个菜单,长期会让那个菜单自己变臃肿。轴不该
  合并。

**方案 C —— 按已配置显隐(show-when-configured)**
- Connectors/Credentials/Schedules 只在 count ≥ 1 时渲染;空的收到一个
  "Manage / +" 溢出入口后面。
- 取舍:活跃项始终可见、空项自动隐身,听着最聪明;但**"东西消失了"是真实
  的认知负担**——count 掉到 0 时该行凭空消失,用户会找不到入口;且逻辑最
  复杂(每项都要监听自身计数)。收益不抵这份不确定性。**不推荐**。

### 推荐:方案 A

理由:
1. 它是唯一同时满足「保住 Schedules 的招牌可见性」「收纳冷集成」「未来零
   rail 增长」三者的方案。
2. 干扰最小:各项点击后仍开原有 Dialog,不动业务逻辑,只动 rail 布局与一个
   披露组。
3. 保持两条轴分离——**工作区集成**(rail 的 Integrations 组)与**账户个人
   设置**(页脚 AccountDropdown)是不同的东西,不该并进一个菜单(这也是不选
   B 的原因)。

## 4. 设计要点(交给执行者的约束,不是实现步骤)

- **分层不变量**:rail nav 只放会切换 `mainSurface` / 内联展开的**工作面**
  (Agents、Task Board、Schedules);冷集成进折叠组;账户/个人设置留在页脚。
  新增能力时按此不变量归位,别再往分隔线下堆扁平行。
- **Schedules 升格**后仍须保留其计数徽标;放置位置与 Task Board 同层级、
  同视觉权重。
- **Integrations 组默认折叠**,展开状态持久化;组头需有一个能一瞥「里面有
  没有东西/需不需要配」的信号(至少一个 count 或"未配置"提示),否则折叠
  会藏掉「你还没接任何连接器」这种该被看见的空状态。
- **可访问性**:披露组用真正的 disclosure 语义(`aria-expanded` /
  button+region),不是纯 CSS 折叠;键盘可达。
- **移动端**:折叠组在窄屏同样成立,展开不撑破 `sidebarOpen` 抽屉。
- **四门全绿**:动了 `App.tsx` 布局与新组件,须补/改前端单测(vitest)与
  必要的 e2e(sidebar 导航/折叠),按 CLAUDE.md 全套跑过再交。

## 5. 顺手要清的账(与本议题同源,别留)

- **Applications 段的 `onAdd` 当前是 `() => {}` 空操作**——一个 `+` 点了
  没反应的死段。按「一次做对」:要么在本次把新增流程接上,要么把 Applications
  段整个删掉。**不允许把一个无功能的段落收进折叠区当占位**——那只是把死代码
  藏得更深。执行者需就「接上 vs 删除」向用户确认一次(这是产品取舍,不是实现
  细节),默认倾向:若近期无 Applications 真实用例,**删除**。

## 6. 这次不做(defer)

- 不做方案 C 的按配置显隐——认知负担不抵收益,已否。
- 不把 Schedules/Connectors 等做成 rail 内联可展开的完整列表——它们各自的
  Dialog 已经是全功能面,rail 只做入口。
- 不新造独立的「Workspace 设置页」大页面;折叠组 + 各自既有 Dialog 足够,
  等集成种类真的多到 Dialog 不够用了再单独立项。
- 不动 `AccountDropdown` 的现有内容(用量/agent 设置/归档/登出保持原位)。
