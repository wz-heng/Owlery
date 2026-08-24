# Task Board 缺口整治 — 语义出口 / 容器收口 / cancelled 终态 / 交付 UX

Status: approved (user, 2026-08-24)。前一场整治（task-board-overhaul.md）
dogfood 过程中现场撞出的五条缺口，全部有事故现场为证。与「prepare 基线
改从 origin 默认分支取」相关的 HEAD 守门**不在本次范围**——它与
Owlery/Owlery-dev 合仓绑定，另立战役。

## 1. Motivation：五条缺口的事故现场

1. **review 类任务只有状态出口没有语义出口**。T-C 复核带着「不通过」
   结论 complete，依赖图只认 done，T-D 终验收被立即放行，靠验收 worker
   自觉拦截。通过才该放行下游。
2. **打回时建卡权两头都有**。T-D worker block 的同时自建修复/复核卡，
   与 orchestrator 并行建卡撞出两套重复卡（54807b93a4fd/819a57dbc2ad），
   靠人工 cancel + comment 嫁接情报收场。
3. **容器卡无法收口**。战役根卡（如 0d44764ea9d9）从不指派、从无 run，
   worker 终结协议够不到它，orchestrator 又没有收口工具——战役全胜后
   根卡永远挂在 triage。
4. **cancelled 不是终态，住在 blocked 列**。cancel 建模为
   status=blocked + blocked_kind=cancelled；已裁决的死卡顶着 blocked
   样子陈列。实证三连：侧栏 B/C 在 08-10 已 cancel，两周后用户和
   orchestrator 双双误判为「陈尸待裁决」；本役重复卡 cancel 后用户又问
   「为什么有个 T-E blocked」。批量归档入口也收不走它们。
5. **delivery 面板对不可用/已完成动作零解释**。Open PR 重复点击返回
   裸报错而不提示「PR 已存在」+链接；Merge 按钮在最常见路径（已开 PR、
   delivered）下永久灰置且不说明原因。

## 2. What already exists (do not redo)

- delivery op 的 at-most-once 语义（task-git-delivery.md §3）本身正确，
  ⑤ 只改呈现，不碰幂等模型。
- 任务级 `archived` 字段与批量归档入口（task-board-overhaul.md §3.4）；
  ④ 扩其谓词，不重做。
- worker prompt 与 MCP docstring 的建树约定段（overhaul §3.3 落地），
  ② 在同一处追加。

## 3. Design points

### 3.1 review 结论字段化（gate 语义）

- worker 终结协议 `complete` 增加可选 `verdict: "pass" | "fail"`。
- 依赖满足判定改为：done 且（无 verdict 或 verdict=pass）。带
  verdict=fail 的 done 任务不满足任何下游依赖，永不放行。
- 看板卡片对 verdict=fail 的 done 卡显示明确的「不通过」标识，与普通
  done 区分。
- 兼容：存量任务无 verdict，行为不变。worker prompt 中要求复核/验收类
  任务必须带 verdict 收口。

### 3.2 打回建卡权归一

- worker prompt（`server/task_board/prompts.py`）增加明文规则：worker
  发现上游/自身不通过时，以 complete(verdict=fail) 或 block 报告，
  **不得自建修复卡**——建卡与重排是 orchestrator/用户的职权。
- 纯纪律条款写进 prompt 即可，不做工具裁权（worker 建卡在拆分场景仍是
  合法能力，不能一刀切收走）。

### 3.3 容器卡收口

- 新增 orchestrator 侧 MCP/REST 动作 `close`（或等价语义）：仅适用于
  **从未有过 run 的无 run 任务**，且其全部子任务处于终态（done/
  cancelled）。效果 = status→done + result_summary 记收口说明。
- worker 身份调用一律拒绝；executable 任务（有 run 历史）一律拒绝——
  它们必须走 worker 终结协议。

### 3.4 cancelled 终态化

- `cancelled` 升为一等任务终态（transition table 补一行：triage/todo/
  ready/blocked → cancelled；running 仍不可直接 cancel，先停 run）。
- 迁移：存量 `blocked + blocked_kind=cancelled` 行一次性改写为新终态，
  幂等、有 marker。
- 看板呈现：cancelled 不再出现在 blocked 列（归 done 列尾部或独立
  「已终止」区，执行者按现有列结构择优）；blocked 滞龄徽章不再适用；
  批量归档谓词纳入 cancelled。
- API/store/统计（board 容量计数「non-archived, non-done」）逐处核对
  cancelled 的归类，禁止双算或漏算。

### 3.5 delivery 面板动作可解释

- 已成功的出站 op 重复触发时，返回/呈现「已完成 + 结果链接」（如
  PR #N 链接），不再裸抛 conflict 文案；数据已在 op 账本里，纯呈现层。
- 不可用动作（灰按钮）一律带原因 tooltip：来源于状态机的先决条件
  （如「已 delivered：PR 路径下请在平台合并」）。通用规则：**面板上
  每个灰置动作都能回答"为什么"**。

## 4. What this does NOT do

- 不做 HEAD/prepare 基线改造（绑定合仓战役）。
- 不撤销 worker 的 create 能力（②只立纪律）。
- 不做 review verdict 的多值分级（pass/fail 足够；conditional-pass 之类
  等真实用例）。
- 不做 cancelled 的可恢复/重开（cancel 即终局；建新卡代替复活）。
- 不改 delivery op 幂等/at-most-once 模型。

## 5. Test gates

CLAUDE.md 四套全绿为底线。新增覆盖必须包含：
- verdict=fail 的 done 不放行下游（依赖判定单测 + e2e 一条链）。
- close 的三重拒绝（worker 身份 / 有 run 历史 / 子任务未终态）。
- cancelled 迁移幂等性；cancelled 不出现在 blocked 列、可被批量归档、
  容量计数正确（前后端）。
- PR 重复触发返回已存在结果；灰按钮 tooltip 文案存在性。

## 6. Acceptance criteria（用户视角）

1. 复核卡「不通过」后，下游卡纹丝不动，无需任何人肉拦截。
2. 战役收官后根卡可被收口，板上不再有永久 triage 的伞卡。
3. 板上不再存在「看起来 blocked 其实早已 cancel」的卡；cancelled 可归档。
4. 在交付面板上重复点 Open PR 得到 PR 链接而非报错；每个灰按钮悬停可知
   原因。
5. 四套门全绿。

## 7. Execution split

按建树约定：根任务 + 平铺子任务，先后用依赖 link。

- T-A 后端：verdict 字段与依赖判定、close 动作、cancelled 终态+迁移、
  op 重复触发的已存在响应、prompts.py 纪律条款（§3.1–3.4 后端、§3.5
  后端侧、§3.2）。
- T-B 前端：verdict/cancelled/close 的呈现与归档谓词、灰按钮 tooltip、
  PR 已存在呈现（§3.1 UI、§3.4 UI、§3.5 UI）；依赖 T-A，血统上续。
- T-C 独立代码复核（Snape）；依赖 T-B。复核发现问题时按 §3.2 新规：
  只报告不建卡。
- T-D 终验收（Albus）：§6 逐条 + 四套门（完整 e2e）；依赖 T-C。派发时
  给操作清单而非开放题（overhaul 战役 attempt 2 的教训）。
