import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { MemoryReadingView } from "./MemoryReadingView";
import type { MemoryFileMeta, MemoryGraphNode } from "../../api/memory";

afterEach(cleanup);

const file: MemoryFileMeta = {
  file: "feedback-testing.md",
  name: "feedback-testing",
  description: "some feedback",
  type: "feedback",
};

const nodes: MemoryGraphNode[] = [
  { id: "feedback-testing", file: "feedback-testing.md", description: null, type: "feedback", ghost: false },
  { id: "project-notes", file: "project-notes.md", description: null, type: "project", ghost: false },
  { id: "future-idea", file: null, description: null, type: null, ghost: true },
];

describe("MemoryReadingView", () => {
  it("shows a placeholder when no file is selected", () => {
    render(
      <MemoryReadingView
        file={null}
        content={null}
        loading={false}
        graphNodes={[]}
        onNavigateLink={vi.fn()}
        onCorrect={vi.fn()}
      />
    );
    expect(screen.getByText(/Select a memory file/)).toBeTruthy();
  });

  it("shows a loading state while the file content is being fetched", () => {
    render(
      <MemoryReadingView
        file={file}
        content={null}
        loading
        graphNodes={[]}
        onNavigateLink={vi.fn()}
        onCorrect={vi.fn()}
      />
    );
    expect(screen.getByText("Loading…")).toBeTruthy();
  });

  it("renders markdown content", () => {
    render(
      <MemoryReadingView
        file={file}
        content={"# Heading\n\nSome **bold** text."}
        loading={false}
        graphNodes={[]}
        onNavigateLink={vi.fn()}
        onCorrect={vi.fn()}
      />
    );
    expect(screen.getByRole("heading", { name: "Heading" })).toBeTruthy();
    expect(screen.getByText("bold")).toBeTruthy();
  });

  it("renders a resolvable [[wikilink]] as clickable and navigates to its file on click", () => {
    const onNavigateLink = vi.fn();
    render(
      <MemoryReadingView
        file={file}
        content={"See [[project-notes]] for details."}
        loading={false}
        graphNodes={nodes}
        onNavigateLink={onNavigateLink}
        onCorrect={vi.fn()}
      />
    );
    const link = screen.getByText("project-notes");
    expect(link.tagName).toBe("A");
    fireEvent.click(link);
    expect(onNavigateLink).toHaveBeenCalledWith("project-notes.md");
  });

  it("renders a ghost [[wikilink]] greyed-out and inert", () => {
    const onNavigateLink = vi.fn();
    render(
      <MemoryReadingView
        file={file}
        content={"Not written yet: [[future-idea]]."}
        loading={false}
        graphNodes={nodes}
        onNavigateLink={onNavigateLink}
        onCorrect={vi.fn()}
      />
    );
    const ghost = screen.getByText("future-idea");
    expect(ghost.tagName).not.toBe("A");
    expect(ghost.getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(ghost);
    expect(onNavigateLink).not.toHaveBeenCalled();
  });

  it("renders a wikilink pointing at an unknown name as inert too", () => {
    render(
      <MemoryReadingView
        file={file}
        content={"[[totally-unknown]]"}
        loading={false}
        graphNodes={nodes}
        onNavigateLink={vi.fn()}
        onCorrect={vi.fn()}
      />
    );
    const el = screen.getByText("totally-unknown");
    expect(el.tagName).not.toBe("A");
  });

  it("leaves a [[literal]] inside a fenced code block untouched (not turned into a link)", () => {
    render(
      <MemoryReadingView
        file={file}
        content={"```\nconst x = [[project-notes]];\n```"}
        loading={false}
        graphNodes={nodes}
        onNavigateLink={vi.fn()}
        onCorrect={vi.fn()}
      />
    );
    expect(screen.getByText(/\[\[project-notes\]\]/)).toBeTruthy();
    expect(screen.queryByRole("link", { name: "project-notes" })).toBeNull();
  });

  it("leaves a [[literal]] inside inline code untouched", () => {
    render(
      <MemoryReadingView
        file={file}
        content={"Use `[[project-notes]]` as the syntax."}
        loading={false}
        graphNodes={nodes}
        onNavigateLink={vi.fn()}
        onCorrect={vi.fn()}
      />
    );
    expect(screen.getByText("[[project-notes]]").tagName).toBe("CODE");
  });

  it("does not double-wrap a [[wikilink]]-shaped string inside an existing markdown link's label", () => {
    const onNavigateLink = vi.fn();
    render(
      <MemoryReadingView
        file={file}
        content={"[see [[project-notes]] here](https://example.com)"}
        loading={false}
        graphNodes={nodes}
        onNavigateLink={onNavigateLink}
        onCorrect={vi.fn()}
      />
    );
    // The whole thing is one ordinary external link — clicking it must NOT
    // fire the wikilink navigation callback.
    const outer = screen.getByRole("link", { name: /project-notes/ });
    expect(outer).toHaveAttribute("href", "https://example.com");
    fireEvent.click(outer);
    expect(onNavigateLink).not.toHaveBeenCalled();
  });

  it("calls onCorrect when the 纠错 button is clicked", () => {
    const onCorrect = vi.fn();
    render(
      <MemoryReadingView
        file={file}
        content={"body"}
        loading={false}
        graphNodes={[]}
        onNavigateLink={vi.fn()}
        onCorrect={onCorrect}
      />
    );
    fireEvent.click(screen.getByText("纠错"));
    expect(onCorrect).toHaveBeenCalledTimes(1);
  });

  it("disables the 纠错 button while a correction session is being created", () => {
    render(
      <MemoryReadingView
        file={file}
        content={"body"}
        loading={false}
        graphNodes={[]}
        onNavigateLink={vi.fn()}
        onCorrect={vi.fn()}
        correcting
      />
    );
    expect(screen.getByText("纠错").closest("button")).toBeDisabled();
  });
});
