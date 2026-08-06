import { describe, expect, it } from "vitest";
import { modelSuggestions } from "./models";

describe("modelSuggestions", () => {
  it("returns Claude models for the claude-code backend", () => {
    const s = modelSuggestions("claude-code");
    expect(s).toContain("claude-opus-4-8");
    expect(s.every((m) => m.startsWith("claude"))).toBe(true);
  });

  it("returns OpenAI/Codex models for the codex backend", () => {
    const s = modelSuggestions("codex");
    expect(s).toContain("gpt-5-codex");
    // Never suggests a Claude model for codex — that's exactly the cross-family
    // mismatch the backend blacklist rejects (budget-model-routing.md §4.3).
    expect(s.some((m) => m.startsWith("claude"))).toBe(false);
  });

  it("returns no suggestions for an unknown or missing backend", () => {
    expect(modelSuggestions(undefined)).toEqual([]);
    expect(modelSuggestions(null)).toEqual([]);
    expect(modelSuggestions("something-else")).toEqual([]);
  });
});
