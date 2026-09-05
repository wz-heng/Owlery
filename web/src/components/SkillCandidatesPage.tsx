import { useCallback, useEffect, useState } from "react";
import { IconCheck, IconMenu2, IconRefresh, IconX } from "@tabler/icons-react";

import {
  skillsApi,
  type SkillCandidate,
  type SkillCandidateDetail,
  type SkillCandidateScope,
  type SkillCandidateStatus,
} from "../api/skills";
import { useSessionStore } from "../stores/sessionStore";

interface SkillCandidatesPageProps {
  onToggleSidebar?: () => void;
  onOpenSession?: (sessionId: string) => void;
  onOpenTask?: (taskId: string) => void;
}

const STATUS_TABS: SkillCandidateStatus[] = ["pending", "approved", "rejected"];
const SCOPE_LABEL: Record<SkillCandidateScope, string> = {
  "agent-global": "agent-global",
  "agent+repo": "agent + repo",
};

/** Skill candidate review queue (experience-consolidation.md §3.4/§5,
 * experience-consolidation-v2.md §3②). A candidate an agent proposed via the
 * `skills` MCP server's `propose` tool sits here until a human reviews it —
 * nothing lands on disk before that (§4: "no auto-generated skill takes
 * effect"). The detail pane is the full evidence chain: source task/run/
 * session, static lint, a per-file diff over the whole bundle (not just
 * SKILL.md), and — for an approved candidate — its invocation history. */
