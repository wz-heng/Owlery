import { useEffect, useState, type ReactNode } from "react";
import {
  IconAlertTriangle,
  IconExternalLink,
  IconGitBranch,
  IconGitCommit,
  IconGitMerge,
  IconGitPullRequest,
  IconTrash,
  IconTrashX,
  IconUpload,
} from "@tabler/icons-react";

import type {
  DeliveryRetention,
  DeliveryOpState,
  DeliveryStatus,
  TaskDelivery,
  TaskDeliveryOp,
  TaskRun,
} from "../../api/tasks";
import {
  useTaskStore,
  type DeliveryConfirmation,
} from "../../stores/taskStore";
import { cn } from "../../lib/utils";
import {
  DELIVERY_TONE_PILL,
  deliveryButtonState,
  deliveryStatusTone,
  formatDate,
} from "./taskPresentation";

/** Tailwind pill classes keyed by the delivery lifecycle status.  Shares the
 * board card / tree colour language via `deliveryStatusTone`. */
function statusPill(status: DeliveryStatus): string {
  return DELIVERY_TONE_PILL[deliveryStatusTone(status)];
}

/** Tailwind pill classes keyed by an op's execution state. */
function opPill(state: DeliveryOpState): string {
  switch (state) {
    case "succeeded":
      return "bg-success-surface text-success";
    case "running":
      return "bg-attention-surface text-attention";
    case "failed":
    case "interrupted":
      return "bg-destructive-surface text-destructive";
    default:
      return "bg-ink-200 text-muted-foreground";
  }
}

function shortSha(value: string | null | undefined): string {
  return value ? value.slice(0, 10) : "—";
}

function Field({ label, value, title }: { label: string; value: ReactNode; title?: string }) {
  return (
    <div className="min-w-0">
      <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className="truncate text-xs text-ink-800" title={title}>{value}</p>
    </div>
  );
}

interface DeliveryActionButtonProps {
  label: string;
  icon: ReactNode;
  onClick: () => void;
  disabled: boolean;
  destructive?: boolean;
  /** Shown as the native tooltip. Every disabled action must carry one
   * (task-board-gaps.md §3.5 — "each greyed-out action can answer why"). */
  title?: string;
}
function DeliveryActionButton({ label, icon, onClick, disabled, destructive, title }: DeliveryActionButtonProps) {
  return (
    <button
      type="button"
      className={cn(
        "inline-flex h-8 items-center gap-1.5 rounded-lg border px-3 text-xs font-medium disabled:opacity-40 disabled:cursor-not-allowed",
        destructive
          ? "border-destructive/30 text-destructive hover:bg-destructive-surface"
          : "border-ink-300 text-ink-800 hover:border-primary/40 hover:bg-primary-50"
      )}
      disabled={disabled}
      title={title}
      onClick={onClick}
    >
      {icon} {label}
    </button>
  );
}

/** Panel-wide "why is everything grey right now" reason, taking priority over
 * a per-button reason since NONE of them are individually actionable while
 * another op is in flight. */
const BUSY_REASON = "A delivery action is already running";

interface TaskDeliveryPanelProps {
  run: TaskRun;
  onOpenTask?: (taskId: string) => void;
}

