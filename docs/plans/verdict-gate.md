# 结果验证:让 verdict 承重(verdict-gate)

**状态**:定案任务书(2026-09-05,Albus 起草,Aberforth 对抗性评审一轮,三 P0 全部吸收,用户批准开做)。

## 1. 目标

Task Board 的交付判定从「信汇报」改为「查证据」。一个任务算完成,必须同时满足:

1. 存在**开工前冻结**的验收判据;
2. 每条判据有**系统持有的执行记录**作为证据(不收执行者自报);
3. 验证门(gate)状态由机器谓词消费:非 pass/waived 即阻塞下游解锁与交付 finalize,人工放行必须留痕。

一个任务仍然只验证一次(跑一遍判据)。本方案不增加验证轮数,只让唯一那次验证的结果真正拦路。

## 2. 动机

验尸档案里三案同构——**检查存在,结果不拦路**:

- task-board-overhaul attempt 2:终验收 agent 逐项打勾但没有重跑测试,2h47m 无产出才被发现;
- 同战役:review 卡 verdict=不通过,下游任务照样放行、照样交付;
- worker 交活自报「四门全绿」无任何证据,系统照收。

另有 memory-ui B 分支:代码通过了 Snape 审查,但任务承诺的 3 条 @llm 冒烟从未跑过——diff 审查天然看不见「没写的东西」。

共因:verdict 目前只是卡片里一段没人消费的文字,不是任何机器判断的输入。历史失败不是「永不发现」而是「发现得晚」,代价是返工;所以投入形态是**补拦路**,不是重建信任体系(08-30「验收门层」裁定沿用:否掉按域各建检查定义层的通用框架,那只会量产打勾剧场)。

## 3. 设计要点

### 3.1 预注册判据(criteria)

- specify 阶段必须写判据。**结构化 rows,不是一个大文本框**:每条判据 = 验证动作 + 预期观察。机器做廉价的结构拦截(拒收空预期、拒收「测试通过」这种只有结论没有动作的条目),**不假装能判语义充分性**——判据质量靠冻结+可见性交给人审。
- 判据在 attempt 开始前冻结为不可变版本(criteria_version)。开工后允许修改,但必须产生新版本并记录修改者与理由,旧版本下的证据自动失效。**若判据可在完工后无痕修改,预注册即不成立**(Aberforth P0-1)。
- worker 不能在交活时自行修改判据并自行认可。
- 按卡类型给**模板提示**(只是提示,不建域验证器):
  - 修复卡:repro 命令,修前红、修后绿;
  - 功能卡:做完后执行哪几个操作、分别应观察到什么;
  - 研究卡:数字判定,预注册且可重跑(kaggriculture 数字验收模式的推广);
  - 成本卡:基线 + 指标 + 时间窗 + 被牺牲项守线(防「省了钱、打穿 SLO」的单指标验证)。
- **人的否决权保留**:预注册项全过 ≠ 承诺全兑现。reviewer/用户可以「判据不充分」为由直接拒绝验收,此时的正确动作是补判据出新版本,而不是绕过。

### 3.2 证据 = 系统持有的执行记录引用

- **不收自报,不收 worker 可写的文件路径**——那只是把撒谎(或真诚搞错)的成本提高一步:路径可覆盖、文件随 workspace 消失、旧 attempt 的绿日志可冒充(Aberforth P0-2)。
- evidence record 引用**系统已记录的执行轨迹**中的具体条目(attempt 审计回放层已保存每个 turn 的 tool-call 与输出——复用它,不建新记录层),并绑定 `task_id + attempt_id + criterion_id + criteria_version`。worker 伪造不了系统替他记的账。
- 四门的跑门证据同样走这条路:引用系统记录的那次执行,而非贴日志路径。
- 人工验证步骤(如「手机收到测试邮件」)附系统保存的 observation/artifact,显式标注 provenance=human,承认其证据等级低于机器执行记录(「证言≠证据等级」先例的机制化)。
- 执行基础设施错误(环境炸了、超时)记为 verification run 的 error,**不冒充业务 fail**。
- 威胁模型 = agent 会真诚地搞错,不含蓄意伪造。故不建独立 runner/CI/沙箱;若未来出现伪造事实,再议(那需要沙箱、secrets 隔离、测试定义保护等一整套,投入完全不同量级)。

### 3.3 verdict 门状态机与承重