export function SkillCandidatesPage({
  onToggleSidebar,
  onOpenSession,
  onOpenTask,
}: SkillCandidatesPageProps) {
  const token = useSessionStore((s) => s.token);

  const [statusFilter, setStatusFilter] = useState<SkillCandidateStatus>("pending");
  const [candidates, setCandidates] = useState<SkillCandidate[]>([]);
  const [loading, setLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<SkillCandidateDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [activeFile, setActiveFile] = useState("SKILL.md");

  const [approveScope, setApproveScope] = useState<SkillCandidateScope>("agent+repo");
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
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    skillsApi
      .getCandidate(token, selectedId)
      .then((d) => {
        if (cancelled) return;
        setDetail(d);
        setActiveFile("SKILL.md");
        setApproveScope(d.candidate.scope);
      })
      .catch(() => {
        if (!cancelled) setDetail(null);
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
      await skillsApi.approve(token, selected.id, undefined, approveScope);
      refresh();
    } catch (err) {
      setActionError(
        err instanceof Error ? err.message : "Failed to approve this candidate."
      );
    } finally {
      setActing(false);
    }
  }, [token, selected, approveScope, refresh]);

  const reject = useCallback(async () => {
    if (!token || !selected || !rejectNote.trim()) return;
    setActing(true);
    setActionError(null);
    try {
      await skillsApi.reject(token, selected.id, rejectNote.trim());
      setRejectNote("");
      refresh();
    } catch (err) {
      setActionError(
        err instanceof Error ? err.message : "Failed to reject this candidate."
      );
    } finally {
      setActing(false);
    }
  }, [token, selected, rejectNote, refresh]);

  const fileNames = detail ? Object.keys(detail.file_diffs).sort() : [];
  const activeDiff = detail?.file_diffs[activeFile] ?? detail?.diff ?? "";

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
        <div className="skill-candidate-list w-72 shrink-0 overflow-y-auto border-r border-ink-300 p-1.5">
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
                <div className="truncate text-xs text-muted-foreground">
                  {candidate.slug} · {SCOPE_LABEL[candidate.scope]}
                </div>
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
                  slug: {selected.slug} · repository: {selected.repository} · scope:{" "}
                  {SCOPE_LABEL[selected.scope]}
                </p>
                {selected.materialized_backends && selected.materialized_backends.length > 0 && (
                  <div className="mt-1.5 flex gap-1.5">
                    {selected.materialized_backends.map((backend) => (
                      <span
                        key={backend}
                        className="inline-flex items-center rounded-full border border-ink-300 bg-card px-2 py-0.5 text-[11px] text-muted-foreground"
                      >
                        {backend}
                      </span>
                    ))}
                  </div>
                )}
              </div>

              <section
                className="rounded-lg border border-ink-300 bg-card p-3 text-xs"
                aria-label="Evidence chain"
              >
                <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  Evidence
                </h3>
                <div className="flex flex-wrap gap-x-4 gap-y-1">
                  <span>
                    task:{" "}
                    {detail?.task ? (
                      <button
                        type="button"
                        className="text-primary-700 underline hover:no-underline disabled:cursor-default disabled:text-foreground disabled:no-underline"
                        onClick={() => onOpenTask?.(detail.task!.id)}
                        disabled={!onOpenTask}
                      >
                        {detail.task.title} ({detail.task.status})
                      </button>
                    ) : (
                      "—"
                    )}
                  </span>
                  <span>
                    run:{" "}
                    {detail?.run
                      ? `attempt ${detail.run.attempt_no} (${detail.run.state})`
                      : "—"}
                  </span>
                  <span>
                    session:{" "}
                    {detail?.session ? (
                      <button
                        type="button"
                        className="text-primary-700 underline hover:no-underline disabled:cursor-default disabled:text-foreground disabled:no-underline"
                        onClick={() => onOpenSession?.(detail.session!.id)}
                        disabled={!onOpenSession}
                      >
                        {detail.session.backend}
                      </button>
                    ) : (
                      "—"
                    )}
                  </span>
                </div>
                {selected.lint_results && (
                  <div className="mt-2 border-t border-ink-300 pt-2">
                    <div className="flex flex-wrap gap-x-4 gap-y-1">
                      <span>
                        frontmatter: {selected.lint_results.frontmatter_valid ? "valid" : "invalid"}
                      </span>
                      <span>
                        slug conflict: {selected.lint_results.slug_conflict ? "yes" : "no"}
                      </span>
                      <span>
                        bundle refs: {selected.lint_results.bundle_refs_valid ? "valid" : "invalid"}
                      </span>
                    </div>
                    {selected.lint_results.issues.length > 0 && (
                      <ul className="mt-1 list-inside list-disc text-amber-700">
                        {selected.lint_results.issues.map((issue) => (
                          <li key={issue}>{issue}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                )}
              </section>

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
                  Files
                </h3>
                {fileNames.length > 1 && (
                  <div className="mt-1.5 flex flex-wrap gap-1" role="tablist" aria-label="File tree">
                    {fileNames.map((name) => (
                      <button
                        key={name}
                        type="button"
                        role="tab"
                        aria-selected={activeFile === name}
                        className={`inline-flex h-7 items-center rounded-md border px-2 text-[11px] font-mono transition-colors ${
                          activeFile === name
                            ? "border-primary-700 bg-primary-700 text-white"
                            : "border-ink-300 bg-card text-muted-foreground hover:bg-ink-100"
                        }`}
                        onClick={() => setActiveFile(name)}
                      >
                        {name}
                      </button>
                    ))}
                  </div>
                )}
                {detailLoading ? (
                  <p className="mt-1 text-xs text-muted-foreground">Loading…</p>
                ) : (
                  <pre className="mt-1 max-h-96 overflow-auto rounded-lg border border-ink-300 bg-card p-3 text-xs whitespace-pre-wrap">
                    {activeDiff || "(no diff)"}
                  </pre>
                )}
              </section>

              {selected.status === "approved" && (
                <section className="rounded-lg border border-ink-300 bg-card p-3 text-xs">
                  {selected.superseded_at && (
                    <div className="mb-2 rounded-md border border-amber-300 bg-amber-50 px-2 py-1 text-amber-800">
                      Superseded {new Date(selected.superseded_at).toLocaleString()} — a
                      later approval relocated this skill; this copy is no longer the
                      active landed version.
                    </div>
                  )}
                  <div>landed at <code>{selected.landed_path}</code></div>
                  <div>branch <code>{selected.landed_branch}</code></div>
                  <div>use count: {selected.use_count}</div>
                </section>
              )}
              {detail && detail.invocations.length > 0 && (
                <section className="rounded-lg border border-ink-300 bg-card p-3 text-xs">
                  <div className="mb-1 font-medium text-muted-foreground">
                    Invocation history
                    {selected.status !== "approved" && " (prior version)"}
                  </div>
                  <ul className="space-y-0.5">
                    {detail.invocations.map((inv) => (
                      <li key={inv.id} className="flex flex-wrap gap-x-2">
                        <span>{new Date(inv.used_at).toLocaleString()}</span>
                        {inv.backend && <span>· {inv.backend}</span>}
                        {inv.run_id && <span>· run {inv.run_id}</span>}
                        {inv.task_id && (
                          <button
                            type="button"
                            className="text-primary-700 underline hover:no-underline disabled:cursor-default disabled:text-foreground disabled:no-underline"
                            onClick={() => onOpenTask?.(inv.task_id!)}
                            disabled={!onOpenTask}
                          >
                            open task
                          </button>
                        )}
                      </li>
                    ))}
                  </ul>
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
                    <label htmlFor="skill-approve-scope" className="text-xs text-muted-foreground">
                      Land as
                    </label>
                    <select
                      id="skill-approve-scope"
                      className="h-8 rounded-lg border border-ink-300 bg-card px-2 text-xs outline-none focus:border-primary"
                      value={approveScope}
                      onChange={(e) => setApproveScope(e.target.value as SkillCandidateScope)}
                    >
                      <option value="agent+repo">agent + repo</option>
                      <option value="agent-global">agent-global</option>
                    </select>
                  </div>
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