export function TaskDeliveryPanel({ run, onOpenTask }: TaskDeliveryPanelProps) {
  const taskId = run.task_id;
  const runId = run.id;
  const delivery = useTaskStore((state) => state.deliveries[runId]) as TaskDelivery | undefined;
  const chain = useTaskStore((state) => state.deliveryChains[runId]);
  const ops = useTaskStore((state) =>
    delivery ? state.deliveryOps[delivery.id] : undefined
  );
  const mutating = useTaskStore((state) => state.mutating);
  const confirmation = useTaskStore((state) => state.deliveryConfirmation);
  const [retention, setRetention] = useState<DeliveryRetention>(
    delivery?.retention ?? "keep"
  );
  const [teardownAllBusy, setTeardownAllBusy] = useState(false);
  const store = useTaskStore.getState();

  useEffect(() => {
    void useTaskStore.getState().loadDelivery(taskId, runId);
    void useTaskStore.getState().loadDeliveryChain(taskId, runId);
  }, [taskId, runId]);

  useEffect(() => {
    if (delivery) setRetention(delivery.retention);
  }, [delivery?.retention]);

  if (delivery?.superseded_by_delivery_id) {
    const target = chain?.target;
    return (
      <section className="mt-4 rounded-xl border border-ink-300 bg-ink-50 p-3" aria-label="Git delivery">
        <p className="flex flex-wrap items-center gap-1.5 text-xs text-ink-800">
          <IconGitMerge size={14} className="shrink-0 text-muted-foreground" />
          <span>
            Collapsed — delivered by{" "}
            {target ? (
              <button
                type="button"
                className="font-medium text-primary-700 hover:underline"
                onClick={() => onOpenTask?.(target.task_id)}
              >
                {target.task_title}
              </button>
            ) : (
              "another task"
            )}
            's delivery, which already contains every commit on this branch.
          </span>
        </p>
      </section>
    );
  }

  if (!delivery) {
    return (
      <section className="mt-4 rounded-xl border border-ink-300 bg-ink-50 p-3" aria-label="Git delivery">
        <header className="flex flex-wrap items-center gap-2">
          <span className="inline-flex items-center gap-1.5 text-sm font-semibold">
            <IconGitBranch size={15} /> Git delivery
          </span>
          <span className="rounded-full bg-ink-200 px-2 py-0.5 text-[10px] font-semibold uppercase text-muted-foreground">
            Not accepted
          </span>
        </header>
        <p className="mt-2 text-xs text-muted-foreground">
          Capture the immutable base and current worktree evidence before any commit, push, or pull request.
        </p>
        <div className="mt-3 border-t border-ink-300 pt-3">
          <DeliveryActionButton
            label="Accept"
            icon={<IconGitBranch size={14} />}
            disabled={mutating}
            title={mutating ? BUSY_REASON : undefined}
            onClick={() => void store.acceptDelivery(taskId, runId)}
          />
        </div>
      </section>
    );
  }

  const status = delivery.status;
  const busy = mutating || status === "preparing" || status === "delivering";
  const accept = deliveryButtonState("accept", delivery);
  const commit = deliveryButtonState("commit", delivery);
  const push = deliveryButtonState("push", delivery);
  const pullRequest = deliveryButtonState("pull_request", delivery);
  const merge = deliveryButtonState("merge", delivery);
  const teardown = deliveryButtonState("teardown", delivery);
  const reasonFor = (state: { reason: string | null }) => (busy ? BUSY_REASON : state.reason ?? undefined);

  const opLog = ops ?? [];
  const superseded = chain?.superseded ?? [];
  // A batch teardown resolves confirmations one entry at a time (§3.1's
  // "reuse the existing per-delivery confirmation" rule) — the pending
  // confirmation may belong to any superseded run, not just this tip's own.
  const activeConfirmation =
    confirmation &&
    (confirmation.runId === runId || superseded.some((entry) => entry.run_id === confirmation.runId))
      ? confirmation
      : null;

  return (
    <section className="mt-4 rounded-xl border border-ink-300 bg-ink-50 p-3" aria-label="Git delivery">
      <header className="flex flex-wrap items-center gap-2">
        <span className="inline-flex items-center gap-1.5 text-sm font-semibold">
          <IconGitBranch size={15} /> Git delivery
        </span>
        <span className={cn("rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase", statusPill(status))}>
          {status}
        </span>
        {delivery.dirty && (
          <span className="rounded-full bg-attention-surface px-2 py-0.5 text-[10px] font-semibold uppercase text-attention">
            Uncommitted
          </span>
        )}
        {delivery.pr_url && (
          <a
            className="ml-auto inline-flex items-center gap-1 text-xs font-medium text-primary-700 hover:underline"
            href={delivery.pr_url}
            target="_blank"
            rel="noreferrer"
          >
            {delivery.pr_number != null ? `PR #${delivery.pr_number}` : "Pull request"}
            {delivery.pr_state ? ` · ${delivery.pr_state}` : ""} <IconExternalLink size={13} />
          </a>
        )}
      </header>

      <div className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 sm:grid-cols-3">
        <Field label="Repository" value={delivery.repository} title={delivery.repository} />
        <Field
          label="Base"
          value={`${delivery.base_ref ?? "—"} · ${shortSha(delivery.base_head)}`}
          title={delivery.base_ref ?? undefined}
        />
        <Field
          label="Attempt branch"
          value={`${delivery.attempt_branch} · ${shortSha(delivery.attempt_head)}`}
          title={delivery.attempt_branch}
        />
        <Field
          label="Commits ahead"
          value={delivery.commits_ahead != null ? delivery.commits_ahead : "—"}
        />
        <Field
          label="Diffstat"
          value={
            delivery.diffstat
              ? `${delivery.diffstat.files} files · +${delivery.diffstat.insertions} −${delivery.diffstat.deletions}`
              : "—"
          }
        />
        <Field
          label="Remote"
          value={delivery.remote_url || delivery.remote_name || "—"}
          title={delivery.remote_url || delivery.remote_name}
        />
        {delivery.pushed_ref && (
          <Field label="Pushed ref" value={delivery.pushed_ref} title={delivery.pushed_ref} />
        )}
      </div>

      {delivery.reason_detail && (
        <p className="mt-3 flex items-start gap-1.5 rounded-lg bg-destructive-surface p-2 text-xs text-destructive">
          <IconAlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>
            {delivery.reason_kind ? <strong>{delivery.reason_kind}: </strong> : null}
            {delivery.reason_detail}
          </span>
        </p>
      )}

      <div className="mt-3 flex flex-wrap gap-2 border-t border-ink-300 pt-3">
        <DeliveryActionButton
          label="Accept"
          icon={<IconGitBranch size={14} />}
          disabled={busy || !accept.enabled}
          title={reasonFor(accept)}
          onClick={() => void store.acceptDelivery(taskId, runId)}
        />
        <DeliveryActionButton
          label="Commit"
          icon={<IconGitCommit size={14} />}
          disabled={busy || !commit.enabled}
          title={reasonFor(commit)}
          onClick={() => void store.deliveryAction(taskId, runId, "commit")}
        />
        <DeliveryActionButton
          label="Push"
          icon={<IconUpload size={14} />}
          disabled={busy || !push.enabled}
          title={reasonFor(push)}
          onClick={() => void store.deliveryAction(taskId, runId, "push")}
        />
        <DeliveryActionButton
          label="Open PR"
          icon={<IconGitPullRequest size={14} />}
          disabled={busy || !pullRequest.enabled}
          title={reasonFor(pullRequest)}
          onClick={() => void store.deliveryAction(taskId, runId, "pull_request")}
        />
        <DeliveryActionButton
          label="Merge"
          icon={<IconGitMerge size={14} />}
          disabled={busy || !merge.enabled}
          title={reasonFor(merge)}
          onClick={() => void store.deliveryAction(taskId, runId, "merge")}
        />
        <DeliveryActionButton
          label="Teardown"
          icon={<IconTrash size={14} />}
          disabled={busy || !teardown.enabled}
          title={reasonFor(teardown)}
          destructive
          onClick={() => void store.teardownDelivery(taskId, runId, { retention })}
        />
        <label className="ml-auto min-w-44 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
          Retention
          <select
            className="task-input mt-1 h-8 py-0 text-xs normal-case tracking-normal"
            value={retention}
            disabled={busy}
            onChange={(event) => setRetention(event.target.value as DeliveryRetention)}
          >
            <option value="keep">Keep worktree and branch</option>
            <option value="remove_worktree_keep_branch">Remove worktree, keep branch</option>
            <option value="remove_all">Remove worktree and branch</option>
          </select>
        </label>
      </div>

      {superseded.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg bg-ink-100 p-2.5">
          <p className="text-xs text-ink-800">
            This branch has collapsed {superseded.length} earlier{" "}
            {superseded.length === 1 ? "delivery" : "deliveries"} on the same chain.
          </p>
          <DeliveryActionButton
            label="Teardown all collapsed"
            icon={<IconTrashX size={14} />}
            disabled={busy || teardownAllBusy}
            destructive
            onClick={async () => {
              setTeardownAllBusy(true);
              try {
                await store.teardownSuperseded(taskId, runId, { retention });
              } finally {
                setTeardownAllBusy(false);
              }
            }}
          />
        </div>
      )}

      <div className="mt-3">
        <p className="mb-2 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
          Operations <span className="font-normal">{opLog.length}</span>
        </p>
        {opLog.length === 0 ? (
          <p className="rounded-lg bg-ink-100 p-3 text-center text-xs italic text-muted-foreground">
            No delivery operations recorded yet.
          </p>
        ) : (
          <ul className="space-y-1.5">
            {opLog.map((op) => (
              <OpRow key={op.id} op={op} />
            ))}
          </ul>
        )}
      </div>

      {activeConfirmation && (
        <ConfirmationDialog confirmation={activeConfirmation} />
      )}
    </section>
  );
}

