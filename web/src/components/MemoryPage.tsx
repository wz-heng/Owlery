import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  IconAlertTriangle,
  IconMenu2,
  IconRefresh,
  IconSearch,
  IconSitemap,
  IconX,
} from "@tabler/icons-react";

import {
  memoryApi,
  type MemoryFileMeta,
  type MemoryGraphResponse,
  type MemoryListResponse,
  type MemorySearchHit,
} from "../api/memory";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { AgentSeal } from "./ui/seal";
import { MemoryFileList } from "./memory/MemoryFileList";
import { MemoryGraphView } from "./memory/MemoryGraphView";
import { MemoryReadingView } from "./memory/MemoryReadingView";
import { buildCorrectionPrompt } from "./memory/memoryPresentation";

interface MemoryPageProps {
  onOpenSession?: (sessionId: string) => void;
  onToggleSidebar?: () => void;
}

/** Top-level Memory page (memory-ui.md §设计要点 2): a three-column browser
 * over every agent's persistent memory dir — agent switcher, file list with
 * type filter chips, and a reading/graph view — plus a top search box that
 * can jump across agents. Entirely read-only: refetches on entry / agent
 * switch / the manual refresh button, never a live push, and the only way
 * to change a memory file is the "纠错" delegation button (never a direct
 * edit from here). */
