export interface EventPrinciple {
  title: string;
  producer: string;
  durability: "持久化消息" | "瞬时状态" | "条件持久化";
  sequence: string;
  reducer: string;
  store: string;
  renderer: string;
  why: string;
  risk: string;
}

export const EVENT_PRINCIPLES: Record<string, EventPrinciple> = {
  status: {
    title: "会话状态事件",
    producer: "SessionManager",
    durability: "瞬时状态",
    sequence: "不带 seq，每次都应用",
    reducer: `case "status":
  updateSessionStatus(
    sessionId,
    data.status as SessionStatus
  );
  break;`,
    store: "sessions[id].status",
    renderer: "StatusPill / 输入区可用性",
    why: "状态是此刻的控制信号，不是聊天记录。即使内容快照没有变化，running、waiting_approval 和 idle 仍必须立即到达。",
    risk: "若把状态也按消息序号去重，重连后的 idle 可能被误丢弃，界面会永久停在“生成中”。",
  },
  user_message: {
    title: "用户消息事件",
    producer: "SessionManager + Database",
    durability: "持久化消息",
    sequence: "服务端分配单调递增 seq",
    reducer: `case "user_message":
  addMessage(sessionId, {
    role: "user",
    type: "text",
    content: data.content as string,
    seq: seq ?? undefined,
  });
  break;`,
    store: "messages[sessionId][]",
    renderer: "MessageBubble · user",
    why: "前端不乐观插入用户消息；统一等待服务端广播，避免“本地一条、广播又一条”的双写竞争。",
    risk: "若前端先插入又不做关联去重，同一条提问会在慢网或重连后出现两次。",
  },
  assistant_text: {
    title: "助手文本事件",
    producer: "CLI stream-json parser",
    durability: "持久化消息",
    sequence: "服务端分配单调递增 seq",
    reducer: `case "assistant_text":
  addMessage(sessionId, {
    role: "assistant",
    type: "text",
    content: data.content as string,
  });
  break;`,
    store: "messages[sessionId][]",
    renderer: "MessageBubble · Markdown",
    why: "流里的每个文本块先被标准化成统一 Message，再交给渲染层；UI 不需要理解 Claude 或 Codex 的原始输出格式。",
    risk: "若解析器格式直接泄露到组件，换模型后整个聊天界面都要跟着改。",
  },
  tool_use: {
    title: "工具调用事件",
    producer: "Harness tool-use parser",
    durability: "持久化消息",
    sequence: "服务端分配单调递增 seq",
    reducer: `case "tool_use":
  addMessage(sessionId, {
    role: "assistant",
    type: "tool_use",
    tool_name: data.tool as string,
    tool_input: data.input,
    tool_use_id: data.tool_use_id,
  });
  break;`,
    store: "messages[sessionId][]",
    renderer: "ToolUseBlock",
    why: "工具意图先成为可审计的消息，再由审批或执行链路继续。用户能看到模型打算做什么，而不是只看到最终结果。",
    risk: "若只保留 tool_result，失败时无法回答“模型当时究竟请求了什么”。",
  },
  tool_approval_request: {
    title: "人工审批事件",
    producer: "Permission gate",
    durability: "瞬时状态",
    sequence: "不带 seq，等待实时决策",
    reducer: `case "tool_approval_request":
  addMessage(sessionId, approval);
  updateSessionStatus(
    sessionId,
    "waiting_approval"
  );
  break;`,
    store: "messages[] + sessions.status",
    renderer: "ToolApproval",
    why: "审批不是一段提示文字，而是状态机里的硬关卡。执行链路必须停在 waiting_approval，直到收到明确允许或拒绝。",
    risk: "只有视觉弹窗、没有状态约束时，后台工具仍可能继续执行，安全提示就成了装饰。",
  },
  tool_result: {
    title: "工具结果事件",
    producer: "Tool runtime",
    durability: "持久化消息",
    sequence: "服务端分配单调递增 seq",
    reducer: `case "tool_result":
  addMessage(sessionId, {
    role: "tool",
    type: "tool_result",
    content: data.output as string,
    is_error: data.is_error as boolean,
  });
  break;`,
    store: "messages[sessionId][]",
    renderer: "ToolResultBlock",
    why: "成功与失败使用同一事件形状，以 is_error 明确区分；模型与用户都能基于同一份执行证据继续判断。",
    risk: "把 stderr 当普通文本会让失败看起来像成功，后续模型可能在错误前提上继续操作。",
  },
  question_request: {
    title: "用户追问事件",
    producer: "AskUserQuestion bridge",
    durability: "条件持久化",
    sequence: "问题消息持久化；待回答状态实时保存",
    reducer: `case "question_request":
  addMessage(sessionId, questionMessage);
  addPendingQuestion(sessionId, {
    question_id: questionId,
    questions,
  });
  break;`,
    store: "messages[] + pendingQuestions[]",
    renderer: "QuestionPrompt",
    why: "问题内容属于历史，是否仍待回答属于当前状态。两者分开，回答后仍能回看当时问了什么。",
    risk: "若只存表单状态，回答后问题会从历史中消失；若只存消息，刷新后无法恢复可交互表单。",
  },
  question_answer: {
    title: "用户回答事件",
    producer: "Question API / WebSocket",
    durability: "持久化消息",
    sequence: "服务端分配单调递增 seq",
    reducer: `case "question_answer":
  addMessage(sessionId, answerMessage);
  removePendingQuestion(
    sessionId,
    questionId
  );
  break;`,
    store: "messages[] − pendingQuestions[]",
    renderer: "MessageBubble · answer",
    why: "一次事件同时留下回答证据并关闭交互状态，避免“答案显示了，但表单还在”等半完成界面。",
    risk: "两次独立更新若顺序失控，用户可能重复提交同一个问题。",
  },
  error: {
    title: "错误与限额事件",
    producer: "Harness / credential / limit layer",
    durability: "条件持久化",
    sequence: "错误消息可持久化；恢复控制为瞬时",
    reducer: `case "error":
  addMessage(sessionId, errorMessage);
  if (data.code === "limit_paused") {
    setParkedTurn(sessionId, {
      resumeAt: data.resume_at,
      limitKind: data.limit_kind,
    });
  }
  break;`,
    store: "messages[] + parkedTurns[id]",
    renderer: "Error sheet + auto-resume banner",
    why: "限额不是普通失败：任务的上下文仍然有效，只是执行权被暂时停放。因此既要留下错误证据，也要保留可恢复状态。",
    risk: "若把 429 当终局错误，用户只能重新发送并可能重复执行此前已经成功的工具。",
  },
  limit_resumed: {
    title: "限额恢复事件",
    producer: "ParkedTurnRunner",
    durability: "瞬时状态",
    sequence: "不带 seq，每次都应用",
    reducer: `case "limit_resumed":
  clearParkedTurn(sessionId);
  break;`,
    store: "parkedTurns − sessionId",
    renderer: "移除 auto-resume banner",
    why: "恢复事件只改变控制面，不伪造新的聊天内容；原任务沿已有上下文继续。",
    risk: "若恢复依赖前端倒计时自行猜测，睡眠标签页、时钟偏差或后台恢复失败都会造成假状态。",
  },
  result: {
    title: "本轮结算事件",
    producer: "HarnessRun",
    durability: "持久化消息",
    sequence: "服务端分配单调递增 seq",
    reducer: `case "result":
  addMessage(sessionId, {
    role: "system",
    type: "result",
    cost: data.cost as number,
    session_id: data.claude_session_id,
  });
  break;`,
    store: "messages[sessionId][]",
    renderer: "Done / cost badge",
    why: "内容结束和会话状态 idle 是两个信号：result 记录本轮结算，status 决定控制权是否真的已经释放。",
    risk: "只看到最后一段文本就认为结束，会在工具回收或队列推进尚未完成时提前开放输入。",
  },
};

