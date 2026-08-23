# Task Board 整治 — 从委派日志回到工作台

Status: approved (user, 2026-08-23). Scope decision: 五条产品项全做，一次做对。

## 1. Decision and motivation

Task Board 上线以来实际扮演的是「委派的包装纸」：任务在派活那一刻创建、
干完即关，板上长期 0 open / 0 in_progress，历史堆积后打开即考古现场。
2026-08-23 盘点的具体症状：

- **交付按钮泛滥**：交付面板无条件跟随每个完成的 `git_worktree` run
  （task-git-delivery.md §21），但真实工作流是链式收敛——子任务的 run
  逐个在同一条分支血统上续，最后一个分支包含全部（预算路由收敛于
  `owlery/task-5b08d7539918-run-1`、记忆 UI 收敛于 B 分支）。产品不认识
  链，于是每张卡都端出 Accept/Commit/Push，用户每轮都要人工确认该
  accept 哪张。
- **Releases 面板挤压看板**：release 历史只增不减且不可折叠，「账本」和
  「工作台」抢同一块屏，看板可视区随部署次数单调缩小。
- **树图混乱**：schema 本来就把分解树（`parent_task_id`）和执行 DAG
  （`task_dependencies`）分开（task-board.md §3.3），但建树从无规则：
  修复轮有时挂设计任务、有时挂上一环（预算路由 F/G 挂在终验收 E 下）、
  有时不挂（记忆 UI A/B 无 parent）。parent 被混用来表达「归属」和
  「先后」两件事，树图因此不可读。
- **done 无收纳**：48/50 可见任务是 done，与 2 条陈尸两周的 blocked 混在
  默认视图里；真 blocked 出现时不会有人注意。
- **list 载荷失控**：任务 body 揣着整本任务书全文，MCP `tasks list` 一页
  返回 ~19 万字符，limit 封顶 50 且无分页，既撑爆 agent 上下文也数不出
  板上总量。

本任务书只治产品侧。使用纪律（分板、blocked 治理、建树规则的遵守）由
orchestrator agent 的记忆承担，不在此实现范围——但 §3.3 会把建树约定写进
MCP 工具描述，让约定对未来的 orchestrator 自然生效。

## 2. What already exists (do not redo)

- 分解树与依赖 DAG 的双关系模型、环/深度校验（task-board.md §3.3）。
- 完整的 delivery 状态机与 op 账本（task-git-delivery.md §4）；本次不改
  任何状态机语义，只在其上加一层**派生**的收敛关系。
- 任务级 `archived` / `archived_at` 字段已存在
  （`server/task_board/models.py`）；④ 建在它上面，不发明新状态。
- Release-line 管线与 Releases 面板（release-line-deploy.md §3.4）；只改
  展示层收纳，不碰管线。

## 3. Design points

### 3.1 交付收敛：superseded 派生关系（治按钮泛滥）

**原则：收敛关系从 git 事实推导，不引入新的用户概念，不依赖使用纪律。**

- 判定：delivery A 被 delivery B 收敛，当且仅当两者属于同一 board 的同一
  仓库、`headA` 是 `headB` 的严格祖先（`git merge-base --is-ancestor`，
  且 `headA != headB`）。attempt 分支存活于主仓库，worktree teardown 后
  判定依然可行。
- **dirty 豁免**：`dirty = true` 的 delivery 永不标记为被收敛——未提交
  的改动不被任何分支包含，收起它的按钮会丢工作。
- 持久化：`task_deliveries` 增加 `superseded_by_delivery_id`（可空）。
  在新 delivery 达到 `ready` 时及 status 刷新时幂等重算；重算只能从
  null → 指向某 delivery 或在祖先关系消失时回退为 null（分支被删等边界
  由执行者定义清楚）。派生字段，不进状态机。
- UI：被收敛的 delivery 面板整体塌缩为一行「已由任务 X 的交付收敛
  （该分支包含本分支全部提交）」+ 跳转链接；Accept/Commit/Push 等按钮
  全部不渲染。tip（未被收敛者）保持完整动作行。一条链上任意时刻只有
  一个可 accept 入口。
- accept tip 成功后，提供对全部被收敛 delivery 的**批量 teardown**入口
  （逐个走既有 teardown op，复用既有确认与保留策略，不新造批量 op）。

### 3.2 Releases 面板收纳（治挤压）

- 默认态只渲染：当前 in-flight（或最近一次终态）的 release 行 + 折叠
  控件。面板高度不再随历史增长。
- 展开态显示最近 10 条，更早的走「加载更多」分页（REST 加
  offset/limit 或 before-cursor，执行者选一种并全局一致）。