export function MemoryPage({ onOpenSession, onToggleSidebar }: MemoryPageProps) {
  const token = useSessionStore((s) => s.token);
  const agents = useSessionStore((s) => s.agents);
  const activeAgentId = useSessionStore((s) => s.activeAgentId);
  const setActiveAgentId = useSessionStore((s) => s.setActiveAgentId);
  const setActiveSessionId = useSessionStore((s) => s.setActiveSessionId);
  const setComposerDraft = useSessionStore((s) => s.setComposerDraft);

  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(
    activeAgentId ?? agents[0]?.id ?? null
  );
  const [listData, setListData] = useState<MemoryListResponse | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [graphData, setGraphData] = useState<MemoryGraphResponse | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);

  const [activeTypes, setActiveTypes] = useState<Set<string>>(new Set());
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState<string | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [viewMode, setViewMode] = useState<"read" | "graph">("read");

  const [searchInput, setSearchInput] = useState("");
  const [searchResults, setSearchResults] = useState<MemorySearchHit[] | null>(null);
  const [searching, setSearching] = useState(false);

  const [correcting, setCorrecting] = useState(false);
  const [correctError, setCorrectError] = useState<string | null>(null);

  // Seed the switcher from the app's globally-active agent the first time
  // the agent catalog loads; after that, the user's picks in this page win.
  useEffect(() => {
    if (selectedAgentId === null && agents.length > 0) {
      setSelectedAgentId(activeAgentId ?? agents[0].id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agents, activeAgentId]);

  useEffect(() => {
    if (!token || !selectedAgentId) return;
    let cancelled = false;
    // Drop the previous agent's data immediately (not just on success): a
    // failed fetch must never leave stale list/graph/file data attributed
    // to the *new* `selectedAgentId` sitting around — `currentFile` derives
    // from `listData`, and "纠错" POSTs to whatever `selectedAgentId` is, so
    // a stale pairing would file a correction for the wrong agent/file
    // (Snape review). Note this intentionally does NOT touch `selectedFile`
    // — that's the caller's job: `selectAgent` clears it (switch → default
    // to the index), `jumpToHit` sets it to the search hit's file, and
    // either way it must survive this effect re-running so the hit's file
    // is still what gets applied once the new agent's list arrives.
    setListData(null);
    setGraphData(null);
    setFileContent(null);
    setListLoading(true);
    setListError(null);
    Promise.all([
      memoryApi.list(token, selectedAgentId),
      memoryApi.graph(token, selectedAgentId),
    ])
      .then(([list, graph]) => {
        if (cancelled) return;
        setListData(list);
        setGraphData(graph);
      })
      .catch(() => {
        if (!cancelled) setListError("Failed to load this agent's memory.");
      })
      .finally(() => {
        if (!cancelled) setListLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [token, selectedAgentId, refreshTick]);

  const currentFile: MemoryFileMeta | null = useMemo(() => {
    if (!listData) return null;
    const name = selectedFile ?? listData.index?.file ?? listData.files[0]?.file ?? null;
    if (!name) return null;
    if (listData.index?.file === name) return listData.index;
    return listData.files.find((f) => f.file === name) ?? null;
  }, [listData, selectedFile]);

  useEffect(() => {
    if (!token || !selectedAgentId || !currentFile) {
      setFileContent(null);
      return;
    }
    let cancelled = false;
    setFileLoading(true);
    memoryApi
      .file(token, selectedAgentId, currentFile.file)
      .then((text) => {
        if (!cancelled) setFileContent(text);
      })
      .catch(() => {
        if (!cancelled) setFileContent(null);
      })
      .finally(() => {
        if (!cancelled) setFileLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [token, selectedAgentId, currentFile]);

  const toggleType = useCallback((type: string) => {
    setActiveTypes((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  }, []);

  const selectAgent = useCallback((agentId: string) => {
    setSelectedAgentId(agentId);
    setSelectedFile(null);
    setViewMode("read");
    setActiveTypes(new Set());
    setSearchResults(null);
  }, []);

  // Guards against a slow earlier search's response landing after a faster
  // later one and clobbering it — only the most recently *issued* request's
  // result is ever applied (Snape review).
  const searchRequestRef = useRef(0);
  const runSearch = useCallback(async () => {
    const q = searchInput.trim();
    if (!q || !token) {
      searchRequestRef.current += 1;
      setSearchResults(null);
      return;
    }
    const requestId = ++searchRequestRef.current;
    setSearching(true);
    try {
      const res = await memoryApi.search(token, q);
      if (searchRequestRef.current !== requestId) return;
      setSearchResults(res.hits);
    } catch {
      if (searchRequestRef.current !== requestId) return;
      setSearchResults([]);
    } finally {
      if (searchRequestRef.current === requestId) setSearching(false);
    }
  }, [token, searchInput]);

  const clearSearch = useCallback(() => {
    setSearchInput("");
    setSearchResults(null);
  }, []);

  const jumpToHit = useCallback(
    (hit: MemorySearchHit) => {
      setSelectedAgentId(hit.agent_id);
      setSelectedFile(hit.file);
      setViewMode("read");
      clearSearch();
    },
    [clearSearch]
  );

  const handleCorrect = useCallback(async () => {
    if (!token || !selectedAgentId || !currentFile) return;
    setCorrecting(true);
    setCorrectError(null);
    try {
      const res = await fetch(
        `${window.location.origin}/api/agents/${encodeURIComponent(selectedAgentId)}/sessions`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${token}`,
          },
          body: JSON.stringify({
            name: `纠错:${currentFile.name ?? currentFile.file}`,
          }),
        }
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const session = (await res.json()) as SessionInfo;
      // Read the current list at the moment the POST resolves, not a
      // closed-over value from render time — otherwise a session added by
      // WS/another tab while this POST was in flight would be dropped
      // (CLAUDE.md: use getState() in callbacks that mutate the store; also
      // Snape review).
      const store = useSessionStore.getState();
      store.setSessions([...store.sessions, session]);
      setComposerDraft(session.id, buildCorrectionPrompt(currentFile.file));
      setActiveAgentId(selectedAgentId);
      setActiveSessionId(session.id);
      onOpenSession?.(session.id);
    } catch {
      setCorrectError("Failed to start a correction session.");
    } finally {
      setCorrecting(false);
    }
  }, [
    token,
    selectedAgentId,
    currentFile,
    setComposerDraft,
    setActiveAgentId,
    setActiveSessionId,
    onOpenSession,
  ]);

  return (
    <main className="memory-page flex h-full min-h-0 flex-1 flex-col overflow-hidden bg-background">
      <header className="flex shrink-0 items-center gap-2 border-b border-ink-300 bg-background/95 px-4 py-3 backdrop-blur md:px-6">
        {onToggleSidebar && (
          <button
            type="button"
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg text-muted-foreground hover:bg-ink-200 md:hidden"
            onClick={onToggleSidebar}
            aria-label="Open sidebar"
          >
            <IconMenu2 size={18} />
          </button>
        )}
        <h1 className="font-serif text-base font-semibold shrink-0">Memory</h1>
        <form
          className="relative ml-2 min-w-40 flex-1 max-w-md"
          onSubmit={(event) => {
            event.preventDefault();
            void runSearch();
          }}
        >
          <IconSearch
            size={15}
            className="pointer-events-none absolute left-2.5 top-2.5 text-muted-foreground"
          />
          <input
            className="h-9 w-full rounded-lg border border-ink-300 bg-card pl-8 pr-8 text-sm outline-none focus:border-primary"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            placeholder="Search all agents' memory"
            aria-label="Search all agents' memory"
          />
          {searchInput && (
            <button
              type="button"
              className="absolute right-2 top-2 text-muted-foreground hover:text-foreground"
              onClick={clearSearch}
              aria-label="Clear search"
            >
              <IconX size={14} />
            </button>
          )}
        </form>
        <div className="ml-auto flex items-center gap-1.5">
          <button
            type="button"
            className={`inline-flex h-8 items-center gap-1.5 rounded-lg border px-2.5 text-xs font-medium transition-colors ${
              viewMode === "graph"
                ? "border-primary-700 bg-primary-700 text-white"
                : "border-ink-300 bg-card text-muted-foreground hover:bg-ink-100"
            }`}
            onClick={() => setViewMode(viewMode === "graph" ? "read" : "graph")}
            aria-pressed={viewMode === "graph"}
          >
            <IconSitemap size={14} /> Graph
          </button>
          <button
            type="button"
            className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-ink-300 bg-card text-muted-foreground hover:bg-ink-100 disabled:opacity-50"
            onClick={() => setRefreshTick((t) => t + 1)}
            disabled={listLoading}
            aria-label="Refresh"
            title="Refresh"
          >
            <IconRefresh size={15} className={listLoading ? "animate-spin" : undefined} />
          </button>
        </div>
      </header>

      {correctError && (
        <div className="mx-4 mt-2 flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive-surface px-3 py-2 text-xs text-destructive md:mx-6">
          <IconAlertTriangle size={15} className="mt-0.5 shrink-0" />
          <span className="flex-1">{correctError}</span>
          <button type="button" onClick={() => setCorrectError(null)} aria-label="Dismiss error">
            <IconX size={14} />
          </button>
        </div>
      )}

      {searchResults !== null ? (
        <div className="flex-1 overflow-y-auto p-4 md:p-6">
          <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {searching
              ? "Searching…"
              : `${searchResults.length} result${searchResults.length === 1 ? "" : "s"} for "${searchInput}"`}
          </h2>
          {searchResults.length === 0 && !searching && (
            <p className="text-sm text-muted-foreground">No memory files matched.</p>
          )}
          <ul className="space-y-1.5">
            {searchResults.map((hit) => {
              const agent = agents.find((a) => a.id === hit.agent_id);
              return (
                <li key={`${hit.agent_id}-${hit.file}`}>
                  <button
                    type="button"
                    className="w-full rounded-lg border border-ink-300 bg-card p-3 text-left hover:border-primary-300"
                    onClick={() => jumpToHit(hit)}
                  >
                    <div className="flex items-center gap-2 text-xs font-medium text-foreground">
                      <span>{agent?.name ?? hit.agent_id}</span>
                      <span className="text-muted-foreground">·</span>
                      <span className="text-muted-foreground">{hit.name ?? hit.file}</span>
                    </div>
                    <p className="mt-1 truncate text-xs text-muted-foreground">{hit.snippet}</p>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      ) : agents.length === 0 ? (
        <div className="flex flex-1 items-center justify-center p-8 text-sm text-muted-foreground">
          No agents yet.
        </div>
      ) : (
        <div className="flex flex-1 min-h-0">
          <div className="w-44 shrink-0 overflow-y-auto border-r border-ink-300 p-1.5">
            {agents.map((agent) => (
              <button
                key={agent.id}
                type="button"
                className={`flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm transition-colors ${
                  agent.id === selectedAgentId
                    ? "bg-primary-50 text-foreground"
                    : "text-foreground/85 hover:bg-ink-100"
                }`}
                aria-current={agent.id === selectedAgentId ? "true" : undefined}
                onClick={() => selectAgent(agent.id)}
              >
                <AgentSeal agent={agent} scale="chip" className="shrink-0" />
                <span className="truncate">{agent.name}</span>
              </button>
            ))}
          </div>

          <div className="w-64 shrink-0 border-r border-ink-300">
            {listError ? (
              <div className="p-4 text-xs text-destructive">{listError}</div>
            ) : listLoading && !listData ? (
              <div className="p-4 text-xs text-muted-foreground">Loading…</div>
            ) : (
              <MemoryFileList
                index={listData?.index ?? null}
                files={listData?.files ?? []}
                activeTypes={activeTypes}
                onToggleType={toggleType}
                selectedFile={selectedFile}
                onSelectFile={setSelectedFile}
              />
            )}
          </div>

          <div className="min-w-0 flex-1">
            {viewMode === "graph" ? (
              <MemoryGraphView
                nodes={graphData?.nodes ?? []}
                edges={graphData?.edges ?? []}
                onSelectNode={(fileName) => {
                  setSelectedFile(fileName);
                  setViewMode("read");
                }}
              />
            ) : (
              <MemoryReadingView
                file={currentFile}
                content={fileContent}
                loading={fileLoading}
                graphNodes={graphData?.nodes ?? []}
                onNavigateLink={setSelectedFile}
                onCorrect={handleCorrect}
                correcting={correcting}
              />
            )}
          </div>
        </div>
      )}
    </main>
  );
}
