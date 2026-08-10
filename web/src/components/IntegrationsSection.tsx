import { useEffect } from "react";
import { IconChevronRight, IconPlugConnected } from "@tabler/icons-react";
import { ConnectorList } from "./ConnectorList";
import { CredentialList } from "./CredentialList";
import { useSessionStore } from "../stores/sessionStore";

const CREDENTIALS_API = `${window.location.origin}/api/credentials`;

/** Sidebar "Integrations" disclosure group — the cold, configure-once
 * counterpart to the hot rail items above the hairline
 * (docs/plans/sidebar-hierarchy.md §3, §4). Connectors and Credentials are
 * collapsed inside it by default; the collapse state persists across
 * reloads (localStorage, same convention as `showDelegations`).
 *
 * The group header carries its own glanceable count (installed connectors +
 * credentials combined) so collapsing never hides the one signal that
 * matters at a glance: whether anything is configured at all
 * (sidebar-hierarchy.md §4).
 *
 * `credentials` is fetched here unconditionally (not gated on `expanded`)
 * because other UI reads it regardless of this group's collapse state — the
 * new-session credential selector (SessionList) and per-backend credential
 * options (AgentSettings). Connector installations get the same treatment
 * already, fetched globally by AgentList. */
export function IntegrationsSection() {
  const token = useSessionStore((s) => s.token);
  const expanded = useSessionStore((s) => s.integrationsExpanded);
  const setExpanded = useSessionStore((s) => s.setIntegrationsExpanded);
  const connectorCount = useSessionStore((s) => s.connectorInstallations.length);
  const credentialCount = useSessionStore((s) => s.credentials.length);
  const setCredentials = useSessionStore((s) => s.setCredentials);
  const total = connectorCount + credentialCount;

  useEffect(() => {
    if (!token) return;
    fetch(CREDENTIALS_API, { headers: { Authorization: `Bearer ${token}` } })
      .then((r) => (r.ok ? r.json() : null))
      .then((creds) => creds && setCredentials(creds))
      .catch(() => {});
  }, [token, setCredentials]);

  return (
    <div className="integrations-section shrink-0">
      <button
        type="button"
        className="integrations-header group flex h-8 w-full items-center justify-between rounded-lg px-2 hover:bg-sidebar-accent transition-colors"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
        aria-controls="integrations-panel"
      >
        <span className="flex items-center gap-1.5 min-w-0">
          <IconChevronRight
            size={13}
            className={`integrations-fold shrink-0 text-sidebar-foreground/40 transition-transform ${
              expanded ? "rotate-90" : ""
            }`}
          />
          <IconPlugConnected size={14} className="shrink-0 text-sidebar-foreground/55" />
          <span className="text-[11px] font-semibold leading-4 text-sidebar-foreground/55 group-hover:text-sidebar-foreground/80 transition-colors uppercase tracking-[0.12em] truncate">
            Integrations
          </span>
        </span>
        <span className="integrations-count shrink-0 text-xs tabular-nums text-sidebar-foreground/50 group-hover:text-sidebar-foreground/75 transition-colors">
          {total > 0 ? total : "none"}
        </span>
      </button>

      {expanded && (
        <div
          id="integrations-panel"
          role="region"
          aria-label="Integrations"
          className="integrations-panel flex flex-col gap-0.5 mt-0.5 pl-2"
        >
          <ConnectorList />
          <CredentialList />
        </div>
      )}
    </div>
  );
}
