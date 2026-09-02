# 经验沉淀 v2 — Aberforth 评审吸收轮

状态:定案(09-02,Albus 裁定 + 用户放行)。前身 v1 见
`docs/plans/experience-consolidation.md`,已并 main(PR #17)。本轮吸收
09-02 Aberforth 对 v1 的评审中经裁定接受的五件事;被否的部分连同理由收在
§「不做」,防止未来重蹈。

## 1. 目标

1. **clean-pass 自愿提炼入口**:一次通过的 run 也能在原 worker 仍持有
   完整上下文时,显式地把走通的流程沉淀为候补。
2. **审查页证据链**:批准候补时能看到证据,不再凭文案批准。
3. **能力包(bundle)+ 双作用域**:候补从单文件 SKILL.md 升级为目录
   (可含 scripts/templates/examples/tests);作用域支持
   agent-global(跨仓)与 agent+repo(现状)两档。
4. **Codex 激活**:批准后的 skill 让 Codex 后端 agent 也真实加载。
5. **invocation→run 关联**:skill 调用记录消费它的 run/session,
   供人审修订候补时看历史。

## 2. 动机(逐条,含事实锚点)

**①** v1 的触发器是「非一次通过才强制复盘」——这条分水岭保留(防复盘
疲劳)。但机制的创始案例(Aberforth 走通 hermes PR 全流程后未固化,
数月后重踩全部坑)本身就是一次通过:v1 把创始案例排除在外了。现状核查:
`reflect` 工具与 skills 提名通道本就全时可调
(`server/task_board/manager.py` 复盘门注释自承 "callable any time"),
**实缺口只是 affordance**——clean pass 完成时没有任何提示或信号位,
worker 不会想起来。修入口,不改分水岭。

**②** 候补数据已带 task/run 字段,`SkillCandidatesPage` 没有把它们
变成证据。凭标题+理由+diff 批准,是审批侧的打勾剧场——与 08-30
verdict-gate 裁定(「检查存在,结果不拦路」)同构,也与 v1 战役里
Snape 抓的「批准的 skill 不真实加载」是同一物种的镜像。

**③** SKILL.md 规范本就是「目录 + 主文件」;可复用能力常需要已验证
脚本、模板、样例、验收命令。跨仓流程(如「外部 PR 全流程」)在现行
per-(agent, repository) 隔离下没处放,只能每仓复制一份。

**④** v1 的激活走 Claude `--plugin-dir`,Codex agent 提名得了、
消费不了,与 Owlery「双后端同一能力面」冲突。事实核查(09-02 侦察):
Codex 已于 2025-12 原生支持同格式 SKILL.md 自动发现(扫
`$HOME/.agents/skills`、仓库 `.agents/skills` 等,见
https://developers.openai.com/codex/skills )。格式同源,激活层只需
一个 materialize 目标的 adapter,治理/候补/人审模型不动。

**⑤** use_count 只按 slug 计数,证明「被触发过」不证明「帮上了忙」。
人审一个修订/废弃候补时,至少要能看到这个 skill 被哪些 run 用过、
那些 run 的结局如何。只记外键,不建指标(理由见「不做」)。

## 3. 设计要点

### ① clean-pass 提炼入口
- `complete` 增加可选的 reusable-outcome 语义(参数或等价显式动作):
  worker 自报「这次走通的流程新颖/复杂/预计复用」。
- worker 协议文案(task 工作协议注入)更新:clean pass 且自认流程有
  复用价值时,提示在**原 run 内**先走 skills 提名 + `reflect`,趁
  上下文热;绝不另派 agent 重读历史。
- 铁则:自愿、不强制、机器不判定「新颖」。强制复盘的触发条件一个字
  不改。

### ② 审查页证据链
- `SkillCandidatesPage` 每个候补链出:来源 task / run / attempt、
  原执行会话(可点进)、新建 skill 还是修订既有 skill、作用域、
  目标后端。
- 候补创建时跑静态 lint(frontmatter 合法、slug 冲突、bundle 内引用
  文件存在可读),结果存候补记录并在页面展示。lint 挡明显残次,
  不替代人审。

### ③ bundle + 双作用域
- 候补与批准后的落地支持目录形态:`SKILL.md` 为主文件,可携
  `scripts/` `templates/` `examples/` `tests/` 等子目录;worker 在
  复盘/提名时亲手把文件写进候补目录(worker 在场、手里就有产物)。
- 审批 UI 展示完整文件树与每文件 diff,不只主文件。
- 作用域二值:`agent-global`(无 repo 指纹的 plugin 目录,所有该
  agent 的 session 加载)与 `agent+repo`(现状)。提名时选定,
  人审可改。
- approve 双落地(git 分支 + 真实加载目录)的 v1 不变量原样保留,
  global 档同样双落地。

### ④ Codex 激活 adapter
- 统一候补/治理/审批不动;approve 的 materialize 按 agent 后端多写
  一个目标:Claude → 现行 plugin 目录;Codex → 该 agent 的
  `.agents/skills` 发现路径(具体挂载点——per-agent home、
  `CODEX_HOME` 还是 session cwd 注入——由执行者研究定,约束只有一条:
  **不得往用户仓库工作树里种文件**)。
- 禁把 SKILL.md 全文塞 prompt 的糊法:每轮上下文税 + 破坏按需发现。
- Codex 侧 use_count 归因:若 Codex 事件流里没有等价的 skill 调用
  事件,则明确标注 Codex 调用不计数(best-effort),不造假数据。

### ⑤ invocation→run 外键
- skill 调用记录增加消费方 run/session id(现有 use_count 归因机制
  的自然延伸,v1 T-B 已修过跨仓错账,沿同一路径)。
- 修订/废弃类候补的审查页展示该 skill 的调用历史(哪些 run、终态)。
- 到外键和展示为止。不聚合、不算率、不设阈值。

## 4. 不做(及理由,防重蹈)

- **自动试运行 skill**:需要环境供给与成败判定,本身是一个新验收门
  层,独立大特性;等真实需求再立项。静态 lint 已覆盖残次品。
- **artifact ID + 内容哈希提名管线**:worker 在场亲手写文件进候补
  目录即可;哈希溯源是给「不在场的提炼者」设计的,而 v1 已否掉二级
  提炼 agent。
- **有效性指标层**(调用后一次通过率、「相比未调用减少返工」等):
  无对照组的因果归因,产出伪精确——一个用了 skill 且失败的 run,
  锅在 skill 还是任务,数字答不了。同否掉多 agent 文章 A/B 主张的
  理由。
- **自动生成修订/废弃候补**(连续非 clean pass 触发):等第一个真实
  的 skill 腐化案例;v1 的治理闭环(skill 走不通→非一次通过→复盘
  修正)先跑,不够用再说。
- **每任务强制复盘**:v1 已否,维持。复盘疲劳杀死策展。
- **机器判定「新颖/复杂/值得沉淀」**:什么值得留下永远是人(worker
  自报 + 用户审批)的事。

## 5. 验收(预注册)

四门全绿之外:
1. 试金石 A:一个 clean pass run 内,worker 经新入口自提候补并走完
   提名,候补出现在审查页且证据链可点。
2. 试金石 B:approve 一个 agent-global 候补后,该 agent 在**另一个
   仓库**(或无 git 仓)的 session 真实加载到它(Claude 路径)。
3. 试金石 C:approve 后,Codex 后端 agent 的 session 能经原生发现
   路径加载同一 skill(真实 spawn 路径,不许假调用——v1 T-B 的
   e2e 打勾剧场教训)。
4. 审查页对至少一个真实候补展示完整证据链(来源 task/run/会话/
   lint 结果/文件树)。
