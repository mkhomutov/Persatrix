import { describe, it, expect } from "vitest";
import {
  formatTimestamp,
  channelLabel,
  isDMChannel,
  senderLabel,
  formatTime,
  dayKey,
  formatDayLabel,
} from "./format.js";

// Pure display-formatting helpers shared by the Chat and ChannelTimeline panels.
// The panels exercise these indirectly; these tests pin the pure behaviour
// (especially the fall-throughs) directly.

describe("formatTimestamp", () => {
  it("renders a parseable timestamp as a local date-time string", () => {
    // Don't assert an exact locale string (it varies by runner timezone/locale);
    // assert it parses to the same instant the input encodes.
    const out = formatTimestamp("2024-05-01T12:00:00Z");
    expect(new Date(out).getTime()).toBe(Date.parse("2024-05-01T12:00:00Z"));
  });

  it("falls back to the raw string for an unparseable value", () => {
    expect(formatTimestamp("not-a-date")).toBe("not-a-date");
  });
});

describe("channelLabel", () => {
  it("prefers the channel name", () => {
    expect(channelLabel({ id: "c-1", name: "general" })).toBe("general");
  });

  it("falls back to the id when unnamed (DMs/threads)", () => {
    expect(channelLabel({ id: "dm-1", name: "" })).toBe("dm-1");
  });
});

describe("isDMChannel", () => {
  it("marks a channel typed dm", () => {
    expect(isDMChannel({ id: "x", channel_type: "dm" })).toBe(true);
  });

  it("marks a dm:-prefixed id even when the type is omitted", () => {
    expect(isDMChannel({ id: "dm:ada:local" })).toBe(true);
  });

  it("leaves group/thread channels unmarked", () => {
    expect(isDMChannel({ id: "general", channel_type: "group" })).toBe(false);
    expect(isDMChannel({ id: "group:standup" })).toBe(false);
  });

  it("is null-safe", () => {
    expect(isDMChannel(undefined)).toBe(false);
    expect(isDMChannel({})).toBe(false);
  });
});

describe("senderLabel", () => {
  const userId = "local";
  const agentsById = {
    ada: { id: "ada", name: "Ada", role: "Researcher" },
    nameless: { id: "nameless" },
  };

  it("renders the operator's own id as You", () => {
    expect(senderLabel("local", userId, agentsById)).toBe("You");
  });

  it("renders a known agent as name — role", () => {
    expect(senderLabel("ada", userId, agentsById)).toBe("Ada — Researcher");
  });

  it("uses the bare name when an agent has no role", () => {
    expect(senderLabel("nameless", userId, agentsById)).toBe("nameless");
  });

  it("falls back to the raw id for an unknown sender", () => {
    expect(senderLabel("ghost", userId, agentsById)).toBe("ghost");
  });
});

// Day grouping + short times for the conversation timeline: the feed draws a
// divider per local calendar day and shows a compact time on each row (the full
// date-time rides the row's tooltip). Dates are built from local-time parts so
// the assertions hold in any runner timezone.
describe("formatTime", () => {
  it("renders a short local time, not the raw ISO string", () => {
    const ts = new Date(2026, 8, 29, 15, 25).toISOString();
    const out = formatTime(ts);
    expect(out).toMatch(/25/);
    expect(out).not.toMatch(/\dT\d/);
    expect(out.length).toBeLessThan(12);
  });

  it("falls back to the raw string for an unparseable value", () => {
    expect(formatTime("not-a-date")).toBe("not-a-date");
  });
});

describe("dayKey", () => {
  it("is equal for two times on the same local day", () => {
    const a = new Date(2026, 8, 29, 0, 5).toISOString();
    const b = new Date(2026, 8, 29, 23, 55).toISOString();
    expect(dayKey(a)).toBe(dayKey(b));
  });

  it("differs across local midnight", () => {
    const a = new Date(2026, 8, 28, 23, 59).toISOString();
    const b = new Date(2026, 8, 29, 0, 1).toISOString();
    expect(dayKey(a)).not.toBe(dayKey(b));
  });

  it("is empty for an unparseable value", () => {
    expect(dayKey("nope")).toBe("");
  });
});

describe("formatDayLabel", () => {
  const now = new Date(2026, 8, 29, 12, 0);

  it("names today and yesterday", () => {
    expect(formatDayLabel(new Date(2026, 8, 29, 8).toISOString(), now)).toBe(
      "Today",
    );
    expect(formatDayLabel(new Date(2026, 8, 28, 22).toISOString(), now)).toBe(
      "Yesterday",
    );
  });

  it("spells out an older date with its day of the month", () => {
    const label = formatDayLabel(new Date(2026, 8, 21, 9).toISOString(), now);
    expect(label).not.toMatch(/today|yesterday/i);
    expect(label).toMatch(/21/);
  });

  it("adds the year only for a date outside the current year", () => {
    const old = formatDayLabel(new Date(2025, 0, 3).toISOString(), now);
    expect(old).toMatch(/2025/);
    const recent = formatDayLabel(new Date(2026, 1, 3).toISOString(), now);
    expect(recent).not.toMatch(/2026/);
  });

  it("is empty for an unparseable value", () => {
    expect(formatDayLabel("nope", now)).toBe("");
  });
});
