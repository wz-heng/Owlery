# Attempt 审计回放:组装层 + 定点补盲

## 1. 目标

给定 Task Board 的一次 run/attempt,一键重建它的完整因果链——turns、
工具调用、委派、成本、进程终态——呈现为单一有序时间线。

**验收基准(硬标准)**:task-board-overhaul T-D attempt 2 式的
「2h47m 零日志死亡」,使用回放视图在 5 分钟内定位死因。具体地:
一个 attempt 无声死掉后,时间线必须能回答三个问题——
最后一刻它在干什么、它是怎么死的(exit code / signal / 超时 / 收割)、
中间的空白有多长、两端各是什么事件。

## 2. 动机

2026-08 侦察(对开发仓实证)结论:回放所需数据七成已在库里,
但最关键的死法恰好在盲区。

**已有**:`task_events` 记录 run 状态迁移(started / heartbeat /
terminal / interrupted,带原因 payload);`turn_usage` 按 turn 记
成本、token、`duration_ms`;委派树靠 `sessions.parent_session_id`
可递归重建(`delegations.py` 已有 BFS 邻接表),
`delegation_runs.start_seq` 能把子委派锚回父 session 的具体 turn;
`bg_tasks` 已含 command、exit_code、stdout/stderr、起止时间,是
七块里最完整的。前端已有 `TaskRunTimeline.tsx`(挂在
`TaskDrawer.tsx`),但未消费 `task_events`。

**盲区(现有数据解释不了的死法)**:

1. CLI 进程崩溃/被杀:exit code、signal、stderr 只在 harness 内存
   (`run.py` 的 `_stderr_lines`),进程死亡即蒸发,transcript 直接断掉。
2. `messages` 表无任何时间戳列:无法定位「哪一刻停止推进」,
   也看不出卡在哪个工具调用上;turn 边界只能靠 seq 隐式推断。
3. 心跳原地覆盖:`task_runs.last_heartbeat_at` 只有最后一个值,
   自动心跳路径 `emit_event=False`,看不出「心跳何时停的」。
4. turn 中途死掉不写 `turn_usage` 行,且该表与具体 turn 无外键,
   只能按 `created_at` 粗对齐。
5. watchdog 超时只发 WS 事件(`_surface_turn_timeout`),
   reason/limit 不落表。

## 3. 设计要点

### 3.1 写侧:四针定点补盲

方案取向是「组装层 + 定点补盲」,不是全量事件溯源:只在上述盲区
补写路径,其余一律复用现有表。

1. **`messages.created_at`**。新列,写入时落当前时间;存量行留
   NULL,不回填、不捏造。这一针同时解决 turn 内时间线和
   工具调用耗时两个盲区。
2. **turn 终态铁律(本方案的脊梁)**:harness run 无论以何种方式
   结束——正常完成、进程崩溃、被 SIGTERM/SIGKILL、watchdog 超时、
   收尸路径清理——必须落一条持久化终态记录,至少含:
   session 锚点(session_id + 末尾 message seq)、exit code、
   signal、stderr 尾部(截断存,量级参考 bg_tasks 的 200KB 截尾
   或更小)、终止原因分类(含 watchdog 的 reason/limit)。
   **不变量:没有任何 turn 可以死得不留解释。**收口点在 harness
   的进程退出/收尸路径与 `_surface_turn_timeout`;表结构
   (独立 `harness_exits` 表或等价物)由执行者定,但不变量必须
   有测试钉死:测试中把一个 turn 的子进程杀掉,断言终态记录存在
   且字段完整。
3. **自动心跳降采样落 `task_events`**:流式活动心跳以低频
   (分钟级间隔,非每 chunk)emit 事件,让「心跳何时停」可查。
   频率与去重策略由执行者定,原则是一次 attempt 的心跳事件
   量级在几十条而非几万条。
4. **`turn_usage` 加 turn 锚点**(指向 messages seq 的列),
   写入时已知,成本从此可挂到具体 turn。存量行留 NULL。

### 3.2 读侧:回放组装 API

一个只读 endpoint,按 **session 键控通用实现**(入参 session_id,
task run 入口只是查出 run 对应的 session 后调它):合并
task_events、带时间戳的 messages(turn 边界 + 工具调用)、
turn 终态记录、turn_usage、bg_tasks、递归委派树(子 session 的
时间线可展开或下钻),输出单一按时间排序的事件流。

**空档检测是一等公民**:任何超过阈值(建议默认 5 分钟,可调)
无事件的区间,在响应里显式表示为 gap 对象(时长 + 两端事件),
不是让前端自己对时间轴找空白。旧数据(created_at 为 NULL 的
区间)显式标注「早于观测上线,时间线不完整」,不冒充完整。

### 3.3 前端:升级 TaskRunTimeline

现有 `TaskRunTimeline.tsx` 从「attempt 属性卡片列表」升级为
消费回放 API 的时间线视图:事件按时序渲染,gap 渲染为显式的
「黑洞」块(时长醒目 + 两端事件),终态记录(exit code / signal /
stderr 尾部 / 超时原因)在 terminal 节点展开可读。委派下钻、
跳转子 session 沿用现有跳转机制。视觉与交互细节由执行者定,
验收只看 §1 的三个问题能否在视图内直接回答。

## 4. 不做清单

1. **不引 OpenTelemetry / 外部观测栈**:单用户单机单进程,全部
   真相在同一个 SQLite 里;外部栈换来的能力用自家表就有,却让
   回放与委派树/Task Board 的 join 变成跨系统拼接。
2. **不做实时监控 dashboard**:验收场景是验尸(post-hoc),
   产品哲学是「离席仍干活」——不为「人盯着看」的场景做功能;
   正在跑的 session 聊天视图本来就是实时的。
3. **不做日志聚合 / 全文检索**:回放的结构(入口是 run、时间线
   有序、黑洞高亮)消灭了「不知道去哪找」的前提;全文检索服务的
   是数据考古,无需求实例,不泛化。
4. **不做指标 / 告警**:告警预设「有人随时可被打断」;Owlery 里
   「出事要有人知道」的正确通道是 Task Board 卡片终态本身。
5. **不回填历史数据**:老 run 的 exit code / stderr / 心跳历史
   在进程死亡时已蒸发,无来源可回填;给老行捏造时间戳是伪造
   证据。存量行 NULL + 视图如实标注。
6. **交互会话不单独做 UI 入口**:组装 API 按 session 通用,
   能力白得;但聊天视图自身就是完整转写且用户在场,单独入口
   是为对称性而非痛点,出现真实案例再加(届时是小时级工作)。

## 5. 验收

- §1 硬标准:构造一个中途被杀的 attempt(测试夹具即可),
  回放视图能直接回答三个问题。
- turn 终态铁律有自动化测试钉死(§3.1 第 2 针)。
- 四门全绿(pytest / vitest / tsc / e2e),Snape 代码复核通过。
