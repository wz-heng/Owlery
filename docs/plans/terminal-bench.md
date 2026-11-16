# Terminal-Bench 2.0 上榜:给 Owlery 拿一个第三方可引用的数字

**状态**:立项(2026-11-16 用户拍板)。分四阶段,带两道成本/可行性闸门。

## 1. 目标

把 Owlery 作为 agent 跑完 Terminal-Bench 2.0 全量题目(官方 Harbor harness,每题 5 attempts),产出**可复现的 job 目录 + 实得分数**,并走官方通道提交上榜(tbench.ai)。交付物是"榜上可查的第三方数字",**不预设分数门槛**——名次中游是预期内结果,叙事口径是"通用工作台、未为考试特调、分数可复现"。

## 2. 动机

用户裁定:自建台账说服力不足,必须有第三方评测集背书。选 Terminal-Bench 2.0 的理由:命题(agent 在真实终端环境完成任务)正中 Owlery 本职;榜单活跃、有 integrity policy(在 GAIA/SWE-bench Verified 公信力塌方的当下,防作弊政策本身是公信力);5-attempts 口径评可靠性,对工作台类系统友好。与 GAIA 侦察盒(docs/plans/gaia-pilot.md)的封盘判定不冲突:封掉的是「Owlery vs 裸 CLI 净胜」命题,本案是绝对名次命题,无对照组。

## 3. 阶段与闸门

### Phase 0 — 零成本核实(先于一切)

提交通道在改版(TB 2.0 新提交政策 2026 年中起"in the works"),开工第一步核实并写进卡面评论:
- 当前确切的提交流程(HF 仓 PR?邮件 job 目录?)、integrity policy 全文、**是否接受第三方自定义 agent/harness 的提交**;
- TB 2.0 的确切题量、Harbor 当前版本与 agent 集成接口文档;
- 估算全量成本公式的参数(题数 × 5 attempts × 单题预估)。

**闸门 0**:若榜方不收自定义 agent 提交,或政策要求 Owlery 无法满足(如必须完全开源某些部分而用户未同意),block 上报,不硬绕。

### Phase 1 — Harbor 适配器 + 冒烟

写一个 Harbor agent 适配器,把 TB 的任务环境接入 Owlery。**硬约束:必须走 Owlery 真实栈**(server → session → harness spawn),不许绕过 Owlery 直接驱动裸 CLI——那样挂榜挂的就不是 Owlery。核心结构性难题留给执行者研究:TB 任务跑在 docker 容器里,Owlery 是宿主机 server 型系统,适配层需要把容器终端桥接成 Owlery agent 可操作的执行面;适配层只做协议翻译,不做 prompt 特调。

用 1-2 道题冒烟跑通全链(任务进 → Owlery agent 干活 → 结果出 → Harbor 判分),实测单题成本与墙钟。

### Phase 2 — 小样本试跑(≤10 题)

抽 10 题跑完整 5-attempts 管线,验证稳定性,**外推全量成本**。

**闸门 1(用户拍板)**:把外推成本(美元+墙钟)报给用户,用户点头才进 Phase 3。Phase 0-2 合计预算盒 $30。

### Phase 3 — 全量 + 提交

全量题目 × 5 attempts,产出官方格式 job 目录。跑完先把分数报用户,**提交动作(PR/邮件,属公开外发)由用户最终确认**。预注册解读规则(防自欺):若用户看分后决定不提交,此线定案为「无第三方背书可用」,内部跑分不得用于任何对外宣传——不存在"自己跑的分凑合用"的中间态。

## 4. 执行纪律(前两次战役的学费,照抄执行)

- **长批跑必须 detached 后台进程 + 写盘断点续跑(幂等,已完成的 attempt 不重跑)**;worker 发起批跑后用 `block` 让位等人工/定时唤醒,**绝不在 turn 里干等**(GAIA 侦察盒 attempt-2 死因)。
- 跑题过程成本/进度逐题落盘,结论可离线复算;原始轨迹全存档。
- 跑的是 Owlery 现状:**禁止为刷分修改产品代码**。适配器等新增外围代码放 `benchmarks/terminal-bench/` 独立目录。若发现 Owlery 真 bug,记录并上报,修不修另立卡,不混进本案。
- 复核:免 Snape,数字验收(适配器属外围工具代码,验收=冒烟全链跑通+可复现;先例=Kaggriculture)。

## 5. 不做

- 不与裸 CLI 对比(封盘条款仍然有效),不重开 GAIA。
- 不做 HAL 集成(二期备选,等本案落地后另议)。
- 不做 prompt/工具特调冲榜,不挑题,不隐藏低分重跑。
- 不在本案内修 Owlery 产品代码。

## 6. 改道记录:闸门 0 触发 → HAL 转正(2026-11-16 用户拍板)

**闸门 0 实录**:Phase 0 核实(卡 a73778e6e242 评论 7383660c99f6,$0 花费)确认 Terminal-Bench 全系列(2.0/2.1/3.0/4.0/4.1)对第三方关闭社区提交——官方 `harbor-framework/terminal-bench` 仓 `leaderboard/SUBMIT.md`(2026-11-12 更新)明文只收维护者自跑结果。组织级关门,非技术障碍(Harbor BaseAgent 集成路径现成)。按 §3 闸门 0 上报,用户选 (a):**目标榜改为 HAL(hal.cs.princeton.edu,Princeton,ICLR 2026)**。

**HAL 版 Phase 0(零成本核实,先于一切)**:
- 提交流程:官网 "Submit an Agent" 指向的 GitHub agents guide 全文;**是否接受外部自定义 agent/harness 提交、由谁跑**(自跑交 trace vs 官方代跑);
- **trace 公开粒度(新风险点)**:HAL 用 W&B Weave 全程记录轨迹并加密发布——核实公开的 trace 里会不会暴露 Owlery 的 system prompt/人设/内部工具协议,以及能否控制;
- 九 benchmark 选型:先上一个,选型标准=①提交确认开放 ②命题贴"工作台在真实环境干活"(候选优先级:SWE-bench Verified Mini / TAU-bench Airline / AssistantBench;GAIA 在 HAL 内跑绝对分不违反 gaia-pilot 封盘条款——封的是 vs 裸 CLI 净胜命题)③全量成本最低者优先;
- 集成工作量与接口:HAL harness 对 agent 的接口形态、W&B 依赖、与 Owlery server 型架构的对接点。

**闸门 0'(HAL 版)**:不收外部 agent 提交,或 trace 公开要求会暴露不可接受的内部细节且无法规避 → block 上报,不硬绕。

其余结构不变:闸门 0' 过 → Phase 1 适配器(硬约束同 §3:走 Owlery 真实栈)→ Phase 2 小样本外推 → 闸门 1 成本用户拍板 → Phase 3 全量+提交(提交动作用户终审,预注册解读规则同 §3)。§4 执行纪律、§5 不做清单原样生效;Phase 0-2 预算盒 $30 重置续用。

**平行支线(用户未拍板,仅备案)**:向 TB 维护者发自荐邮件请其代跑 Owlery——选项 (b),未启动。
