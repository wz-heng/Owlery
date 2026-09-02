import { useCallback, useEffect, useState } from "react";
import { IconCheck, IconMenu2, IconRefresh, IconX } from "@tabler/icons-react";

import {
  skillsApi,
  type SkillCandidate,
  type SkillCandidateStatus,
} from "../api/skills";
import { useSessionStore } from "../stores/sessionStore";

interface SkillCandidatesPageProps {
  onToggleSidebar?: () => void;
}

const STATUS_TABS: SkillCandidateStatus[] = ["pending", "approved", "rejected"];

/** Skill candidate review queue (experience-consolidation.md §3.4/§5): the
 * hermes-style pending -> diff -> approve/reject shape. A candidate an agent
 * proposed via the `skills` MCP server's `propose` tool sits here until a
 * human reviews it — nothing lands on disk before that (§4: "no
 * auto-generated skill takes effect"). */
export function SkillCandidatesPage({ onToggleSidebar }: SkillCandidatesPageProps) {
  const token = useSessionStore((s) => s.token);

  const [statusFilter, setStatusFilter] = useState<SkillCandidateStatus>("pending");
  const [candidates, setCandidates] = useState<SkillCandidate[]>([]);
  const [loading, setLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [diff, setDiff] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const [rejectNote, setRejectNote] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [acting, setActing] = useState(false);

  const refresh = useCallback(() => setRefreshTick((t) => t + 1), []);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    setLoading(true);
    setListError(null);
    skillsApi
      .listCandidates(token, statusFilter)
      .then((rows) => {
        if (cancelled) return;
        setCandidates(rows);
        // Keep a selection alive across a refresh; otherwise default to the
        // first row so a reviewer never lands on a blank detail pane.
        setSelectedId((prev) =>
          prev && rows.some((r) => r.id === prev) ? prev : rows[0]?.id ?? null
        );
      })
      .catch(() => {
        if (!cancelled) setListError("Failed to load skill candidates.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [token, statusFilter, refreshTick]);

  useEffect(() => {
    if (!token || !selectedId) {
      setDiff(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    skillsApi
      .getCandidate(token, selectedId)
      .then((detail) => {
        if (!cancelled) setDiff(detail.diff);
      })
      .catch(() => {
        if (!cancelled) setDiff(null);
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [token, selectedId, refreshTick]);

  const selected = candidates.find((c) => c.id === selectedId) ?? null;

  const approve = useCallback(async () => {
    if (!token || !selected) return;
    setActing(true);
    setActionError(null);
    try {
      await skillsApi.approve(token, selected.id);
      refresh();
    } catch {
      setActionError("Failed to approve this candidate.");
    } finally {
      setActing(false);
    }
  }, [token, selected, refresh]);

  const reject = useCallback(async () => {
    if (!token || !selected || !rejectNote.trim()) return;
    setActing(true);
    setActionError(null);
    try {
      await skillsApi.reject(token, selected.id, rejectNote.trim());
      setRejectNote("");
      refresh();
    } catch {
      setActionError("Failed to reject this candidate.");
    } finally {
      setActing(false);
    }
  }, [token, selected, rejectNote, refresh]);

  return (
    <main className="skill-candidates-page flex h-full min-h-0 flex-1 flex-col overflow-hidden bg-background">
      <header className="flex shrink-0 items-center gap-2 border-b border-ink-300 bg-background/95 px-4 py-3 backdrop-blur md:px-6">
        {onToggleSidebar && (
          <button
            type="button"
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg text-muted-foreground hover:bg-ink-200 md:hidden"
            onClick={onToggleSidebar}
            aria-label="Open sidebar"
          >
            <IconMenu2 size={18} />
          </button>
        )}
        <h1 className="font-serif text-base font-semibold shrink-0">Skill candidates</h1>
        <div className="ml-2 flex items-center gap-1" role="tablist" aria-label="Status filter">
          {STATUS_TABS.map((status) => (
            <button
              key={status}
              type="button"
              role="tab"
              aria-selected={statusFilter === status}
              className={`inline-flex h-8 items-center rounded-lg border px-2.5 text-xs font-medium capitalize transition-colors ${
                statusFilter === status
                  ? "border-primary-700 bg-primary-700 text-white"
                  : "border-ink-300 bg-card text-muted-foreground hover:bg-ink-100"
              }`}
              onClick={() => setStatusFilter(status)}
            >
              {status}
            </button>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-1.5">
          <button
            type="button"
            className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-ink-300 bg-card text-muted-foreground hover:bg-ink-100 disabled:opacity-50"
            onClick={refresh}
            disabled={loading}
            aria-label="Refresh"
            title="Refresh"
          >
            <IconRefresh size={15} className={loading ? "animate-spin" : undefined} />
          </button>
        </div>
      </header>

      {listError && (
        <div className="mx-4 mt-2 rounded-lg border border-destructive/30 bg-destructive-surface px-3 py-2 text-xs text-destructive md:mx-6">
          {listError}
        </div>
      )}

      <div className="flex flex-1 min-h-0">
        <div className="w-72 shrink-0 overflow-y-auto border-r border-ink-300 p-1.5">
          {candidates.length === 0 && !loading ? (
            <p className="p-3 text-xs text-muted-foreground">No {statusFilter} candidates.</p>
          ) : (
            candidates.map((candidate) => (
              <button
                key={candidate.id}
                type="button"
                aria-current={candidate.id === selectedId ? "true" : undefined}
                className={`block w-full rounded-lg px-2.5 py-2 text-left transition-colors ${
                  candidate.id === selectedId
                    ? "bg-primary-50 text-foreground"
                    : "text-foreground/85 hover:bg-ink-100"
                }`}
                onClick={() => setSelectedId(candidate.id)}
              >
                <div className="truncate text-sm font-medium">{candidate.title}</div>
                <div className="truncate text-xs text-muted-foreground">{candidate.slug}</div>
                {candidate.status === "approved" && (
                  <div className="mt-0.5 text-[11px] text-muted-foreground">
                    used {candidate.use_count}×
                    {candidate.last_used_at ? ` · last ${new Date(candidate.last_used_at).toLocaleString()}` : ""}
                  </div>
                )}
              </button>
            ))
          )}
        </div>

        <div className="min-w-0 flex-1 overflow-y-auto p-4 md:p-6">
          {!selected ? (
            <p className="text-sm text-muted-foreground">Select a candidate to review it.</p>
          ) : (
            <div className="max-w-3xl space-y-4">
              <div>
                <h2 className="font-serif text-lg font-semibold">{selected.title}</h2>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  slug: {selected.slug} · repository: {selected.repository}
                </p>
              </div>

              <section>
                <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  Description
                </h3>
                <p className="mt-1 text-sm">{selected.description}</p>
              </section>

              <section>
                <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  Rationale
                </h3>
                <p className="mt-1 text-sm">{selected.rationale}</p>
              </section>

              <section>
                <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  Diff
                </h3>
                {detailLoading ? (
                  <p className="mt-1 text-xs text-muted-foreground">Loading…</p>
                ) : (
                  <pre className="mt-1 max-h-96 overflow-auto rounded-lg border border-ink-300 bg-card p-3 text-xs whitespace-pre-wrap">
                    {diff || "(no diff)"}
                  </pre>
                )}
              </section>

              {selected.status === "approved" && (
                <section className="rounded-lg border border-ink-300 bg-card p-3 text-xs">
                  <div>landed at <code>{selected.landed_path}</code></div>
                  <div>branch <code>{selected.landed_branch}</code></div>
                  <div>use count: {selected.use_count}</div>
                </section>
              )}
              {selected.status === "rejected" && selected.review_note && (
                <section className="rounded-lg border border-ink-300 bg-card p-3 text-xs">
                  reviewer note: {selected.review_note}
                </section>
              )}

              {actionError && (
                <div className="flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive-surface px-3 py-2 text-xs text-destructive">
                  <IconX size={14} className="shrink-0" />
                  <span className="flex-1">{actionError}</span>
                </div>
              )}

              {selected.status === "pending" && (
                <div className="flex flex-col gap-2 border-t border-ink-300 pt-3">
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-primary-700 px-3 text-sm font-medium text-white hover:bg-primary-800 disabled:opacity-50"
                      onClick={approve}
                      disabled={acting}
                    >
                      <IconCheck size={15} /> Approve
                    </button>
                  </div>
                  <div className="flex items-center gap-2">
                    <input
                      className="h-9 flex-1 rounded-lg border border-ink-300 bg-card px-2.5 text-sm outline-none focus:border-primary"
                      value={rejectNote}
                      onChange={(e) => setRejectNote(e.target.value)}
                      placeholder="Reason for rejecting"
                      aria-label="Reason for rejecting"
                    />
                    <button
                      type="button"
                      className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-destructive/40 px-3 text-sm font-medium text-destructive hover:bg-destructive-surface disabled:opacity-50"
                      onClick={reject}
                      disabled={acting || !rejectNote.trim()}
                    >
                      <IconX size={15} /> Reject
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </main>
  );
}
