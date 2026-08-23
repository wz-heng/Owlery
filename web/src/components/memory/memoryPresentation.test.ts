import { describe, expect, it } from "vitest";

import type { MemoryFileMeta, MemoryGraphNode } from "../../api/memory";
import {
  buildCorrectionPrompt,
  filterMemoryFiles,
  memoryTypeAccent,
  resolveWikilink,
} from "./memoryPresentation";

function file(overrides: Partial<MemoryFileMeta> = {}): MemoryFileMeta {
  return {
    file: "foo.md",
    name: "foo",
    description: null,
    type: null,
    ...overrides,
  };
}

function node(overrides: Partial<MemoryGraphNode> = {}): MemoryGraphNode {
  return {
    id: "foo",
    file: "foo.md",
    description: null,
    type: "project",
    ghost: false,
    ...overrides,
  };
}

describe("filterMemoryFiles", () => {
  it("returns every file when no type is selected", () => {
    const files = [file({ file: "a.md", type: "user" }), file({ file: "b.md", type: null })];
    expect(filterMemoryFiles(files, new Set())).toEqual(files);
  });

  it("keeps only files whose type is in the active set", () => {
    const files = [
      file({ file: "a.md", type: "user" }),
      file({ file: "b.md", type: "feedback" }),
      file({ file: "c.md", type: "user" }),
    ];
    const result = filterMemoryFiles(files, new Set(["user"]));
    expect(result.map((f) => f.file)).toEqual(["a.md", "c.md"]);
  });

  it("excludes files with no type when a filter is active", () => {
    const files = [file({ file: "a.md", type: null }), file({ file: "b.md", type: "project" })];
    const result = filterMemoryFiles(files, new Set(["project"]));
    expect(result.map((f) => f.file)).toEqual(["b.md"]);
  });

  it("honors multiple active types", () => {
    const files = [
      file({ file: "a.md", type: "user" }),
      file({ file: "b.md", type: "feedback" }),
      file({ file: "c.md", type: "reference" }),
    ];
    const result = filterMemoryFiles(files, new Set(["user", "reference"]));
    expect(result.map((f) => f.file)).toEqual(["a.md", "c.md"]);
  });
});

describe("resolveWikilink", () => {
  it("finds a node by exact id", () => {
    const nodes = [node({ id: "foo" }), node({ id: "bar" })];
    expect(resolveWikilink(nodes, "bar")).toEqual(nodes[1]);
  });

  it("trims surrounding whitespace before matching", () => {
    const nodes = [node({ id: "foo" })];
    expect(resolveWikilink(nodes, "  foo  ")).toEqual(nodes[0]);
  });

  it("returns null when nothing matches", () => {
    const nodes = [node({ id: "foo" })];
    expect(resolveWikilink(nodes, "missing")).toBeNull();
  });
});

describe("memoryTypeAccent", () => {
  it("returns a distinct class per known type", () => {
    const classes = new Set(
      ["user", "feedback", "project", "reference"].map((t) => memoryTypeAccent(t))
    );
    expect(classes.size).toBe(4);
  });

  it("falls back to a neutral class for unknown/null types", () => {
    expect(memoryTypeAccent(null)).toBe(memoryTypeAccent("something-unrecognized"));
  });
});

describe("buildCorrectionPrompt", () => {
  it("includes the file name, an annotation blank, and the fixed instruction", () => {
    const prompt = buildCorrectionPrompt("codeword-zebra77.md");
    expect(prompt).toContain("codeword-zebra77.md");
    expect(prompt).toContain("用户批注");
    expect(prompt).toContain("请核实并更新你的记忆与索引");
  });
});
