/**
 * The seal is the app's one identity mark, and agent names are free text —
 * so the interesting cases are the names that can't produce a monogram.
 * Round 3 shipped with `monogram()` answering "?" for those, which stamped
 * a question mark into the wax on every turn by an emoji-named agent
 * (`docs/plans/messenger-form.md` §4.1; caught in review).
 */
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Seal, monogram } from "./seal";
import { SEAL_MARK_PATH, SEAL_RIM_PATH } from "../../lib/seal";

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
