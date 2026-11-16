# AgentBeats Phase 1:A2A 适配 + 十题成本外推

2026-11-16 · Albus 起草,用户拍板放行 · 时间盒 1 周 · 模型预算 $40 封顶(超即停报)

## 目标

把 Owlery 真实栈接上 AgentBeats 的 A2A 通道,在 Terminal Bench 2.0 场景上**本地**
跑通十题级验证,产出第二闸材料(十题分数 + 全量成本外推 + 风险残项)。
**本阶段不做任何公开提交**——提交是公开动作,留给闸 2 后的 Phase 2,由用户终审。

## 动机

Phase 0 侦察(卡 8eee8cba3f2c,Albus 终审 pass)已坐实:AgentBeats 是 2026-11
时点唯一对第三方开放的可挂名评测通道,且现存绿 agent 中 Terminal Bench 2.0
与 Owlery 本职最贴合。叙事口径沿 TB 案:通用工作台未特调拿 X 分、全程可复现;
预期名次中游,且该场景榜已静默 4 个月——**分数的价值在「可复现的第三方执行」,
不在名次,名次禁止用于宣传**(终审纪律,写死)。

## 设计要点

1. **A2A 适配器,独立模块 + 接线**(原位手术禁)。新模块包一层最小 A2A server
   (task 生命周期 + message/artifact 协议),挂到 Owlery 现有 session/delegation
   接口上。**必须走 Owlery 真实栈**(server/harness 真实引擎、真实 session 流),
   禁止搭考试专用壳——这是 TB 案延续的红线,验收要能指着代码说清请求从 A2A
   endpoint 到 HarnessRun 的完整路径。
2. **考试 agent 隔离**(GAIA 先例):benchmark 用全新无记忆考试 agent,通用人设,
   不含任何生产 agent(Albus/Dobby/Snape/…)的人设、记忆、凭据。公开镜像层里
   只允许出现 Owlery 公开仓代码 + 考试 agent 的通用配置;API key 等 secrets
   一律走运行时注入(平台原生支持),**一个字节都不进镜像层**。
3. **Docker 打包**匹配 Terminal Bench 2.0 绿 agent 的执行环境(参照
   RDI-Foundation/terminal-bench-leaderboard 已合并提交的 amber-manifest 结构)。
   提交管线(Quick Submit 格式、manifest、provenance)做到「万事俱备只差一按」,
   但**不按**。
4. **十题验证**:从 TB 2.0 题集固定 seed 抽 10 题,先 commit 题单再跑(GAIA 纪律);
   单题成本记账,外推全量(题数以绿 agent 实际题集为准,报告里写明);每题
   $3/50 turns 封顶。产出对照表:十题逐题 pass/fail + 成本 + 失败摘要。

## 预注册判据(冻结,看分前不改)

- 闸 2 材料 = 十题分数 + 全量成本外推 + 适配器风险残项(如 Quick Submit 管线
  未实测部分)。**是否进 Phase 2(全量 + 公开提交)由用户拍板,无自动通过。**
- **看分后不提交 → 十题分数定性为内部跑分,禁止外用**(TB 案同款防自欺条款)。
- 适配器在时间盒内跑不通 → 如实报 block,附卡点;不延长时间盒,由 Albus 裁定
  是收摊还是另立卡。

## 交付与验收

- 代码走正常流:分支交付、四门全绿、Snape 复核(若复核委派再遇 infra 连败,
  按既有裁定升级报 Albus,勿原地重试)、Albus 终验收。
- 十题结果与成本台账随卡 comment 交付,脚本/题单入分支可复算。

## 不做清单

- 不做任何公开提交(Quick Submit / fork+PR 都算),不注册发布任何内容。
- 不跑十题之外的题,不做多场景(tau2 等其他绿 agent 一律不碰)。
- 不为提分做任何 TB 特调(提示词、工具白名单之类)——特调即摧毁「未特调可复现」
  的叙事根基,发现想调的冲动就停手记录。
- 不动生产 agent 与生产库。
