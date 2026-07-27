export type SpecimenEvent = Record<string, unknown> & {
  type: string;
  session_id: string;
  seq?: number;
};

export interface SpecimenScript {
  id: "happy" | "tool" | "question" | "recovery";
  index: string;
  title: string;
  description: string;
  lesson: string;
  events: SpecimenEvent[];
}

export const SPECIMEN_SESSION_ID = "specimen-streaming-session";

const at = (event: Omit<SpecimenEvent, "session_id">): SpecimenEvent =>
  ({
    session_id: SPECIMEN_SESSION_ID,
    ...event,
  }) as SpecimenEvent;

export function makeScripts(now = Date.now()): SpecimenScript[] {
  const resumeAt = new Date(now + 90_000).toISOString();

  return [
    {
      id: "happy",
      index: "01",
      title: "正常流式响应",
      description: "从用户消息到逐步生成，再到一次完整结算。",
      lesson: "界面不是在等待整份答案，而是在消费一串有顺序的事件。",
      events: [
        at({ type: "status", status: "running" }),
        at({ type: "user_message", seq: 1, content: "解释一下为什么天空是蓝色的，控制在三句话。" }),
        at({ type: "assistant_text", seq: 2, content: "阳光看似白色，其实由不同波长的光组成。" }),
        at({ type: "assistant_text", seq: 3, content: "当阳光进入大气层，波长较短的蓝光比红光更容易被空气分子散射到各个方向。" }),
        at({ type: "assistant_text", seq: 4, content: "所以无论我们朝天空的哪个方向看，都会接收到更多散射来的蓝光。" }),
        at({ type: "result", seq: 5, cost: 0.0021, claude_session_id: "demo-happy" }),
        at({ type: "status", status: "idle" }),
      ],
    },
    {
      id: "tool",
      index: "02",
      title: "工具调用与审批",
      description: "模型提出操作，系统暂停，等人类明确放行。",
      lesson: "安全不是一句提示词，而是状态机中一个不可绕过的人类关卡。",
      events: [
        at({ type: "status", status: "running" }),
        at({ type: "user_message", seq: 10, content: "检查项目的 TypeScript 类型是否安全。" }),
        at({ type: "assistant_text", seq: 11, content: "我会先运行只读的类型检查，确认问题发生在哪里。" }),
        at({
          type: "tool_use",
          seq: 12,
          tool: "Bash",
          input: { command: "cd web && npx tsc --noEmit", description: "Type-check the frontend" },
          tool_use_id: "tool-tsc-1",
        }),
        at({
          type: "tool_approval_request",
          tool_name: "Bash",
          tool_input: { command: "cd web && npx tsc --noEmit" },
          tool_use_id: "tool-tsc-1",
        }),
        at({ type: "status", status: "running" }),
        at({
          type: "tool_result",
          seq: 13,
          output: "TypeScript: 0 errors\nChecked 84 source files in 2.3s",
          tool_use_id: "tool-tsc-1",
          is_error: false,
        }),
        at({ type: "assistant_text", seq: 14, content: "类型检查通过，没有发现错误。" }),
        at({ type: "result", seq: 15, cost: 0.0034, claude_session_id: "demo-tool" }),
        at({ type: "status", status: "idle" }),
      ],
    },
    {
      id: "question",
      index: "03",
      title: "向用户追问",
      description: "信息不足时不瞎猜，把选择权交还给用户。",
      lesson: "好的智能系统不仅会执行，也知道什么时候应该停下来提问。",
      events: [
        at({ type: "status", status: "running" }),
        at({ type: "user_message", seq: 20, content: "把这个功能部署出去。" }),
        at({
          type: "question_request",
          seq: 21,
          question_id: "question-host-1",
          questions: [
            {
              header: "访问范围",
              question: "这个标本页面应该先给谁访问？",
              options: [
                { label: "公开预览", description: "任何拿到链接的人都能打开。" },
                { label: "仅自己", description: "先作为本机或私有验收页面。" },
              ],
            },
          ],
        }),
        at({
          type: "question_answer",
          seq: 22,
          question_id: "question-host-1",
          content: "公开预览",
        }),
        at({ type: "assistant_text", seq: 23, content: "明白。我会采用无需自购域名的公开预览地址，并保留以后绑定域名的能力。" }),
        at({ type: "result", seq: 24, cost: 0.0018, claude_session_id: "demo-question" }),
        at({ type: "status", status: "idle" }),
      ],
    },
    {
      id: "recovery",
      index: "04",
      title: "限额恢复与事件去重",
      description: "模拟暂停、恢复，以及重连时最危险的重复消息。",
      lesson: "可靠性来自可验证的守卫：重复事件真的被丢弃，暂停任务也能原地恢复。",
      events: [
        at({ type: "status", status: "running" }),
        at({ type: "user_message", seq: 30, content: "继续生成刚才中断的报告。" }),
        at({ type: "assistant_text", seq: 31, content: "我已经恢复上下文，正在续写结论部分。" }),
        at({ type: "assistant_text", seq: 31, content: "这条是重连后重复广播的消息，绝不能再次渲染。" }),
        at({
          type: "error",
          seq: 32,
          code: "limit_paused",
          message: "使用额度暂时耗尽；任务已安全停放。",
          resume_at: resumeAt,
          limit_kind: "five_hour",
        }),
        at({ type: "status", status: "idle" }),
        at({ type: "limit_resumed" }),
        at({ type: "status", status: "running" }),
        at({ type: "assistant_text", seq: 33, content: "额度窗口已恢复。报告继续生成，而且没有重复上一段内容。" }),
        at({ type: "result", seq: 34, cost: 0.0042, claude_session_id: "demo-recovery" }),
        at({ type: "status", status: "idle" }),
      ],
    },
  ];
}