export const EVENT_CONTRACTS = [
  { family: "内容", events: "user_message · assistant_text", persisted: "是", seq: "是", purpose: "构成可重放的对话历史" },
  { family: "工具", events: "tool_use · tool_result", persisted: "是", seq: "是", purpose: "留下意图与执行结果证据" },
  { family: "人机边界", events: "tool_approval_request", persisted: "条件", seq: "否", purpose: "把执行链路停在人类关卡" },
  { family: "追问", events: "question_request · question_answer", persisted: "消息是", seq: "是", purpose: "历史与待回答状态分离" },
  { family: "控制", events: "status · limit_resumed", persisted: "否", seq: "否", purpose: "表达此刻，而不是历史" },
  { family: "异常", events: "error", persisted: "条件", seq: "条件", purpose: "区分终局失败与可恢复暂停" },
  { family: "结算", events: "result", persisted: "是", seq: "是", purpose: "记录成本和模型会话锚点" },
];

export const DESIGN_DECISIONS = [
  {
    decision: "服务端分配 seq",
    why: "只有服务端同时看见数据库提交和广播顺序，能定义全客户端一致的消息身份。",
    cost: "每个持久化事件都要经过统一写入边界。",
    boundary: "多区域写入时需要升级为全局日志位置或分区序号。",
  },
  {
    decision: "瞬时状态不带 seq",
    why: "running / idle 表达当前控制权，旧快照不能证明它已经发生过。",
    cost: "消费者必须保证这些更新天然幂等。",
    boundary: "需要审计状态历史时，应另建状态日志，不能滥用聊天消息表。",
  },
  {
    decision: "Zustand 作为归一化边界",
    why: "WebSocket 只处理事件，组件只读取 UI 形状，两端不互相理解协议细节。",
    cost: "单例 Store 需要严格按 sessionId 分片。",
    boundary: "超大历史仍应虚拟化并从服务端分页，不能无限堆在内存。",
  },
  {
    decision: "服务端广播用户消息",
    why: "发送端和其他标签页走同一来源，消除乐观插入与广播的双写竞争。",
    cost: "极慢网络下，用户消息出现会比本地乐观渲染晚一个往返。",
    boundary: "若追求零延迟，需要 client_message_id 和确认合并协议。",
  },
];
