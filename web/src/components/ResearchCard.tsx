import { IconWorldSearch, IconX, IconCheck, IconAlertTriangle } from "@tabler/icons-react";
import { useSessionStore, type ResearchJob } from "../stores/sessionStore";
import { SealChip, type CardTone } from "./ui/sheet-card";

const API = window.location.origin;

const PHASES = ["scope", "search", "verify", "synthesize", "done"];
const PHASE_LABEL: Record<string, string> = {
  scope: "Planning angles",
  search: "Searching the web",
  verify: "Verifying claims",
  synthesize: "Writing the report",
  done: "Done",
};

const STATUS_TONE: Record<string, CardTone> = {
  running: "brand",
  completed: "success",
  failed: "destructive",
  cancelled: "neutral",
  interrupted: "neutral",
};

/** Live deep-research progress for the active session (native-deep-research.md
 * §7). The final cited report arrives as a normal injected turn; this card is
 * the progress/affordance surface. Terminal jobs linger briefly as a result. */
export function ResearchCard({ sessionId }: { sessionId: string }) {
  const token = useSessionStore((s) => s.token);
  const jobs = useSessionStore((s) => s.research[sessionId]) ?? [];
  if (jobs.length === 0) return null;

  const cancel = async (job: ResearchJob) => {
    try {
      await fetch(`${API}/api/sessions/${sessionId}/research/${job.id}/cancel`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      });
    } catch {
      /* best-effort; the WS event will reconcile */
    }
  };

  return (
    <div className="research-cards flex flex-col gap-2">
      {jobs.map((job) => {
        const running = job.status === "running";
        const phaseIdx = PHASES.indexOf(job.phase ?? "scope");
        return (
          <SealChip
            key={job.id}
            className="research-card"
            data-research-id={job.id}
            data-status={job.status}
            tone={STATUS_TONE[job.status] ?? "neutral"}
            // A running job shows the live icon in place of the wax: motion
            // is the information, and the ornament budget is one mark per
            // surface. It settles, and the seal comes back.
            head={
              running ? (
                <IconWorldSearch
                  size={15}
                  className="text-primary animate-pulse shrink-0"
                />
              ) : undefined
            }
            glyph={<IconWorldSearch />}
            title={
              <span className="research-question block truncate font-medium text-foreground">
                {job.question}
              </span>
            }
            actions={
              <>
                {job.status === "completed" && (
                  <IconCheck size={15} className="text-success shrink-0" />
                )}
                {(job.status === "failed" ||
                  job.status === "cancelled" ||
                  job.status === "interrupted") && (
                  <IconAlertTriangle size={15} className="text-destructive shrink-0" />
                )}
                {running && (
                  <button
                    type="button"
                    className="btn-research-cancel inline-flex h-5 w-5 items-center justify-center rounded text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                    onClick={() => cancel(job)}
                    title="Cancel research"
                    aria-label="Cancel research"
                  >
                    <IconX size={13} />
                  </button>
                )}
              </>
            }
          >
            <div className="research-status text-xs text-muted-foreground">
              {running ? (
                <span className="research-phase">
                  {PHASE_LABEL[job.phase ?? "scope"] ?? job.phase}
                  {job.detail ? ` — ${job.detail}` : "…"}
                  {phaseIdx >= 0 ? `  (${phaseIdx + 1}/${PHASES.length})` : ""}
                </span>
              ) : job.status === "completed" ? (
                <span>
                  Report delivered below
                  {typeof job.verified === "number"
                    ? ` · ${job.verified} verified finding${job.verified === 1 ? "" : "s"}`
                    : ""}
                  {job.sources && job.sources.length
                    ? ` · ${job.sources.length} source${job.sources.length === 1 ? "" : "s"}`
                    : ""}
                </span>
              ) : (
                <span className="text-destructive">
                  {job.status === "cancelled"
                    ? "Cancelled."
                    : job.error || "Research failed."}
                </span>
              )}
            </div>
          </SealChip>
        );
      })}
    </div>
  );
}
