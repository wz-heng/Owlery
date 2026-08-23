import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { MemoryFileList } from "./MemoryFileList";
import type { MemoryFileMeta } from "../../api/memory";

afterEach(cleanup);

const index: MemoryFileMeta = {
  file: "MEMORY.md",
  name: null,
  description: null,
  type: null,
};

const files: MemoryFileMeta[] = [
  { file: "a.md", name: "Alpha", description: "first", type: "user" },
  { file: "b.md", name: "Beta", description: "second", type: "feedback" },
  { file: "c.md", name: "Gamma", description: "third", type: "user" },
];

describe("MemoryFileList", () => {
  it("always pins the index file at the top, unaffected by type filters", () => {
    render(
      <MemoryFileList
        index={index}
        files={files}
        activeTypes={new Set(["feedback"])}
        onToggleType={vi.fn()}
        selectedFile={null}
        onSelectFile={vi.fn()}
      />
    );
    expect(screen.getByText("MEMORY.md")).toBeTruthy();
    expect(screen.getByText("Beta")).toBeTruthy();
    expect(screen.queryByText("Alpha")).toBeNull();
  });

  it("shows every non-index file when no type chip is active", () => {
    render(
      <MemoryFileList
        index={index}
        files={files}
        activeTypes={new Set()}
        onToggleType={vi.fn()}
        selectedFile={null}
        onSelectFile={vi.fn()}
      />
    );
    expect(screen.getByText("Alpha")).toBeTruthy();
    expect(screen.getByText("Beta")).toBeTruthy();
    expect(screen.getByText("Gamma")).toBeTruthy();
  });

  it("calls onToggleType with the clicked chip's type", () => {
    const onToggleType = vi.fn();
    render(
      <MemoryFileList
        index={index}
        files={files}
        activeTypes={new Set()}
        onToggleType={onToggleType}
        selectedFile={null}
        onSelectFile={vi.fn()}
      />
    );
    fireEvent.click(screen.getByText("Feedback"));
    expect(onToggleType).toHaveBeenCalledWith("feedback");
  });

  it("calls onSelectFile with the file name when a row is clicked", () => {
    const onSelectFile = vi.fn();
    render(
      <MemoryFileList
        index={index}
        files={files}
        activeTypes={new Set()}
        onToggleType={vi.fn()}
        selectedFile={null}
        onSelectFile={onSelectFile}
      />
    );
    fireEvent.click(screen.getByText("Alpha"));
    expect(onSelectFile).toHaveBeenCalledWith("a.md");
  });

  it("shows an empty-state message when the filter excludes everything", () => {
    render(
      <MemoryFileList
        index={null}
        files={files}
        activeTypes={new Set(["reference"])}
        onToggleType={vi.fn()}
        selectedFile={null}
        onSelectFile={vi.fn()}
      />
    );
    expect(screen.getByText(/No files match the selected types/)).toBeTruthy();
  });
});
