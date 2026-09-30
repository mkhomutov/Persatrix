import { describe, it, expect } from "vitest";
import { matchesQuery, byLabel } from "./filter.js";

// The sidebar's "Jump to…" box, the member list and the new-channel member
// picker all narrow long lists with the same rule, so it lives in one pure
// helper: every word the operator typed must appear somewhere in the item's
// searchable text, ignoring case and accents.
describe("matchesQuery", () => {
  const fields = ["General", "group:general", "Company-wide chatter"];

  it("matches everything for an empty or blank query", () => {
    expect(matchesQuery(fields, "")).toBe(true);
    expect(matchesQuery(fields, "   ")).toBe(true);
    expect(matchesQuery(fields, undefined)).toBe(true);
  });

  it("matches a case-insensitive substring of any field", () => {
    expect(matchesQuery(fields, "GEN")).toBe(true);
    expect(matchesQuery(fields, "chatter")).toBe(true);
    expect(matchesQuery(fields, "ops")).toBe(false);
  });

  it("requires every word, each in any field", () => {
    expect(matchesQuery(fields, "general company")).toBe(true);
    expect(matchesQuery(fields, "general ops")).toBe(false);
  });

  it("ignores accents and tolerates missing fields", () => {
    expect(matchesQuery(["Café crème", null, undefined], "cafe")).toBe(true);
  });
});

describe("byLabel", () => {
  it("orders by a label, case-insensitively and number-aware", () => {
    const items = ["ops", "General", "team 10", "team 9"];
    expect([...items].sort(byLabel((s) => s))).toEqual([
      "General",
      "ops",
      "team 9",
      "team 10",
    ]);
  });
});
