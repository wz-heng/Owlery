import { useEffect, useState } from "react";
import {
  IconAlertTriangle,
  IconGitBranch,
  IconHistory,
  IconRocket,
} from "@tabler/icons-react";

import type { ReleaseDeployment, TaskBoard } from "../../api/tasks";
import { useTaskStore, type ReleaseConfirmation } from "../../stores/taskStore";
import { cn } from "../../lib/utils";
import { DELIVERY_TONE_PILL, type DeliveryTone, formatDate } from "./taskPresentation";

function shortSha(value: string | null | undefined): string {
  return value ? value.slice(0, 10) : "—";
}

function releaseTone(state: ReleaseDeployment["state"]): DeliveryTone {
  switch (state) {
    case "live":
      return "success";
    case "staged":
    case "staging":
    case "switching":
      return "attention";
    case "failed":
      return "destructive";
    default:
      return "neutral";
  }
}

interface ReleaseActionButtonProps {
  label: string;
  icon: React.ReactNode;
  onClick: () => void;
  disabled: boolean;
  destructive?: boolean;
}
function ReleaseActionButton({ label, icon, onClick, disabled, destructive }: ReleaseActionButtonProps) {
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

interface ReleasePanelProps {
  board: TaskBoard;
}

/** Board-level Releases surface (docs/plans/release-line-deploy.md §3.4): the
 * configured release branch, its staged/live candidates, Stage/Switch with
 * precondition-disabled states and the busy census surfaced on refusal, the
 * version history, and a typed-confirmation Rollback. Rendered only when the
 * board has opted into local deploy — mirrors the per-run panel it replaces. */
export function ReleasePanel({ board }: ReleasePanelProps) {
  const releases = useTaskStore((state) => state.releases[board.id]) ?? [];
  const remoteTip = useTaskStore((state) => state.releaseRemoteTip[board.id]);
  const mutating = useTaskStore((state) => state.mutating);
  const error = useTaskStore((state) => state.error);
  const confirmation = useTaskStore((state) => state.releaseConfirmation);
  const store = useTaskStore.getState();

  useEffect(() => {
    void useTaskStore.getState().loadReleases(board.id);
  }, [board.id]);

  const live = releases.find((release) => release.state === "live");
  const staged = releases.find((release) => release.state === "staged");
  const history = [...releases].sort(
    (a, b) => Date.parse(b.created_at) - Date.parse(a.created_at)
  );
  const busy = mutating;
  const activeConfirmation = confirmation && confirmation.boardId === board.id ? confirmation : null;
  const tipAheadOfLive = remoteTip != null && remoteTip !== live?.sha;

  return (
    <section className="mx-4 mb-2 rounded-xl border border-ink-300 bg-ink-50 p-3 md:mx-6" aria-label="Releases">
      <header className="flex flex-wrap items-center gap-2">
        <span className="inline-flex items-center gap-1.5 text-sm font-semibold">
          <IconRocket size={15} /> Releases
        </span>
        <span className="inline-flex items-center gap-1 rounded-full bg-ink-200 px-2 py-0.5 text-[10px] font-semibold uppercase text-muted-foreground">
          <IconGitBranch size={11} /> {board.deploy_release_ref}
        </span>
        <span
          className={cn(
            "rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase",
            tipAheadOfLive ? DELIVERY_TONE_PILL.attention : "bg-ink-200 text-muted-foreground"
          )}
          title="The remote's current tip for the configured release branch"
        >
          Remote tip {shortSha(remoteTip)}
        </span>
        {live ? (
          <span className={cn("rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase", DELIVERY_TONE_PILL.success)}>
            Live {live.version} · {shortSha(live.sha)}
          </span>
        ) : (
          <span className="text-[11px] text-muted-foreground">No live release yet.</span>
        )}
        {staged && (
          <span className={cn("rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase", DELIVERY_TONE_PILL.attention)}>
            Staged {staged.version} · {shortSha(staged.sha)}
          </span>
        )}
        {tipAheadOfLive && !staged && (
          <span className="text-[11px] text-attention">New commits available to stage.</span>
        )}
      </header>

      {error && (
        <p className="mt-2 flex items-start gap-1.5 rounded-lg bg-destructive-surface p-2 text-xs text-destructive">
          <IconAlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </p>
      )}

      <div className="mt-2 flex flex-wrap gap-2">
        <ReleaseActionButton
          label="Stage"
          icon={<IconGitBranch size={14} />}
          disabled={busy || !board.allow_local_deploy}
          onClick={() => void store.stageRelease(board.id)}
        />
        <ReleaseActionButton
          label="Switch"
          icon={<IconRocket size={14} />}
          disabled={busy || !staged}
          destructive
          onClick={() => void store.switchRelease(board.id)}
        />
        {live && (
          <ReleaseActionButton
            label="Rollback"
            icon={<IconAlertTriangle size={14} />}
            disabled={busy}
            destructive
            onClick={() => void store.rollbackRelease(board.id)}
          />
        )}
      </div>

      {history.length > 0 && (
        <div className="mt-3">
          <p className="mb-1.5 flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
            <IconHistory size={12} /> History <span className="font-normal">{history.length}</span>
          </p>
          <ul className="space-y-1">
            {history.slice(0, 8).map((release) => (
              <li
                key={release.id}
                className="flex flex-wrap items-center gap-2 rounded-lg border border-ink-200 bg-card px-2.5 py-1.5 text-xs"
              >
                <span className="font-mono font-medium text-ink-800">{release.version}</span>
                <span className="font-mono text-muted-foreground">{shortSha(release.sha)}</span>
                <span className={cn("rounded-full px-1.5 py-0.5 text-[9px] font-semibold uppercase", DELIVERY_TONE_PILL[releaseTone(release.state)])}>
                  {release.state}
                </span>
                {release.error && (
                  <span className="text-destructive" title={release.error}>{release.error}</span>
                )}
                <span className="ml-auto text-[10px] text-muted-foreground">{formatDate(release.updated_at)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {activeConfirmation && <ReleaseConfirmationDialog confirmation={activeConfirmation} />}
    </section>
  );
}

function ReleaseConfirmationDialog({ confirmation }: { confirmation: ReleaseConfirmation }) {
  const [phrase, setPhrase] = useState("");
  const mutating = useTaskStore((state) => state.mutating);

  useEffect(() => {
    setPhrase("");
  }, [confirmation]);

  const matched = phrase.trim() === confirmation.verb;

  const resubmit = () => {
    const store = useTaskStore.getState();
    store.clearReleaseConfirmation();
    if (confirmation.action === "rollback") {
      void store.rollbackRelease(confirmation.boardId, true);
    } else if (confirmation.action === "switch") {
      void store.switchRelease(confirmation.boardId);
    } else {
      void store.stageRelease(confirmation.boardId);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink-950/25 p-4 backdrop-blur-[1px]"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) useTaskStore.getState().clearReleaseConfirmation();
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
            onClick={() => useTaskStore.getState().clearReleaseConfirmation()}
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
