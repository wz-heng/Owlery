import { useMemo } from "react";

import type { MemoryGraphEdge, MemoryGraphNode } from "../../api/memory";
import { MEMORY_TYPE_LABEL, MEMORY_TYPES, memoryTypeAccent } from "./memoryPresentation";

const TYPE_COLOR: Record<string, string> = {
  user: "#6366a3",
  feedback: "#c17a2e",
  project: "#3f8f5e",
  reference: "#6b7280",
};
const UNKNOWN_COLOR = "#a8a29e";
const GHOST_COLOR = "#d4d4d8";

function nodeColor(node: MemoryGraphNode): string {
  if (node.ghost) return GHOST_COLOR;
  return (node.type && TYPE_COLOR[node.type]) || UNKNOWN_COLOR;
}

interface LayoutPoint {
  x: number;
  y: number;
}

/** Deterministic circular layout — no force-directed dependency (memory-ui.md
 * §设计要点 4's only constraint: don't pull in a heavyweight graph lib). One
 * agent's memory graph is at most a few dozen nodes, so a plain ring reads
 * fine and needs zero simulation warmup. */
export function circularLayout(
  nodeIds: readonly string[],
  width: number,
  height: number
): Map<string, LayoutPoint> {
  const cx = width / 2;
  const cy = height / 2;
  const radius = Math.max(40, Math.min(cx, cy) - 40);
  const positions = new Map<string, LayoutPoint>();
  const n = nodeIds.length;
  nodeIds.forEach((id, i) => {
    const angle = n === 0 ? 0 : (2 * Math.PI * i) / n - Math.PI / 2;
    positions.set(id, {
      x: cx + radius * Math.cos(angle),
      y: cy + radius * Math.sin(angle),
    });
  });
  return positions;
}

interface MemoryGraphViewProps {
  nodes: MemoryGraphNode[];
  edges: MemoryGraphEdge[];
  onSelectNode: (fileName: string) => void;
}

const WIDTH = 640;
const HEIGHT = 480;

/** One agent's `[[link]]` graph as a plain SVG ring layout — nodes coloured
 * by memory type, ghost nodes (linked-to but not yet written) rendered
 * hollow/dashed. Cross-agent graphs are explicitly out of scope
 * (memory-ui.md §不做清单): one graph per agent, no mixing. */
export function MemoryGraphView({ nodes, edges, onSelectNode }: MemoryGraphViewProps) {
  const positions = useMemo(
    () => circularLayout(nodes.map((n) => n.id), WIDTH, HEIGHT),
    [nodes]
  );

  if (nodes.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-sm text-muted-foreground">
        No links to graph yet.
      </div>
    );
  }

  return (
    <div className="memory-graph-view flex h-full min-h-0 flex-col overflow-auto p-4">
      <svg
        role="img"
        aria-label="Memory link graph"
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="mx-auto max-w-full"
      >
        {edges.map((edge, i) => {
          const from = positions.get(edge.source);
          const to = positions.get(edge.target);
          if (!from || !to) return null;
          return (
            <line
              key={`${edge.source}->${edge.target}-${i}`}
              x1={from.x}
              y1={from.y}
              x2={to.x}
              y2={to.y}
              stroke="#c8c2b8"
              strokeWidth={1.25}
            />
          );
        })}
        {nodes.map((node) => {
          const p = positions.get(node.id);
          if (!p) return null;
          return (
            <g
              key={node.id}
              transform={`translate(${p.x}, ${p.y})`}
              role={node.ghost ? undefined : "button"}
              tabIndex={node.ghost ? undefined : 0}
              aria-label={node.id}
              className={node.ghost ? "cursor-default" : "cursor-pointer"}
              onClick={() => {
                if (!node.ghost && node.file) onSelectNode(node.file);
              }}
              onKeyDown={(event) => {
                if (!node.ghost && node.file && (event.key === "Enter" || event.key === " ")) {
                  event.preventDefault();
                  onSelectNode(node.file);
                }
              }}
            >
              <circle
                r={10}
                fill={node.ghost ? "white" : nodeColor(node)}
                stroke={nodeColor(node)}
                strokeWidth={node.ghost ? 1.5 : 0}
                strokeDasharray={node.ghost ? "3 2" : undefined}
                opacity={node.ghost ? 0.6 : 1}
              />
              <text
                x={14}
                y={4}
                fontSize={11}
                fill={node.ghost ? "#a8a29e" : "#292524"}
                fontStyle={node.ghost ? "italic" : undefined}
              >
                {node.id}
              </text>
            </g>
          );
        })}
      </svg>
      <div className="mt-3 flex flex-wrap items-center justify-center gap-3 text-[11px] text-muted-foreground">
        {MEMORY_TYPES.map((type) => (
          <span key={type} className="inline-flex items-center gap-1.5">
            <span className={`inline-block size-2 rounded-full ${memoryTypeAccent(type)}`} />
            {MEMORY_TYPE_LABEL[type]}
          </span>
        ))}
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block size-2 rounded-full border border-dashed border-ink-400" />
          Ghost (linked, not yet written)
        </span>
      </div>
    </div>
  );
}
