import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { MemoryGraphView, circularLayout } from "./MemoryGraphView";
import type { MemoryGraphEdge, MemoryGraphNode } from "../../api/memory";

afterEach(cleanup);

const nodes: MemoryGraphNode[] = [
  { id: "real-node", file: "real-node.md", description: null, type: "project", ghost: false },
  { id: "ghost-node", file: null, description: null, type: null, ghost: true },
];
const edges: MemoryGraphEdge[] = [{ source: "real-node", target: "ghost-node" }];

describe("circularLayout", () => {
  it("places every node id on the ring", () => {
    const positions = circularLayout(["a", "b", "c"], 400, 300);
    expect(positions.size).toBe(3);
    for (const id of ["a", "b", "c"]) {
      expect(positions.has(id)).toBe(true);
    }
  });

  it("returns an empty map for no nodes", () => {
    expect(circularLayout([], 400, 300).size).toBe(0);
  });
});

describe("MemoryGraphView", () => {
  it("shows an empty state when there are no nodes", () => {
    render(<MemoryGraphView nodes={[]} edges={[]} onSelectNode={vi.fn()} />);
    expect(screen.getByText(/No links to graph yet/)).toBeTruthy();
  });

  it("renders a node per graph node, real and ghost alike", () => {
    render(<MemoryGraphView nodes={nodes} edges={edges} onSelectNode={vi.fn()} />);
    expect(screen.getByText("real-node")).toBeTruthy();
    expect(screen.getByText("ghost-node")).toBeTruthy();
  });

  it("calls onSelectNode with the file name when a real node is clicked", () => {
    const onSelectNode = vi.fn();
    render(<MemoryGraphView nodes={nodes} edges={edges} onSelectNode={onSelectNode} />);
    fireEvent.click(screen.getByLabelText("real-node"));
    expect(onSelectNode).toHaveBeenCalledWith("real-node.md");
  });

  it("does not call onSelectNode when a ghost node is clicked", () => {
    const onSelectNode = vi.fn();
    render(<MemoryGraphView nodes={nodes} edges={edges} onSelectNode={onSelectNode} />);
    fireEvent.click(screen.getByText("ghost-node"));
    expect(onSelectNode).not.toHaveBeenCalled();
  });
});
