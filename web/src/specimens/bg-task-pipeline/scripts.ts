export type BgScenarioId = "success" | "cancel" | "spill" | "watchdog";

export type BgSpecimenEventType =
  | "user_prompt"
  | "tool_use"
  | "bg_started"
  | "turn_closed"
  | "worker_output"
  | "cancel_requested"
  | "watchdog_fired"
  | "bg_completed"
  | "rest_hydrated"
  | "prompt_spilled"
  | "result_injected"
  | "followup_reply";

export interface BgSpecimenEvent {
  type: BgSpecimenEventType;
  actor: "User" | "Model" | "MCP" | "Manager" | "Worker" | "WebSocket" | "Queue";
  content?: string;
  status?: "completed" | "failed" | "cancelled" | "interrupted";
  exitCode?: number;
  stdout?: string;
  stderr?: string;
  bytes?: number;
  truncated?: boolean;
}

export interface BgScript {
  id: BgScenarioId;
  index: string;
  title: string;
  description: string;
  lesson: string;
  events: BgSpecimenEvent[];
}

export const BG_SESSION_ID = "specimen-bg-pipeline";
export const BG_TASK_ID = "bg-a81c43f29d10";
export const BG_COMMAND = "bun run test:e2e:fast";

const base = (): BgSpecimenEvent[] => [
  { type: "user_prompt", actor: "User", content: "运行完整 E2E；结束后告诉我结果，但别堵住当前对话。" },
  { type: "tool_use", actor: "Model", content: BG_COMMAND },
  { type: "bg_started", actor: "Manager", content: "创建持久记录，启动独立进程组，并立即返回 task_id。" },
  { type: "turn_closed", actor: "Model", content: "当前模型进程已经结束；后台任务仍由 FastAPI 进程持有。" },
];

export const BG_SCRIPTS: BgScript[] = [
  {
    id: "success",
    index: "01",
    title: "跨轮次完成",
    description: "模型 turn 先结束，命令继续跑，完成后自动唤起下一轮。",
    lesson: "真正的后台任务必须拥有比单次模型进程更长的生命周期。",
    events: [
      ...base(),
      { type: "worker_output", actor: "Worker", content: "74/76 tests passed…", bytes: 18420 },
      { type: "bg_completed", actor: "WebSocket", status: "completed", exitCode: 0, content: "终态先广播，让聊天里的真状态卡立即翻转。" },
      { type: "rest_hydrated", actor: "MCP", status: "completed", exitCode: 0, stdout: "76 passed (1.6m)\nAll deterministic E2E checks completed.", content: "完整输出按需从 REST 读取，不塞进每条 WebSocket 事件。" },
      { type: "result_injected", actor: "Queue", status: "completed", exitCode: 0, stdout: "76 passed (1.6m)", content: "合成 [bg-task-result] 用户 turn，通过 start_message 排队进入原会话。" },
      { type: "followup_reply", actor: "Model", content: "全量确定性 E2E 76/76 通过；没有发现回归。" },
    ],
  },
  {
    id: "cancel",
    index: "02",
    title: "主动取消",
    description: "用户取消后，SIGTERM 作用于整个进程组，仍然交付终态。",
    lesson: "取消不是隐藏卡片；它必须杀掉后代进程，并给模型一次收口机会。",
    events: [
      ...base(),
      { type: "worker_output", actor: "Worker", content: "browser shard 1/3 running…", bytes: 2300 },
      { type: "cancel_requested", actor: "User", content: "用户点击 Cancel；manager 设置 cancel_requested 后向进程组发送 SIGTERM。" },
      { type: "bg_completed", actor: "WebSocket", status: "cancelled", exitCode: -15, content: "cancelled 与 interrupted 分开，避免把用户意图误报成系统故障。" },
      { type: "rest_hydrated", actor: "MCP", status: "cancelled", exitCode: -15, stdout: "browser shard 1/3 running…", content: "停止前已经产生的尾部输出仍可审计。" },
      { type: "result_injected", actor: "Queue", status: "cancelled", exitCode: -15, content: "取消同样是 terminal state，因此仍触发回流 turn。" },
      { type: "followup_reply", actor: "Model", content: "测试已按你的要求停止；停止前正在运行第一个浏览器分片。" },
    ],
  },
  {
    id: "spill",
    index: "03",
    title: "巨量输出溢写",
    description: "结果超过 argv 安全线时写入文件，只把可读取的指针交给模型。",
    lesson: "大输出不能硬塞进下一次 CLI 参数；传递位置比传递内容更可靠。",
    events: [
      ...base(),
      { type: "worker_output", actor: "Worker", content: "生成 120,421 bytes 测试日志。", bytes: 120421 },
      { type: "bg_completed", actor: "WebSocket", status: "completed", exitCode: 0, content: "任务正常完成；输出仍保存在 bg_tasks 持久行。" },
      { type: "rest_hydrated", actor: "MCP", status: "completed", exitCode: 0, stdout: "…[120 KB output]\nSPILL-OK-83472", content: "UI 展开卡片时才按需取完整输出。" },
      { type: "prompt_spilled", actor: "Queue", bytes: 120421, content: "超过 100 KB：原子写入 large-prompts/<session>/<uuid>.txt，并保留路由 marker。" },
      { type: "result_injected", actor: "Queue", status: "completed", exitCode: 0, content: "模型只收到 [owlery-large-prompt] 指针，随后自行 Read 文件。" },
      { type: "followup_reply", actor: "Model", content: "已读取溢写日志；末尾哨兵 SPILL-OK-83472 存在，任务成功。" },
    ],
  },
  {
    id: "watchdog",
    index: "04",
    title: "静默看门狗",
    description: "命令曾有输出、随后静默卡死；60 秒后自动清理并标成 interrupted。",
    lesson: "看似完成却不退出的进程，比明确失败更需要系统级收口。",
    events: [
      ...base(),
      { type: "worker_output", actor: "Worker", content: "tests completed; waiting for teardown…", bytes: 9180 },
      { type: "watchdog_fired", actor: "Manager", content: "已有输出后静默超过 60 秒：SIGTERM，5 秒后仍不退出则 SIGKILL。" },
      { type: "bg_completed", actor: "WebSocket", status: "interrupted", exitCode: -15, content: "非用户取消的信号终止归类为 interrupted，而不是命令 failed。" },
      { type: "rest_hydrated", actor: "MCP", status: "interrupted", exitCode: -15, stdout: "tests completed; waiting for teardown…", content: "保留卡死前最后输出，便于定位 teardown 问题。" },
      { type: "result_injected", actor: "Queue", status: "interrupted", exitCode: -15, content: "中断结果仍进入会话，让模型解释并决定是否重跑。" },
      { type: "followup_reply", actor: "Model", content: "任务主体已完成但退出阶段卡死，系统已清理进程；建议检查 teardown。" },
    ],
  },
];
