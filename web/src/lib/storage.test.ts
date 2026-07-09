import { beforeEach, describe, expect, it } from "vitest";

import { SHOW_DELEGATIONS_KEY, TOKEN_KEY, readStored } from "./storage";

describe("readStored", () => {
  beforeEach(() => localStorage.clear());

  it("reads the current key", () => {
    localStorage.setItem(TOKEN_KEY, "abc");
    expect(readStored(TOKEN_KEY)).toBe("abc");
  });

  it("returns null when neither key is set", () => {
    expect(readStored(TOKEN_KEY)).toBeNull();
  });

  it("migrates a pre-rename value forward, so nobody is logged out", () => {
    localStorage.setItem("octopus_token", "legacy");

    expect(readStored(TOKEN_KEY)).toBe("legacy");

    // Moved across, and the old key is read at most once.
    expect(localStorage.getItem(TOKEN_KEY)).toBe("legacy");
    expect(localStorage.getItem("octopus_token")).toBeNull();
  });

  it("prefers the current key over a stale legacy one", () => {
    localStorage.setItem(TOKEN_KEY, "new");
    localStorage.setItem("octopus_token", "old");

    expect(readStored(TOKEN_KEY)).toBe("new");
    expect(localStorage.getItem("octopus_token")).toBe("old"); // untouched
  });

  it("migrates the show-delegations flag too", () => {
    localStorage.setItem("octopus_show_delegations", "true");

    expect(readStored(SHOW_DELEGATIONS_KEY)).toBe("true");
    expect(localStorage.getItem("octopus_show_delegations")).toBeNull();
  });

  it("does not invent a legacy name for an unknown key", () => {
    expect(readStored("owlery_unknown")).toBeNull();
  });
});
