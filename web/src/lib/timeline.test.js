import { describe, it, expect } from "vitest";
import { dayLabelAt, isCompact } from "./timeline.js";

// The timeline's grouping rules, in display order (oldest first): a day
// divider opens each local calendar day, and a consecutive same-sender message
// within five minutes renders compact (no repeated avatar or name).
const at = (d, h, m) => new Date(2026, 8, d, h, m).toISOString();
const msg = (sender, ts) => ({ sender_id: sender, timestamp: ts });

describe("dayLabelAt", () => {
  const now = new Date(2026, 8, 29, 12, 0);

  it("labels the first message of each day and nothing else", () => {
    const list = [msg("a", at(28, 9, 0)), msg("a", at(28, 18, 0)), msg("b", at(29, 8, 0))];
    expect(dayLabelAt(list, 0, now)).toBe("Yesterday");
    expect(dayLabelAt(list, 1, now)).toBe("");
    expect(dayLabelAt(list, 2, now)).toBe("Today");
  });

  it("never starts a day on an unparseable timestamp", () => {
    expect(dayLabelAt([msg("a", "garbage")], 0, now)).toBe("");
  });
});

describe("isCompact", () => {
  it("groups a same-sender follow-up within five minutes", () => {
    const list = [msg("a", at(29, 9, 0)), msg("a", at(29, 9, 4))];
    expect(isCompact(list, 0)).toBe(false);
    expect(isCompact(list, 1)).toBe(true);
  });

  it("starts a full row for another sender, a longer gap, or a new day", () => {
    expect(isCompact([msg("a", at(29, 9, 0)), msg("b", at(29, 9, 1))], 1)).toBe(false);
    expect(isCompact([msg("a", at(29, 9, 0)), msg("a", at(29, 9, 6))], 1)).toBe(false);
    expect(isCompact([msg("a", at(28, 23, 58)), msg("a", at(29, 0, 1))], 1)).toBe(false);
  });

  it("does not guess across an unparseable timestamp", () => {
    expect(isCompact([msg("a", "garbage"), msg("a", at(29, 9, 0))], 1)).toBe(false);
  });
});
