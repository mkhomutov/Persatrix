import { describe, it, expect } from "vitest";
import {
  formatTimestamp,
  channelLabel,
  isDMChannel,
  senderLabel,
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
