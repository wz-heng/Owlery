/** localStorage keys, and a one-time migration off the pre-rename `octopus_*`
 * names (docs/plans/rename-owlery.md). Without this an existing user would be
 * silently logged out on the first load of the renamed build, because their
 * auth token lives under the old key. */

export const TOKEN_KEY = "owlery_token";
export const SHOW_DELEGATIONS_KEY = "owlery_show_delegations";

const LEGACY_KEYS: Record<string, string> = {
  [TOKEN_KEY]: "octopus_token",
  [SHOW_DELEGATIONS_KEY]: "octopus_show_delegations",
};

/** Read `key`, falling back to its pre-rename name — and, when the fallback
 * hits, moving the value across so the old key is read at most once. */
export function readStored(key: string): string | null {
  const current = localStorage.getItem(key);
  if (current !== null) return current;

  const legacyKey = LEGACY_KEYS[key];
  if (!legacyKey) return null;
  const legacy = localStorage.getItem(legacyKey);
  if (legacy === null) return null;

  localStorage.setItem(key, legacy);
  localStorage.removeItem(legacyKey);
  return legacy;
}
