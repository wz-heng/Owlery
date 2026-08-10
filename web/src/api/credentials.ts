/** Typed fetch wrapper for the /api/credentials list route.
 *
 * Mirrors api/connectors.ts's fetchInstallations: a single global-list fetch
 * shared by every consumer of `sessionStore.credentials` (the sidebar
 * CredentialList, the new-session credential selector in SessionList, and
 * AgentSettings' per-backend credential options). */

import type { CredentialInfo } from ".";

const API = `${window.location.origin}/api/credentials`;

export async function fetchCredentials(token: string): Promise<CredentialInfo[]> {
  const res = await fetch(API, { headers: { Authorization: `Bearer ${token}` } });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
