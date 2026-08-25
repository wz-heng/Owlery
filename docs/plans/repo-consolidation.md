# 合仓:留 Owlery,退役 Owlery-dev

**定案日:2026-08-24(方向)/ 2026-08-25(开工)。硬条件:本战役与
「prepare 基线改从 origin 默认分支取」绑定——产品改造不上生产,合仓不完成。**

## 1. 目标

`~/vibe-coding-project/Owlery` 成为唯一开发仓,兼任 Task Board worker
源仓;`~/vibe-coding-project/Owlery-dev` 归置后退役。此后所有开发、
docs 分支、run 分支都从 Owlery 出,经 origin(github.com/wz-heng/Owlery,
**PUBLIC**)交付,生产照旧由 owlery-deploy 槽位拉 GitHub 部署。

## 2. 动机

- 生产早已不跑在 Owlery 检出上(生产=deploy 槽位),双仓分裂只剩负债:
  任务书写在 dev 没 push 就对 worker 不可达(task-board-overhaul 首派
  2h47m 空转的死因之一);两仓各自漂移,人要在脑内维护同步状态。
- worker 源仓的本地 HEAD 是毒源:HEAD 停在旧分支,prepare 就以旧基线
  开工。合仓后人机共用一仓,HEAD 漂移从偶发变成日常,不做守门不合仓。
- clean 门拓扑硬伤:今天 prepare 要求源仓 `git status` 全净,人一有
  WIP 就派不了任何 git_worktree 任务。合仓等于把这个冲突变成每天的事,
  必须一并根治。

## 3. T1 · 产品改造:prepare 基线取 origin 默认分支(Dobby)

现状(`server/task_board/workspaces.py` prepare 路径):clean 门 →
`base_ref = symbolic-ref HEAD` → `worktree add … HEAD`。基线=本地 HEAD。

改造后的不变量:**只要源仓配有 origin remote,attempt 的基线只由
origin 默认分支决定,与本地 HEAD、本地工作树状态完全无关。**

设计要点:

- prepare 时先 fetch origin 的默认分支(默认分支名如何探测——
  `ls-remote --symref origin HEAD` 或 remote-tracking HEAD 缓存——细节
  留执行者研究),`worktree add` 以 `origin/<default>` 的 tip 为起点;
  `base_ref` 仍记分支短名(如 `main`,PR base / merge 语义不变),
  `base_head` = origin tip SHA。
- fetch 失败(断网/代理抽风)回退到**本地已有的 remote-tracking ref**
  ——仍是 origin 派生,可能落后但不脏;绝不回退到本地 HEAD。metadata
  里记录走了降级路径。两者皆不可得才拒绝 prepare。
- **无 origin remote 的仓保留今日全套语义**(本地 HEAD 基线 + clean
  门)。这条保住现有测试夹具(全是无 remote 临时仓)和独立小仓用户。
- origin 路径下**撤掉 prepare 的 clean 门**:基线不再取自本地,源仓
  脏不脏与 attempt 内容无关。merge 时的 clean + on-base 门原样保留
  (`delivery.py _op_merge`),交付安全不降。这是 clean 门拓扑硬伤的
  根治,人有 WIP 也能派活。
- 消费侧(`_op_pr` / `_op_merge` / `_verify_base` / 祖先推导收敛)按
  branch 名 + SHA 工作,origin 基线天然兼容;执行者需逐一过一遍确认,
  不需要结构性改动。
- 测试:legacy 路径靠现有夹具回归;origin 路径新增带 bare origin 的
  夹具(单测 + e2e 各至少一条),覆盖 fetch 成功 / fetch 降级 / 双失败
  拒绝 / 脏源仓照常 prepare 四种形态。

## 4. T2 · Owlery-dev 归置(Dobby)

安全不变量:**任何本地分支删除前,其 tip 必须可达自 origin/* 或
planning/*(github.com/wz-heng/owlery-planning,PRIVATE)或已并入
main;不可达者不删,敏感内容推 planning、产品内容推 origin,拿不准的
留下并上报。** 不硬编码「13 条空壳」清单——以清扫时的逐分支验证为准。
敏感判别按 08-24 盘点先例:bilibili / monetization / kaggriculture /
failure-atlas 属敏感线,推 planning;其余产品向推 origin。

- 先 `git worktree list` 清理挂着的 worktree(现有 3 个),再清分支。
- `research/`(2.7G,kaggriculture 数据)移出仓外,落
  `~/vibe-coding-project/kaggriculture-research/`(不入任何 git)。
- 清扫完成的验收态:Owlery-dev 内 `git status` 全净、只剩 main、
  worktree 列表为空;产出一份清扫报告(每分支的处置与依据)。
- **目录本体的删除是用户手动动作,不在本任务内。**

## 5. T3 · 秩序改写(用户 + Albus)

- 各 agent 人设禁区改写,新表述(用户在 agent 设置里应用):
  > 生产部署检出是 `/Users/wuzhongheng/owlery-deploy/`(含 `current`
  > 软链与槽位目录),绝不在其中改文件、跑 git 写操作或提交——那是
  > 部署快照。开发唯一在 `~/vibe-coding-project/Owlery`;docs / feat
  > 分支写完必须 push origin 才算交付。
  同时删除所有「Owlery-dev」「Owlery=生产实例」的旧表述。
- Albus 记忆更新:docs 工作流改为「从 Owlery 拉 worktree、push origin」;
  repo-topology 记忆改写为单仓拓扑。(战役收官时我自己做。)

## 6. 顺序与门

1. T1 开发 → Snape 复核 → 用户 merge + push。
2. **T1 经 Releases 面板部署上生产 ← 硬门:此前不得把日常开发迁入
   Owlery、不得退役 Owlery-dev。**(T2 清扫不依赖 T1,可并行。)
3. T2 清扫完成 + T1 上生产后:T3 改写,用户删除 Owlery-dev 目录,合仓
   完成。

## 7. 不做

- 不做 per-board 基线分支配置——origin 默认分支一个语义够用,真出现
  第二用例再立项。
- 不改 copy 工作区模式;不放宽 merge op 的 clean/on-base 要求(本地合
  就先切到 base,否则走 PR)。
- 不迁移 Owlery-dev 的 git 历史(两仓同 origin,无历史可失)。
- 不动 owlery-planning 的结构;不做定期自动 fetch 守护进程。
- 不给 Owlery 立「保持 main 常驻」之类的人工纪律——T1 的意义就是让
  基线不依赖人的自觉。