- 回滚等操作入口保留在展开态，行为不变。折叠状态记入 localStorage
  （沿用 `readStored` 约定）。

### 3.3 树图语义（治树乱）

约定（本任务书定为规范）：

- `parent_task_id` 只表达**归属**：一场战役 = 一个根任务，所有子任务
  （含修复轮、复核轮、终验收）**平铺挂根**，不许挂在上一环下面。
- **先后**只用 `task_dependencies` 表达。

产品改动：

- 把上述约定写进 MCP `create` / `specify` 工具的 docstring 与 worker
  prompt 中涉及拆分的段落——约定对模型可见才会被遵守。
- Tree view（task-board.md §10.2）按新语义渲染：parent 树为骨架，依赖
  以视觉可区分的边/badge 呈现在同根分组内；执行者阅读现状后决定最小
  改法，目标是「一场战役一眼看清骨架与先后」。
- 不迁移历史数据；旧的畸形树保持原样。

### 3.4 done 与 blocked 收纳（治考古现场）

- 看板默认视图：done 列只显示最近 15 条，其余折叠为「更早 N 条」入口，
  点开分页加载。
- 已交付完成（delivery 终态且 teardown 完毕）的 done 任务提供一键
  「归档」，批量入口作用于当前折叠区；archived 任务不出现在默认视图
  （执行者核实现状语义，若已如此则只补 UI 入口）。
- blocked 卡显示滞龄 badge（如「blocked 14d」），让陈尸可见。
- 不做自动归档策略引擎、不做 TTL；归档是显式人类动作。

### 3.5 list 减负与分页（治 19 万字符）

- MCP `tasks list` 与 REST `GET /api/tasks` 的列表项不再返回 body 全文：
  返回摘要字段（id/title/status/assignee/parent/board/时间戳/archived）
  + `body_excerpt`（首 ~200 字符）。全文一律走 `show` / 单任务接口。
- 分页：`limit` + `offset`（或 cursor，与 §3.2 的选择保持一致）+
  `total` 计数；server cap 保留但可翻页穷尽。
- worker 协议不变：worker 走 `show`/`current`，不受影响。前端卡片本就
  不需全文；drawer 若尚未走单任务接口则改为打开时取全文。

## 4. What this does NOT do（否掉的方案及理由）

- **不引入 "delivery group / 发布单" 实体**：收敛关系可从 git 祖先事实
  推导；第二个手工维护的概念只会制造新的不一致面。
- **不用纪律替代产品修复**（「只有终验收任务请求交付」之类的约定）：
  靠自觉必复发，这正是本次整治的起因。
- **不做历史数据迁移**：不重挂旧任务的 parent，不回填旧 delivery 的
  supersede（重算天然覆盖仍有活跃分支的旧 delivery，足够）。
- **不做多板拆分类产品功能**：board 维度已存在，分板是使用纪律。
- **不做自动归档/TTL 策略引擎**。
- **不改 delivery 状态机与 worker 终结协议**。

## 5. Test gates

CLAUDE.md 四套全绿为底线。本次必须新增覆盖：

- supersede 判定单元测试：直链、菱形（两个独立分支互不收敛）、dirty
  豁免、teardown 后仍可判定、分支删除后的回退语义。
- list 摘要与分页：REST + MCP 两层；一页载荷上限断言（防 body 回归）。
- e2e：被收敛卡无 accept 按钮且显示收敛指向；tip 卡按钮齐全；Releases
  默认折叠且展开分页可用；done 列默认收纳；归档后任务离开默认视图。

## 6. Acceptance criteria（用户视角）

1. 一条任务链交付后，看板上有且只有一张卡可 accept，其余显示收敛指向
   ——用户不再需要询问该 accept 哪个。
2. 部署 N 次后打开看板，Releases 占用高度与 N 无关。
3. 新战役按约定拆分后，树图一眼可读：根下平铺 + 依赖边。
4. 板上积累数百 done 任务后，默认视图仍只有一屏内的活跃内容。
5. `tasks list` 默认一页载荷在数千 token 量级，且可翻页数出总量。

## 7. Execution split（建议，派单由用户定）

按 §3.3 新约定建树：根任务「Task Board 整治」，子任务平铺挂根，先后用
依赖 link：

- T-A 后端：supersede 派生 + list/REST 摘要与分页（§3.1 后端、§3.5）。
- T-B 前端：收敛 UI、Releases 收纳、树图渲染、done/blocked 收纳、归档
  入口（§3.1 UI、§3.2、§3.3 渲染、§3.4）；依赖 T-A。
- T-C 独立代码复核（Snape 标准）；依赖 T-B。
- T-D 终验收：对照 §6 逐条验收 + 四套测试门；依赖 T-C。
