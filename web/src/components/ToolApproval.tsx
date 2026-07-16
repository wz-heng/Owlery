import { IconAlertTriangle } from "@tabler/icons-react";
import type { Message } from "../stores/sessionStore";
import { Button } from "./ui/button";
import { SheetCard } from "./ui/sheet-card";

interface Props {
  message: Message;
  onApprove: (toolUseId: string) => void;
  onDeny: (toolUseId: string) => void;
}

export function ToolApproval({ message, onApprove, onDeny }: Props) {
  const toolUseId = message.tool_use_id;
  if (!toolUseId) return null;

  // Plum wax. `attention` is the "waiting on a human" state and this is
  // the canonical one — the seal makes that legible at a glance without
  // touching the round-1 colour semantics.
  return (
    <SheetCard
      className="msg msg-approval"
      tone="attention"
      side="left"
      glyph={<IconAlertTriangle />}
      title="Approval needed"
      meta={message.tool_name}
    >
      <div className="approval-header text-sm">
        <strong className="text-foreground">{message.tool_name}</strong>{" "}
        <span className="text-muted-foreground">wants to execute:</span>
      </div>
      <pre className="approval-detail mt-2 rounded-md border border-attention/25 bg-card/70 px-3 py-2 text-xs font-mono text-foreground overflow-x-auto whitespace-pre-wrap break-words max-h-60 overflow-y-auto">
        {JSON.stringify(message.tool_input, null, 2)}
      </pre>
      <div className="approval-actions mt-3 flex gap-2">
        <Button
          className="btn btn-approve"
          size="sm"
          onClick={() => onApprove(toolUseId)}
        >
          Allow
        </Button>
        <Button
          className="btn btn-deny"
          variant="outline"
          size="sm"
          onClick={() => onDeny(toolUseId)}
        >
          Deny
        </Button>
      </div>
    </SheetCard>
  );
}