- **拆开两件事**(Aberforth P0-3):review 卡自身的 done 只表示「审查这件工作做完了」;「被审对象过没过」是 verification gate,一等公民,显式绑定 `subject_task_id + subject_attempt_id`。下游 readiness 检查的是 subject attempt 的 required gates,**不是** review 卡是否 done——否则历史故障原样重演。
- gate 状态机:`pending / pass / fail / waived`。**默认关门**:required gate 处于 pending 同样阻塞,不能等 fail 出现才拦(否则 reviewer 还没交结论、下游已开工的竞态无解)。
- **delivery 拆层**:worker 提交 delivery candidate + 证据 → 判定 → 系统 finalize。pending/fail 阻塞的是 finalize 与下游 acceptance,**不阻塞 candidate 提交**(否则 reviewer 拿不到被审材料,死锁)。
- fail 不阻止 reopen/retry。新 attempt 的 gate 回到 pending;旧 verdict、旧证据、旧 waiver 一律不自动继承。
- **放行谓词中心化**:acceptance predicate 只有一份,MCP、UI、恢复流程、管理操作全部共用,且 gate 判定、acceptance、依赖解锁在 TaskRepository 同一事务/CAS 边界内完成(沿用 Task Board 既有 CAS 纪律)。
- **用户放行 = waived**,不是把 fail 改成 pass。记录 actor、理由、时间、被豁免的 gate、subject attempt、criteria_version(08-24 红测裁决放行是先例,现在必须留痕)。新 attempt 默认使 waiver 失效,除非用户再次明确携带。

### 3.4 伸缩与分工

- 验证方式按事情大小伸缩,**底线(有判据、有证据)不伸缩**。简单任务:判据三行、证据由系统从执行记录自动生成引用,不强制 Snape。kaggriculture 免 Snape 数字验收是伸缩先例。
- Snape 角色不变:代码审查(diff 层面的对错)照旧。本机制给他两样东西:预注册判据让「对着承诺清单查测试覆盖」有了固定靶子;gate 承重让他的 fail 第一次具有机器强制力。

## 4. 存量与迁移

存量开放任务一律 grandfather:无判据的旧卡不加 required gate、不被新谓词阻塞。机制上线后新建的卡适用全部规则。不做回填。

## 5. 执行者需研究的点

细节留给执行者,以下是已知需要设计判断的位置(不是实现步骤):

- 非确定性:flaky 测试/指标抖动下,重跑次数与最终 verdict 的归属规则;
- 判据 rows 与逐条证据引用的 UX/token 成本控制,别让简单任务在呈现层真变臃肿;
- 证据引用的保留周期与脱敏(transcript 里可能有敏感输出);
- 验证适用环境的标注(本地绿 ≠ 部署环境绿,至少写明在哪验的);
- 与 experience-consolidation 复盘门的衔接(gate fail 算不算非 clean pass 的输入)。

## 6. 验收判据(本任务书自身的,预注册)

1. required gate 为 pending 或 fail 时,finalize delivery 与下游 unblock 的所有路径被机器拒绝(测试证明,含并发/恢复路径共用同一谓词的证明);
2. 判据冻结后修改产生新版本、旧证据失效;evidence record 无法引用不存在的执行记录条目(测试证明);
3. **三案复演试金石**:机制化测试重演三起历史事故——(a) 验收者不重跑只打勾 → 无有效证据,finalize 被拒;(b) review gate=fail → 下游 unblock 被拒;(c) worker 自报绿无证据 → candidate 可提交但 finalize 被拒。三案全部拦住才算过;
4. waive 路径:用户放行留痕齐全,新 attempt 后 waiver 自动失效(测试证明);
5. 四门全绿 + grandfather 行为验证(存量卡不受阻)。

## 7. 不做清单

- 不建修复/安全/成本三套域专用验证器(打勾剧场,08-30 裁定);
- 不建失败原因 taxonomy(等第二个机器消费方出现再立项);
- 不建独立验证 runner/CI/沙箱(威胁模型不含蓄意伪造);
- 不做常驻验证 agent、持续监控告警(usage 页观测已够);
- 不动四门本身;
- 不做交付后异步长时间窗验证(成本类多天观察窗)——真有第二个用例再立项;
- 不建多级权限体系:单用户系统,waive=用户本人操作+留痕即可;
- 不做存量任务回填。
