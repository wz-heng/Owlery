# 记忆管理 UI（memory-ui）

> 2026-08-23 与用户定案。三个澄清已拍板：场景=审计纠错+日常查阅+对外演示三者全要；
> 写模型=一切修改走「委托 agent 自改」，UI 零写路径；展示深度=做 [[链接]] 关系图。

## 目标

给 Owlery 增加一个**只读**的记忆面板：浏览、检索、可视化各 agent 的持久记忆，
并提供「委托纠错」入口。审计、查阅、演示三个场景一次做全。

## 动机

持久记忆是 Owlery「持久团队」定位的核心差异化（介绍页三支柱之一），但今天它只是
`<agents_dir>/<agent_id>/memory/` 里的一堆 markdown——用户完全不可见、不可审、
不可展示。agent 记错了没人发现，找一条历史结论要靠翻文件，给人演示时拿不出实物。
纠错现状=用户在聊天里口头让 agent 改，没有从「看到错」到「发起改」的入口。

## 设计要点

### 1. 后端：只读 memory router

新增 `server/routers/memory.py`，挂现有 auth，四个能力：

- **列表** `GET /api/memory/{agent_id}`——扫该 agent 的 memory dir，解析 frontmatter
  （name / description / type），返回文件清单+元数据；`MEMORY.md` 单列，作为该 agent
  的记忆首页（索引）。
- **正文** `GET /api/memory/{agent_id}/file?name=…`——返回单文件原文。路径必须锁死在
  该 agent 的 memory dir 内（防穿越），这是本 router 唯一的安全要点。
- **搜索** `GET /api/memory/search?q=…`——跨全部 agent 全文扫描，返回
  agent + 文件 + 命中片段。语料量级=每 agent 几十个小文件，逐文件直扫即可。
- **图** `GET /api/memory/{agent_id}/graph`——解析全目录 `[[链接]]`，返回
  nodes（含 type、是否悬空）+ edges。链接语义=**agent 内命名空间**；指向尚不存在
  name 的悬空链接也要返回并标记 ghost——那是 agent「标记了但还没写」的记忆。

解析放后端的理由：pytest 覆盖便宜；图和搜索天然要扫全目录；前端拿到的就是干净结构。
路径 helper 直接复用 `server/agent_memory.py` 的 `agent_memory_dir()`，不另起炉灶。

### 2. 前端：顶级 Memory 页

- 独立路由 + 侧栏入口。入口温度=中温，放 Schedules 同层——遵循 sidebar-hierarchy
  的温度分层原则；它不是集成，不进 Integrations 组。
- 三栏结构：agent 切换 / 文件列表（type 筛选 chips：user・feedback・project・
  reference）/ 阅读视图。顶部全局搜索框，命中可跨 agent 跳转。
- 阅读视图 markdown 渲染；`[[链接]]` 渲染为可点击跳转（同 agent 内解析），
  悬空链接置灰不可点。
- **关系图**：按 agent 一张（不做跨 agent 混图），节点按 type 着色，ghost 节点
  虚显。可视化选型执行者定，约束只有一条：不引重量级依赖。

### 3. 纠错=委托，不发明新机制

阅读视图一个「纠错」按钮：为该 agent 新建 chat session，prompt 预填模板
（文件名 + 用户批注占位 + 「请核实并更新你的记忆与索引」），跳转到该 session，
用户补完批注后自己发送。复用现有 session 创建与聊天流，**零新后端写机制**。

铁律：本功能没有任何写路径。UI 和后端都不直接改记忆文件——修改一律由 agent
自己完成，`MEMORY.md` 索引与正文的同步问题因此整个不存在。

### 4. 刷新语义

无实时推送。进入页面、切换 agent、手动刷新按钮时重拉。

## 测试

- 后端 pytest：frontmatter/链接解析、路径穿越防护、搜索、graph（含 ghost 节点）。
- 前端 vitest：列表/筛选/阅读/链接跳转组件。
- e2e 一条主链路：打开 Memory 页 → 浏览某 agent 记忆 → 点纠错 → 落到预填好的
  新 session。
- 四道门（pytest / vitest / tsc / e2e）照常全绿。

## 不做清单

- **UI 直接编辑/删除记忆**——一切修改走委托 agent 自改，保 agent 自治与索引一致性。
- **file-watcher / WS 实时推送**——重拉足够。
- **跨 agent 链接解析、全局混图**——链接是 agent 内命名空间，混图只会更难读。
- **任何记忆写 API**。
- **搜索索引引擎**（SQLite FTS 等）——语料太小，直扫即可；量级变了再立项。
- **介绍页静态嵌入**——demo 素材用录屏解决。
