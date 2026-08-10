import { useCallback, useEffect } from "react";
import { IconClock } from "@tabler/icons-react";
import { useSessionStore } from "../stores/sessionStore";

/** Sidebar "Schedules" section. No longer a per-agent create form — creation
 * moved to the `/schedule` chat command. This is now just the entry point to
 * the all-agents Schedules overview (SchedulesDialog, owned by App). It loads
 * the schedule list once so the header can show a live count.
 *
 * Rendered at rail level, next to Task Board (docs/plans/sidebar-hierarchy.md
 * §3): Schedules is a hot, glanceable work surface, not a configure-once
 * setting, so it keeps the same visual weight as the Task Board entry rather
 * than living below the infra hairline with Connectors/Credentials. */
export function ScheduleList({ onOpen }: { onOpen: () => void }) {
  const token = useSessionStore((s) => s.token);
  const schedules = useSessionStore((s) => s.schedules);
  const setSchedules = useSessionStore((s) => s.setSchedules);

  const fetchSchedules = useCallback(async () => {
    const resp = await fetch("/api/schedules", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (resp.ok) setSchedules(await resp.json());
  }, [token, setSchedules]);

  useEffect(() => {
    if (token) fetchSchedules();
  }, [token, fetchSchedules]);

  const count = schedules.length;

  return (
    <div className="schedule-section shrink-0">
      <button
        type="button"
        className="schedule-header group mt-1 flex h-9 w-full items-center gap-2 rounded-lg px-3 text-left text-sm text-sidebar-foreground/75 transition-colors hover:bg-sidebar-accent/70 hover:text-sidebar-foreground"
        onClick={onOpen}
        title="View all schedules"
      >
        <IconClock size={17} className="shrink-0" />
        <span className="schedule-title flex-1 truncate">Schedules</span>
        {count > 0 && (
          <span className="schedule-count shrink-0 tabular-nums text-xs text-sidebar-foreground/55 group-hover:text-sidebar-foreground/80 transition-colors">
            {count}
          </span>
        )}
      </button>
    </div>
  );
}
