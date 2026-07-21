/**
 * The seal is the app's one identity mark, and agent names are free text —
 * so the interesting cases are the names that can't produce a monogram.
 * Round 3 shipped with `monogram()` answering "?" for those, which stamped
 * a question mark into the wax on every turn by an emoji-named agent
 * (`docs/plans/messenger-form.md` §4.1; caught in review).
 */
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AgentSeal, Seal, monogram } from "./seal";
import {
  SEAL_MARK_PATH,
  SEAL_RIM_PATH,
  WAX_TONES,
  waxColorForId,
  waxToneForId,
} from "../../lib/seal";

describe("monogram", () => {
  it("takes the first letter or digit, uppercased", () => {
    expect(monogram("Dobby")).toBe("D");
    expect(monogram("snape")).toBe("S");
    expect(monogram("7-zip agent")).toBe("7");
  });

  it("skips leading punctuation and whitespace to find one", () => {
    expect(monogram("  ...vera")).toBe("V");
    expect(monogram("🦉 Owlery")).toBe("O");
  });

  it("takes the first letter of a CJK name", () => {
    expect(monogram("信使")).toBe("信");
  });

  it("returns null — never '?' — when there is no letter to take", () => {
    // The regression: each of these used to stamp "?" into the wax.
    expect(monogram("🦉")).toBeNull();
    expect(monogram("🦉🧪")).toBeNull();
    expect(monogram("!!!")).toBeNull();
    expect(monogram("")).toBeNull();
    expect(monogram(undefined)).toBeNull();
    expect(monogram(null)).toBeNull();
  });
});

describe("Seal", () => {
  const pathsOf = (c: HTMLElement) =>
    Array.from(c.querySelectorAll("path")).map((p) => p.getAttribute("d"));

  it("impresses the owl when asked to render the mark", () => {
    const { container } = render(<Seal side="left" mark />);
    expect(pathsOf(container)).toContain(SEAL_MARK_PATH);
  });

  it("renders a plain rim + the glyph when given a monogram", () => {
    const { container } = render(<Seal side="left">{monogram("Dobby")}</Seal>);
    expect(pathsOf(container)).toContain(SEAL_RIM_PATH);
    expect(pathsOf(container)).not.toContain(SEAL_MARK_PATH);
    expect(container.querySelector(".seal-glyph")?.textContent).toBe("D");
  });

  it("carries no glyph node at all when there is nothing to stamp", () => {
    // A `null` monogram must not render an empty glyph span that would
    // sit in the middle of the wax collecting layout.
    const { container } = render(
      <Seal side="left" mark={monogram("🦉") === null}>
        {monogram("🦉")}
      </Seal>
    );
    expect(container.querySelector(".seal-glyph")).toBeNull();
    expect(pathsOf(container)).toContain(SEAL_MARK_PATH);
  });
});

describe("waxToneForId", () => {
  it("is deterministic — one id always yields the same tone/colour", () => {
    expect(waxToneForId("agent-abc")).toBe(waxToneForId("agent-abc"));
    expect(waxColorForId("agent-abc")).toBe(waxColorForId("agent-abc"));
  });

  it("only ever assigns a colour from the wax palette (never red)", () => {
    for (const id of ["a", "b", "c", "dumbledore", "dobby", "x9", "🦉id"]) {
      expect(WAX_TONES).toContain(waxToneForId(id));
    }
    // Red is reserved for `destructive` state and is not an identity colour.
    expect(WAX_TONES as readonly string[]).not.toContain("red");
  });

  it("spreads distinct ids across more than one wax tone", () => {
    const ids = Array.from({ length: 60 }, (_, i) => `agent-${i}`);
    expect(new Set(ids.map(waxToneForId)).size).toBeGreaterThan(1);
  });

  it("returns a ready-to-use CSS custom-property colour", () => {
    expect(waxColorForId("agent-abc")).toMatch(/^hsl\(var\(--wax-[a-z]+\)\)$/);
  });
});

describe("AgentSeal", () => {
  const pathsOf = (c: HTMLElement) =>
    Array.from(c.querySelectorAll("path")).map((p) => p.getAttribute("d"));

  it("impresses the agent's monogram in its own wax colour", () => {
    const { container } = render(
      <AgentSeal agent={{ id: "a1", name: "Dobby" }} />
    );
    expect(container.querySelector(".seal-glyph")?.textContent).toBe("D");
    const rim = container.querySelector(".seal-rim");
    expect(rim?.getAttribute("style") ?? "").toContain("--wax-");
  });

  it("falls back to the owl mark when the name yields no monogram", () => {
    const { container } = render(
      <AgentSeal agent={{ id: "a2", name: "🦉" }} />
    );
    expect(container.querySelector(".seal-glyph")).toBeNull();
    expect(pathsOf(container)).toContain(SEAL_MARK_PATH);
  });
});
