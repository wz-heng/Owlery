import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  IconAlertTriangle,
  IconExternalLink,
  IconGitBranch,
  IconGitCommit,
  IconGitMerge,
  IconGitPullRequest,
  IconTrash,
  IconUpload,
} from "@tabler/icons-react";

import type {
  DeliveryRetention,
  DeliveryOpState,
  DeliveryStatus,
  Deployment,
  TaskDelivery,
  TaskDeliveryOp,
  TaskRun,
} from "../../api/tasks";
import { taskApi } from "../../api/tasks";
import {
  useTaskStore,
  type DeliveryConfirmation,
} from "../../stores/taskStore";
import { cn } from "../../lib/utils";
import {
  DELIVERY_TONE_PILL,
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
}
function DeliveryActionButton({ label, icon, onClick, disabled, destructive }: DeliveryActionButtonProps) {
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
      onClick={onClick}
    >
      {icon} {label}
    </button>
  );
}

interface TaskDeliveryPanelProps {
  run: TaskRun;
  allowLocalDeploy: boolean;
}

export function TaskDeliveryPanel({ run, allowLocalDeploy }: TaskDeliveryPanelProps) {
  const taskId = run.task_id;
  const runId = run.id;
  const delivery = useTaskStore((state) => state.deliveries[runId]) as TaskDelivery | undefined;
  const ops = useTaskStore((state) =>
    delivery ? state.deliveryOps[delivery.id] : undefined
  );
  const mutating = useTaskStore((state) => state.mutating);
  const confirmation = useTaskStore((state) => state.deliveryConfirmation);
  const token = useTaskStore((state) => state.token);
  const [retention, setRetention] = useState<DeliveryRetention>(
    delivery?.retention ?? "keep"
  );
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const store = useTaskStore.getState();

  const refreshDeployments = useCallback(async () => {
    if (!allowLocalDeploy || !token) return;
    try {
      const result = await taskApi.deployments(token);
      setDeployments(result.deployments);
    } catch {
      // The normal task-store error surface already reports deploy mutations.
      // A read-only status refresh should not obscure the delivery panel.
    }
  }, [allowLocalDeploy, token]);

  useEffect(() => {
    void useTaskStore.getState().loadDelivery(taskId, runId);
  }, [taskId, runId]);

  useEffect(() => {
    if (delivery) setRetention(delivery.retention);
  }, [delivery?.retention]);

  useEffect(() => {
    void refreshDeployments();
  }, [refreshDeployments]);

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
            onClick={() => void store.acceptDelivery(taskId, runId)}
          />
        </div>
      </section>
    );
  }

  const status = delivery.status;
  const busy = mutating || status === "preparing" || status === "delivering";
  const canAccept = status === "pending";
  const canCommit = status === "ready" && delivery.dirty;
  const canPush = status === "ready" && !delivery.dirty && (delivery.commits_ahead ?? 0) > 0;
  const canOpenPr = !!delivery.pushed_ref && delivery.pr_number == null;
  const canMerge = delivery.pr_number != null && status !== "delivered";
  const canTeardown = (["delivered", "failed", "blocked", "conflicted"] as DeliveryStatus[]).includes(status);
  const canStage = !delivery.dirty && !!delivery.attempt_head && (delivery.commits_ahead ?? 0) > 0
    && (["ready", "delivered", "blocked"] as DeliveryStatus[]).includes(status);
  const staged = deployments.find(
    (deployment) => deployment.delivery_id === delivery.id && deployment.state === "staged"
  );
  const live = deployments.find(
    (deployment) => deployment.delivery_id === delivery.id && deployment.state === "live"
  );

  const opLog = ops ?? [];
  const activeConfirmation =
    confirmation && confirmation.runId === runId ? confirmation : null;

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
          disabled={busy || !canAccept}
          onClick={() => void store.acceptDelivery(taskId, runId)}
        />
        <DeliveryActionButton
          label="Commit"
          icon={<IconGitCommit size={14} />}
          disabled={busy || !canCommit}
          onClick={() => void store.deliveryAction(taskId, runId, "commit")}
        />
        <DeliveryActionButton
          label="Push"
          icon={<IconUpload size={14} />}
          disabled={busy || !canPush}
          onClick={() => void store.deliveryAction(taskId, runId, "push")}
        />
        <DeliveryActionButton
          label="Open PR"
          icon={<IconGitPullRequest size={14} />}
          disabled={busy || !canOpenPr}
          onClick={() => void store.deliveryAction(taskId, runId, "pull_request")}
        />
        <DeliveryActionButton
          label="Merge"
          icon={<IconGitMerge size={14} />}
          disabled={busy || !canMerge}
          onClick={() => void store.deliveryAction(taskId, runId, "merge")}
        />
        <DeliveryActionButton
          label="Teardown"
          icon={<IconTrash size={14} />}
          disabled={busy || !canTeardown}
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

      {allowLocalDeploy && (
        <section className="mt-3 rounded-lg border border-primary-200 bg-primary-50/40 p-3" aria-label="Local deploy">
          <header className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-semibold text-ink-800">Local deploy</span>
            {staged ? (
              <span className="rounded-full bg-attention-surface px-2 py-0.5 text-[10px] font-semibold uppercase text-attention">
                Staged · {shortSha(staged.sha)} · slot {staged.slot}
              </span>
            ) : delivery.deployed_sha ? (
              <span className="rounded-full bg-success-surface px-2 py-0.5 text-[10px] font-semibold uppercase text-success">
                Live · {shortSha(delivery.deployed_sha)} · slot {delivery.deployed_slot}
              </span>
            ) : (
              <span className="text-[11px] text-muted-foreground">Stage a settled delivery into the inactive slot.</span>
            )}
          </header>
          <div className="mt-2 flex flex-wrap gap-2">
            <DeliveryActionButton
              label="Stage"
              icon={<IconUpload size={14} />}
              disabled={busy || !canStage}
              onClick={() => void store.deliveryAction(taskId, runId, "deploy_stage").then(refreshDeployments)}
            />
            <DeliveryActionButton
              label="Switch"
              icon={<IconGitMerge size={14} />}
              disabled={busy || !staged}
              destructive
              onClick={() => void store.deliveryAction(taskId, runId, "deploy_switch").then(refreshDeployments)}
            />
            {live && (
              <DeliveryActionButton
                label="Rollback"
                icon={<IconAlertTriangle size={14} />}
                disabled={busy}
                destructive
                onClick={() => void store.rollbackDeployment(taskId, runId, live.id).then(refreshDeployments)}
              />
            )}
          </div>
        </section>
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
    } else if (confirmation.action === "rollback" && confirmation.deploymentId) {
      void store.rollbackDeployment(
        confirmation.taskId, confirmation.runId, confirmation.deploymentId, true
      );
    } else if (confirmation.action === "rollback") {
      // A malformed rollback confirmation cannot name a deployment, so it must
      // never fall through to a delivery action with a different meaning.
      return;
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
