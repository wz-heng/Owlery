import { IconPlus } from "@tabler/icons-react";

/** Sidebar "Applications" section. Houses applications managed by this
 * Owlery instance. The + button opens the add-application flow (wired up
 * by the parent via `onAdd`). */
export function ApplicationList({ onAdd }: { onAdd: () => void }) {
  return (
    <div className="application-section shrink-0">
      <div className="application-header group flex h-8 items-center justify-between rounded-lg px-2 hover:bg-sidebar-accent transition-colors">
        <h2 className="text-[11px] font-semibold leading-4 text-sidebar-foreground/55 group-hover:text-sidebar-foreground/80 transition-colors uppercase tracking-[0.12em]">
          Applications
        </h2>
        <button
          className="btn-application-add inline-flex h-6 w-6 items-center justify-center rounded-md border border-ink-300/70 bg-card/60 text-sidebar-foreground/50 transition-all hover:border-primary/50 hover:bg-primary-50 hover:text-primary-700 active:translate-y-px"
          onClick={onAdd}
          title="Add application"
          aria-label="Add application"
        >
          <IconPlus size={14} />
        </button>
      </div>
    </div>
  );
}
