/** Per-backend model *suggestions* for the free-text model inputs
 * (budget-model-routing.md §4.1).
 *
 * The backend deliberately does NOT enforce a closed model list — CLIs accept
 * arbitrary model strings and new names ship constantly, so validation is a
 * cross-family blacklist, not a whitelist (server/model_routing.py). These
 * lists are therefore only a `<datalist>` of common picks to autocomplete
 * against; the user can always type anything, and a value not in the list is
 * perfectly valid. Keep them short and current rather than exhaustive.
 */

const CLAUDE_MODELS = [
  "claude-opus-4-8",
  "claude-sonnet-5",
  "claude-haiku-4-5",
  "claude-fable-5",
];

const CODEX_MODELS = ["gpt-5-codex", "gpt-5"];

/** Suggested model strings for a backend's free-text model input. An unknown
 * backend gets no suggestions (the input stays plain free text). */
export function modelSuggestions(backend: string | null | undefined): string[] {
  if (backend === "codex") return CODEX_MODELS;
  if (backend === "claude-code") return CLAUDE_MODELS;
  return [];
}
