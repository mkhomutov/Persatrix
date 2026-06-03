import { describe, it, expect } from "vitest";
import { pickInitialAgent } from "./selection.svelte.js";

// pickInitialAgent resolves which persona a freshly-mounted Chat panel opens on.
// Three remembered-id states must be honoured:
//   - an id  → resume that persona (when still present)
//   - ""     → never chose; apply the healthy-first default
//   - null   → the operator deliberately EXITED the conversation; stay in the
//              lobby (return null) rather than snapping back to a default, so an
//              "exit" survives a Chat↔Channels tab round-trip (the panel unmounts
//              on a tab switch and re-runs this on the way back).
const isChattable = (a) => a?.type !== "task";
const LIST = [
  { id: "alice", status: "healthy" },
  { id: "bob", status: "healthy" },
];

describe("pickInitialAgent", () => {
  it("returns null when the operator explicitly exited (rememberedId === null)", () => {
    expect(pickInitialAgent(LIST, isChattable, null)).toBeNull();
  });

  it("applies the healthy-first default for a never-chosen panel (empty id)", () => {
    expect(pickInitialAgent(LIST, isChattable, "")?.id).toBe("alice");
  });

  it("resumes a remembered persona that is still present", () => {
    expect(pickInitialAgent(LIST, isChattable, "bob")?.id).toBe("bob");
  });

  it("returns null for an empty list regardless of the remembered id", () => {
    expect(pickInitialAgent([], isChattable, "")).toBeNull();
  });
});
