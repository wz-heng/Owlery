# 过程指标周报 — Schedule 真实触发证据(§4.3)

记录一次对 `docs/plans/process-metrics.md` §4.3 验收判据("周报经 Schedule
真实自动产出至少一期")的真实验证。裁定方(Albus,任务书作者)确认:预合并
阶段走隔离实例满足判据,不软化成"部署后补办"——详见任务卡评论。所有原始
响应逐条摘录,auth token 已脱敏。

## 环境

- 隔离 `owlery serve` 实例:branch `owlery/task-1dbd483d58df-run-2` @ commit
  `a912e3e` 之前(含本次 schedule 相关修复前的代码),临时 `OWLERY_HOME_DIR`/
  `OWLERY_AGENTS_DIR`/`OWLERY_DB_PATH`(全部在 `/tmp` 下,随后已删除),端口
  `127.0.0.1:8923`,`OWLERY_LEGACY_HOME_DIR=""`。
- PATH 未加任何 fake-CLI 目录 —— harness 解析到真实 `/opt/homebrew/bin/claude`
  (已登录状态),不是 e2e 套件的罐头 CLI。
- **隔离范围的如实说明(Snape 复核第三轮纠正)**:下方 Prompt 跑的两条命令
  对生产 `owlery.db` 的隔离程度并不相同,此前版本把两者混为一谈写成"全程
  只读"是不准确的:
  - 命令①`report.py` 对生产库的连接确实是 `mode=ro` 只读 URI + 本地临时文件
    `.backup()` 快照——这条连接结构上不可能写生产文件。
  - 命令②`skill_backfill_cli.py` 走 `Database.connect_existing_without_
    migrating()`,是一条**具备写能力**的普通连接——这是设计使然:往
    `skill_candidates` 插入新候补正是 §3.3 要求的"唯一写点"本身,不可能做成
    只读连接。这次真实运行打印了"no skill crossed the co-occurrence
    threshold; nothing flagged",按代码路径(`detect_and_flag_corroded_
    skills` 的插入调用只在阈值判定通过的分支里才会执行)可以确定**该次 CLI
    未创建新候补**——但本次没有采集进程级文件写入审计(如 `dtrace`/
    `fs_usage` 之类的系统调用追踪),**不能据此确认部署目录零写入,也不能
    独立归因 WAL 文件的变化**(Snape 复核第三轮再次纠正:"未创建候补"只是
    对 `skill_candidates` 这一张表、通过应用层观察到的事实,不是对整个文件
    系统写入行为的审计结论,此前的措辞仍把两者混为一谈)。
  - 下面这条更窄、经得住复核的断言,已经用一个独立、可重跑的测试
    (`tests/test_skill_health.py::
    test_skill_backfill_cli_only_ever_writes_skill_candidates`)在**一次性
    临时数据库**上验证:构造真实涉败场景、跑真实 CLI 子进程、对全部表的
    行数做改前/改后 diff,**该场景下唯一行数发生变化的表是
    `skill_candidates`,且恰好 +1 行**。这只是行数对比,不是逐行内容/schema
    比对,检测不出"同表内等量删除+插入"或"UPDATE 未改变行数"这类变更——
    不构成"其余表内容完全不变"的结构性证明,只构成这一条更窄的事实。这条
    证据不依赖、也不需要再碰生产库。
  - `/Users/wuzhongheng/owlery-deploy/` 与真实 `~/.owlery`:本次没有做文件
    级写入审计,因此同样不宣称"全程零写入"——只能确认命令①的连接结构上
    不可能写(`mode=ro`),命令②"未创建新候补"是观察到的应用层事实,
    `owlery.db-wal` 的 mtime 变化**未被独立归因**到生产自身进程还是本次
    操作,两者都缺乏进程级审计证据去区分。

## 1. 创建 Schedule(真实 REST API,非内部直调 Python 函数)

```
POST http://127.0.0.1:8923/api/schedules
Authorization: Bearer <REDACTED>
{"name":"process-metrics-schedule-proof","prompt":"...(见下)...","interval_seconds":60,"agent_id":"2800318c90c7"}
```

响应(201 Created):

```json
{
  "id": "8ee7dcebee0c",
  "agent_id": "2800318c90c7",
  "name": "process-metrics-schedule-proof",
  "interval_seconds": 60,
  "recurrence_label": "Every 1m",
  "enabled": true,
  "created_at": "2026-11-16T04:55:41.111544+00:00",
  "last_run_at": null,
  "run_at": null
}
```

Prompt 原文:

```
Run exactly these two shell commands in this working directory, in order, using the Bash tool:

1. .venv/bin/python scripts/process_metrics/report.py --db-path /Users/wuzhongheng/owlery-deploy/owlery.db --out-dir docs/reports/process-metrics
2. .venv/bin/python scripts/process_metrics/skill_backfill_cli.py --db-path /Users/wuzhongheng/owlery-deploy/owlery.db

Then reply with ONLY: the exit status of each command, and the exact output file path printed by command 1. Do not read, interpret, or summarize the report file contents. Do not run any other command.
```

## 2. 调度器真实触发(服务端原始日志,逐行摘录)

```
2026-11-16 12:55:41,112 INFO apscheduler.scheduler: Added job "ScheduleRunner._fire" to job store "default"
INFO:     127.0.0.1:52606 - "POST /api/schedules HTTP/1.1" 201 Created
2026-11-16 12:56:41,113 INFO apscheduler.executors.default: Running job "ScheduleRunner._fire (trigger: interval[0:01:00], next run at: 2026-11-16 12:57:41 CST)" (scheduled at 2026-11-16 12:56:41.112132+08:00)
2026-11-16 12:56:41,165 INFO server.harness.run: Spawning harness claude-code: ['/opt/homebrew/bin/claude', '--print', '--output-format=stream-json', ...]
2026-11-16 12:56:49,852 INFO apscheduler.executors.default: Job "ScheduleRunner._fire (trigger: interval[0:01:00], next run at: 2026-11-16 12:57:41 CST)" executed successfully
```

真实触发时间戳(UTC):`2026-11-16T04:56:41.114340+00:00`(`last_run_at` 字段,
与 APScheduler 本地时区日志行 `12:56:41,113 CST` 一致,CST=UTC+8)。

## 3. 物化出的真实 session + 完整回放(逐条摘录)

`GET /api/sessions?include_archived=true` → 会话 `0e2ab2a5951f`
(`agent_id=2800318c90c7`,`origin="schedule"`,
`working_dir="/Users/wuzhongheng/.owlery/task-workspaces/1dbd483d58df/005903bf74e7"`,
`claude_session_id="2c518d1d-2ff3-4b14-b48e-42322d6befde"`,触发后被
`auto_archive_scheduled_session` 自动归档 —— `archived: true` 正是这条设计
本身的证据,不是数据丢失)。

`GET /api/sessions/0e2ab2a5951f?include_archived=true` 的 `messages` 字段,
按顺序:

1. `user text`:Prompt 原文(见上)。
2. `assistant tool_use`(Bash:命令 1)。
3. `tool tool_result`:
   ```
   wrote docs/reports/process-metrics/2026-11-16.md
   EXIT=0
   ```
4. `assistant tool_use`(Bash:命令 2)。
5. `tool tool_result`:
   ```
   no skill crossed the co-occurrence threshold; nothing flagged
   EXIT=0
   ```
6. `assistant text`(最终回复):
   ```
   - Command 1: exit status 0. It wrote `docs/reports/process-metrics/2026-11-16.md`.
   - Command 2: exit status 0.
   ```

## 4. 产出文件证据

- 路径:`docs/reports/process-metrics/2026-11-16.md`
- 生成时间字段(文件内):`2026-11-16T04:56:45.302300+00:00`
- `sha256sum`:`0ae61c23d5041da021d5f643c2db4dd28c3d2029ea6b51dde297240f12da66be`
- 该文件已以此内容提交(commit `a912e3e`),可直接 `git show a912e3e:docs/reports/process-metrics/2026-11-16.md | sha256sum` 复核。

## 5. 收尾(即建即拆,不留常驻状态)

```
DELETE http://127.0.0.1:8923/api/schedules/8ee7dcebee0c → 204 No Content
GET    http://127.0.0.1:8923/api/schedules → []
```

随后:kill 服务进程(PID 22549)→ 确认 `lsof -i :8923` 为空 → 删除全部临时
`/tmp/owlery-schedule-proof-*` 目录。

关于生产目录是否被写入(Snape 复核第三轮纠正,见上方"隔离范围的如实说明"):
本次没有做进程级文件写入审计,**不宣称** `/Users/wuzhongheng/owlery-deploy/`
或真实 `~/.owlery` "全程零写入"。能确认的只是:命令①的连接结构上不可能写
(`mode=ro`);命令②在应用层未创建新候补(观察到的事实,不是文件系统层的
审计结论)。`owlery.db-wal` 的 mtime 变化**未被独立归因**——既没有证据证明
是本次操作所致,也没有证据排除,只是把它留在"未审计"这一类,而不是像此前
版本那样直接断言"与本次只读连接无关"。

## 复核方法(供独立核验)

1. 本文件逐条引用的服务端日志行、API 响应、会话回放,均为该次真实执行的
   原始输出摘录,未经改写(Schedule id / session id / claude_session_id /
   时间戳均可交叉核对,彼此一致)。
2. `docs/reports/process-metrics/2026-11-16.md` 的实际内容(已提交)即是第
   3 步 tool_result 所称"产出文件"的内容,其正文已带 SQL/口径折叠块——这
   是 Snape 第一轮复核后补上的特性,而这次 Schedule 验证运行的正是工作区
   当时磁盘上已经应用了那批修复、但还未提交的代码(commit a912e3e 把"这批
   代码修复"和"这次 Schedule 真实产出的报告"一起提交,顺序上代码修复在前,
   Schedule 验证在后,提交动作最后)。换言之,这次触发用的不是修复前的旧
   代码,而是第一轮修复之后、第二轮修复(本次评审意见)之前的中间状态。
