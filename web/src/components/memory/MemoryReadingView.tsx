import { useMemo } from "react";
import { IconMessageExclamation, IconRefresh } from "@tabler/icons-react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";

import type { MemoryFileMeta, MemoryGraphNode } from "../../api/memory";
import { resolveWikilink } from "./memoryPresentation";

const WIKILINK_RE = /\[\[([^\]\[]+)\]\]/g;
const WIKILINK_HREF_PREFIX = "#wikilink:";

/** `[[name]]` -> a real markdown link the renderer below intercepts. Kept as
 * a source-text rewrite (not a remark plugin) so the transform stays a
 * one-line, unit-testable pure step instead of a mini-AST visitor. */
function wikilinksToMarkdownLinks(text: string): string {
  return text.replace(
    WIKILINK_RE,
    (_match, name: string) => `[${name.trim()}](${WIKILINK_HREF_PREFIX}${encodeURIComponent(name.trim())})`
  );
}

interface MemoryReadingViewProps {
  file: MemoryFileMeta | null;
  content: string | null;
  loading: boolean;
  graphNodes: readonly MemoryGraphNode[];
  onNavigateLink: (fileName: string) => void;
  onCorrect: () => void;
  correcting?: boolean;
}

/** Right column of the Memory page: renders one file's markdown, with
 * `[[name]]` wikilinks resolved against the agent's graph (clickable when
 * the target resolves to a real file, greyed-out and inert when it's a
 * ghost / unknown target), plus the "纠错" delegation entry point
 * (memory-ui.md §设计要点 2-3). Read-only end to end — there is no editor
 * here and there must never be one. */
export function MemoryReadingView({
  file,
  content,
  loading,
  graphNodes,
  onNavigateLink,
  onCorrect,
  correcting,
}: MemoryReadingViewProps) {
  const transformed = useMemo(
    () => (content ? wikilinksToMarkdownLinks(content) : ""),
    [content]
  );

  if (!file) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-sm text-muted-foreground">
        Select a memory file to read it.
      </div>
    );
  }

  return (
    <div className="memory-reading-view flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center justify-between gap-3 border-b border-ink-300 px-4 py-2.5">
        <div className="min-w-0">
          <h2 className="truncate font-serif text-base font-semibold">
            {file.name ?? file.file}
          </h2>
          {file.description && (
            <p className="truncate text-xs text-muted-foreground">{file.description}</p>
          )}
        </div>
        <button
          type="button"
          className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg border border-ink-300 bg-card px-3 text-xs font-medium text-foreground hover:bg-ink-100 disabled:opacity-50"
          onClick={onCorrect}
          disabled={correcting}
        >
          {correcting ? (
            <IconRefresh size={14} className="animate-spin" />
          ) : (
            <IconMessageExclamation size={14} />
          )}
          纠错
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-4">
        {loading ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : (
          <div className="markdown text-sm leading-relaxed">
            <Markdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[[rehypeHighlight, { ignoreMissing: true }]]}
              components={{
                a: ({ href, children }) => {
                  if (href?.startsWith(WIKILINK_HREF_PREFIX)) {
                    const name = decodeURIComponent(
                      href.slice(WIKILINK_HREF_PREFIX.length)
                    );
                    const node = resolveWikilink(graphNodes, name);
                    const ghost = !node || node.ghost;
                    if (ghost) {
                      return (
                        <span
                          className="wikilink wikilink-ghost cursor-not-allowed text-muted-foreground/60 no-underline"
                          aria-disabled="true"
                          title="This memory has not been written yet"
                        >
                          {children}
                        </span>
                      );
                    }
                    return (
                      <a
                        href="#"
                        className="wikilink text-primary-700 underline decoration-primary-300 hover:text-primary-800"
                        onClick={(event) => {
                          event.preventDefault();
                          onNavigateLink(node.file as string);
                        }}
                      >
                        {children}
                      </a>
                    );
                  }
                  return (
                    <a href={href} target="_blank" rel="noreferrer noopener">
                      {children}
                    </a>
                  );
                },
              }}
            >
              {transformed}
            </Markdown>
          </div>
        )}
      </div>
    </div>
  );
}
