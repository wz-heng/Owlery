import type { MemoryFileMeta, MemoryGraphNode } from "../../api/memory";

/** The four memory types the memory-writing convention uses (see any
 * agent's `MEMORY.md` frontmatter). Fixed, not derived from data — the
 * filter chips always show all four regardless of what a given agent has
 * actually written yet. */
export const MEMORY_TYPES = ["user", "feedback", "project", "reference"] as const;
export type MemoryType = (typeof MEMORY_TYPES)[number];

export const MEMORY_TYPE_LABEL: Record<MemoryType, string> = {
  user: "User",
  feedback: "Feedback",
  project: "Project",
  reference: "Reference",
};

// One accent per type, reused for the filter chips, the file list rows, and
// the graph node fill so the same type reads as the same colour everywhere.
export const MEMORY_TYPE_ACCENT: Record<MemoryType, string> = {
  user: "bg-primary-500",
  feedback: "bg-attention",
  project: "bg-success",
  reference: "bg-ink-500",
};

const UNKNOWN_TYPE_ACCENT = "bg-ink-300";

export function memoryTypeAccent(type: string | null): string {
  return type && (MEMORY_TYPES as readonly string[]).includes(type)
    ? MEMORY_TYPE_ACCENT[type as MemoryType]
    : UNKNOWN_TYPE_ACCENT;
}

/** Files whose `type` is in `activeTypes`; an empty filter set means "show
 * everything" (no chip selected = no filtering, not "show nothing"). Files
 * with no `type` frontmatter only show up when nothing is filtered. */
export function filterMemoryFiles(
  files: MemoryFileMeta[],
  activeTypes: ReadonlySet<string>
): MemoryFileMeta[] {
  if (activeTypes.size === 0) return files;
  return files.filter((f) => f.type != null && activeTypes.has(f.type));
}

/** Resolve a `[[name]]` wikilink target against the current agent's already
 * -fetched graph. `null` = not a known node at all (shouldn't happen since
 * the backend materializes a ghost node for every link target, but a
 * reading view render can race ahead of the graph fetch). */
export function resolveWikilink(
  nodes: readonly MemoryGraphNode[],
  name: string
): MemoryGraphNode | null {
  const trimmed = name.trim();
  return nodes.find((n) => n.id === trimmed) ?? null;
}

/** The correction-delegation prompt template (memory-ui.md §3): file name +
 * a blank for the user's annotation + the fixed instruction. The user fills
 * in the annotation and sends it themselves — this function only builds the
 * starting draft, it never sends anything. */
export function buildCorrectionPrompt(fileName: string): string {
  return [`文件:${fileName}`, "", "用户批注:", "", "请核实并更新你的记忆与索引。"].join(
    "\n"
  );
}
