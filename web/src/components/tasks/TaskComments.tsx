import { useState } from "react";
import { IconMessage, IconSend } from "@tabler/icons-react";

import type { TaskComment } from "../../api/tasks";
import type { Agent } from "../../stores/sessionStore";
import { AgentSeal } from "../ui/seal";
import { formatDate } from "./taskPresentation";

interface TaskCommentsProps {
  comments: TaskComment[];
  agents: Agent[];
  busy: boolean;
  onComment: (body: string) => Promise<boolean>;
}
export function TaskComments({ comments, agents, busy, onComment }: TaskCommentsProps) {
  const [body, setBody] = useState("");
  const agentMap = new Map(agents.map((agent) => [agent.id, agent]));
  return (
    <section aria-labelledby="task-comments-title">
      <h3 id="task-comments-title" className="mb-3 flex items-center gap-2 font-serif text-base font-semibold">
        <IconMessage size={17} /> Comments <span className="text-xs font-normal text-muted-foreground">{comments.length}</span>
      </h3>
      <div className="space-y-3">
        {comments.map((comment) => {
          const agent = comment.author_agent_id ? agentMap.get(comment.author_agent_id) : undefined;
          return (
            <article key={comment.id} className="rounded-xl border border-ink-300 bg-card p-3">
              <header className="mb-2 flex items-center gap-2 text-xs text-muted-foreground">
                {agent && <AgentSeal agent={agent} scale="chip" />}
                <strong className="font-medium text-foreground">{agent?.name ?? (comment.author_kind === "user" ? "You" : "Owlery")}</strong>
                <span className="ml-auto">{formatDate(comment.created_at)}</span>
              </header>
              <p className="whitespace-pre-wrap text-sm leading-5 text-ink-800">{comment.body}</p>
            </article>
          );
        })}
        {comments.length === 0 && <p className="rounded-xl bg-ink-100 p-4 text-center text-xs italic text-muted-foreground">No comments yet.</p>}
      </div>
      <form
        className="mt-3 flex items-end gap-2"
        onSubmit={async (event) => {
          event.preventDefault();
          if (await onComment(body)) setBody("");
        }}
      >
        <textarea
          className="task-input min-h-20 flex-1 resize-y py-2"
          value={body}
          onChange={(event) => setBody(event.target.value)}
          placeholder="Add context, a decision, or review note…"
          aria-label="New task comment"
        />
        <button type="submit" className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-primary-700 px-3 text-xs font-semibold text-white disabled:opacity-50" disabled={busy || !body.trim()}>
          <IconSend size={15} /> Send
        </button>
      </form>
    </section>
  );
}
