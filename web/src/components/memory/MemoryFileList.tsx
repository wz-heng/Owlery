import { IconFileText } from "@tabler/icons-react";

import type { MemoryFileMeta } from "../../api/memory";
import {
  MEMORY_TYPES,
  MEMORY_TYPE_LABEL,
  filterMemoryFiles,
  memoryTypeAccent,
} from "./memoryPresentation";

interface MemoryFileListProps {
  index: MemoryFileMeta | null;
  files: MemoryFileMeta[];
  activeTypes: ReadonlySet<string>;
  onToggleType: (type: string) => void;
  selectedFile: string | null;
  onSelectFile: (file: string) => void;
}

/** Middle column of the Memory page: type filter chips + the file list for
 * the currently selected agent. `MEMORY.md` (the index) always pins to the
 * top, outside the type filter — it's the agent's memory homepage, not a
 * regular typed entry (memory-ui.md §设计要点 2). */
export function MemoryFileList({
  index,
  files,
  activeTypes,
  onToggleType,
  selectedFile,
  onSelectFile,
}: MemoryFileListProps) {
  const visible = filterMemoryFiles(files, activeTypes);

  return (
    <div className="memory-file-list flex h-full min-h-0 flex-col">
      <div
        className="flex flex-wrap gap-1.5 border-b border-ink-300 p-2.5"
        role="group"
        aria-label="Filter by memory type"
      >
        {MEMORY_TYPES.map((type) => {
          const active = activeTypes.has(type);
          return (
            <button
              key={type}
              type="button"
              className={`inline-flex h-6 items-center gap-1.5 rounded-full border px-2.5 text-[11px] font-medium transition-colors ${
                active
                  ? "border-primary-700 bg-primary-700 text-white"
                  : "border-ink-300 bg-card text-muted-foreground hover:border-primary-300"
              }`}
              aria-pressed={active}
              onClick={() => onToggleType(type)}
            >
              <span className={`size-1.5 rounded-full ${memoryTypeAccent(type)}`} aria-hidden />
              {MEMORY_TYPE_LABEL[type]}
            </button>
          );
        })}
      </div>

      <div className="flex-1 overflow-y-auto p-1.5">
        {index && (
          <FileRow
            meta={index}
            isIndex
            active={selectedFile === null || selectedFile === index.file}
            onSelect={() => onSelectFile(index.file)}
          />
        )}
        {visible.length === 0 ? (
          <p className="px-2.5 py-4 text-xs text-muted-foreground">
            {files.length === 0 ? "No memory files yet." : "No files match the selected types."}
          </p>
        ) : (
          visible.map((meta) => (
            <FileRow
              key={meta.file}
              meta={meta}
              active={selectedFile === meta.file}
              onSelect={() => onSelectFile(meta.file)}
            />
          ))
        )}
      </div>
    </div>
  );
}

function FileRow({
  meta,
  active,
  isIndex,
  onSelect,
}: {
  meta: MemoryFileMeta;
  active: boolean;
  isIndex?: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      className={`flex w-full items-start gap-2 rounded-lg px-2.5 py-2 text-left text-sm transition-colors ${
        active
          ? "bg-primary-50 text-foreground"
          : "text-foreground/85 hover:bg-ink-100"
      }`}
      aria-current={active ? "true" : undefined}
      onClick={onSelect}
    >
      <span className="mt-0.5 shrink-0">
        {isIndex ? (
          <IconFileText size={15} className="text-primary-700" />
        ) : (
          <span className={`inline-block size-2 rounded-full ${memoryTypeAccent(meta.type)}`} />
        )}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate font-medium">{meta.name ?? meta.file}</span>
        {meta.description && (
          <span className="block truncate text-xs text-muted-foreground">
            {meta.description}
          </span>
        )}
      </span>
    </button>
  );
}