function OpRow({ op }: { op: TaskDeliveryOp }) {
  const summary = op.error
    ? op.error
    : op.result
      ? Object.entries(op.result)
          .slice(0, 3)
          .map(([key, value]) => `${key}: ${String(value)}`)
          .join(" · ")
      : "";
  return (
    <li className="rounded-lg border border-ink-200 bg-card px-2.5 py-1.5 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono font-medium text-ink-800">{op.kind}</span>
        {op.external && (
          <span className="rounded bg-ink-200 px-1 text-[9px] uppercase text-muted-foreground">ext</span>
        )}
        <span className={cn("rounded-full px-1.5 py-0.5 text-[9px] font-semibold uppercase", opPill(op.state))}>
          {op.state}
        </span>
        <span className="ml-auto text-[10px] text-muted-foreground">
          {formatDate(op.finished_at ?? op.started_at ?? op.created_at)}
        </span>
      </div>
      {summary && (
        <p className={cn("mt-1 truncate", op.error ? "text-destructive" : "text-muted-foreground")} title={summary}>
          {summary}
        </p>
      )}
    </li>
  );
}

function ConfirmationDialog({ confirmation }: { confirmation: DeliveryConfirmation }) {
  const [phrase, setPhrase] = useState("");
  const mutating = useTaskStore((state) => state.mutating);

  useEffect(() => {
    setPhrase("");
  }, [confirmation]);

  const matched = phrase.trim() === confirmation.verb;

  const resubmit = () => {
    const store = useTaskStore.getState();
    const confirmations = { [confirmation.confirmation]: true };
    store.clearDeliveryConfirmation();
    if (confirmation.action === "accept") {
      void store.acceptDelivery(confirmation.taskId, confirmation.runId, undefined, confirmations);
    } else if (confirmation.action === "teardown") {
      void store.teardownDelivery(confirmation.taskId, confirmation.runId, { confirmations });
    } else {
      void store.deliveryAction(confirmation.taskId, confirmation.runId, confirmation.action, {
        confirmations,
      });
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink-950/25 p-4 backdrop-blur-[1px]"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) useTaskStore.getState().clearDeliveryConfirmation();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Confirm ${confirmation.verb}`}
        className="w-[min(24rem,92vw)] rounded-xl border border-destructive/30 bg-background p-4 shadow-[var(--elevation-overlay)]"
      >
        <p className="flex items-center gap-1.5 text-sm font-semibold text-destructive">
          <IconAlertTriangle size={16} /> Confirm {confirmation.verb}
        </p>
        <p className="mt-2 text-xs text-ink-800">{confirmation.message}</p>
        <label className="mt-3 block text-xs text-muted-foreground">
          Type <code className="rounded bg-ink-100 px-1 font-mono text-ink-800">{confirmation.verb}</code> to confirm
          <input
            className="task-input mt-1 h-8 text-sm"
            value={phrase}
            autoFocus
            onChange={(event) => setPhrase(event.target.value)}
            aria-label="confirmation phrase"
          />
        </label>
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            className="h-8 rounded-lg px-3 text-xs text-muted-foreground hover:bg-ink-200"
            onClick={() => useTaskStore.getState().clearDeliveryConfirmation()}
          >
            Cancel
          </button>
          <button
            type="button"
            className="h-8 rounded-lg bg-destructive px-3 text-xs font-semibold text-white disabled:opacity-40"
            disabled={!matched || mutating}
            onClick={resubmit}
          >
            Confirm
          </button>
        </div>
      </div>
    </div>
  );
}
