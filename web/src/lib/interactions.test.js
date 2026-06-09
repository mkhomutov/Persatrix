import { describe, it, expect } from "vitest";
import {
  pickLatestClosed,
  closeTriggerLabel,
  isSummaryUnavailable,
  participantAgentIds,
  SUMMARY_UNAVAILABLE_TEXT,
} from "./interactions.js";

// Pure helpers behind the interaction-summary surface (v0.3.8). The read API is
// per-agent, so the surface merges several agents' closed-interaction lists and
// shows the single newest one; these helpers own that merge and the honest
// rendering of the close trigger + the failure sentinel (SS3).

describe("pickLatestClosed", () => {
  function rec(id, closedAt) {
    return { interaction_id: id, closed_at: closedAt, summary: "s" };
  }

  it("returns null for an empty list", () => {
    expect(pickLatestClosed([])).toBeNull();
    expect(pickLatestClosed(undefined)).toBeNull();
  });

  it("picks the record with the greatest closed_at", () => {
    const newest = rec("c", 300);
    const records = [rec("a", 100), newest, rec("b", 200)];
    expect(pickLatestClosed(records)).toBe(newest);
  });

  it("collapses the same interaction reported by multiple agents to one record", () => {
    // Each participating agent persists its own summary row for the shared
    // interaction. The reducer doesn't dedupe — it returns the single newest
    // record, and two rows sharing an interaction_id + closed_at collapse to
    // one indistinguishable result, so the surface shows one affordance.
    const records = [rec("shared", 200), rec("shared", 200)];
    const picked = pickLatestClosed(records);
    expect(picked.interaction_id).toBe("shared");
  });
});

describe("closeTriggerLabel", () => {
  it("maps the RFC 0020 close reasons to human labels", () => {
    expect(closeTriggerLabel("cost")).toMatch(/cost/i);
    expect(closeTriggerLabel("idle_gap")).toMatch(/idle/i);
    expect(closeTriggerLabel("structural")).toMatch(/ended|concluded/i);
  });

  it("falls back to a neutral label for an unknown or empty reason", () => {
    expect(closeTriggerLabel("")).toMatch(/closed/i);
    expect(closeTriggerLabel("mystery")).toMatch(/closed/i);
  });
});

describe("participantAgentIds", () => {
  it("returns the single peer for a DM, dropping the human principal", () => {
    expect(
      participantAgentIds({ isDM: true, peerId: "ember-owl", exclude: "local" }),
    ).toEqual(["ember-owl"]);
    // A DM "peer" that is the principal itself (defensive) yields nothing.
    expect(
      participantAgentIds({ isDM: true, peerId: "local", exclude: "local" }),
    ).toEqual([]);
  });

  it("returns a group channel's member ids, excluding the human and blanks", () => {
    const members = [
      { id: "ember-owl", respond: "participant" },
      { id: "iron-fox", respond: "observer" },
      { id: "local", respond: "participant" },
      { id: "", respond: "participant" },
    ];
    expect(
      participantAgentIds({ isDM: false, members, exclude: "local" }),
    ).toEqual(["ember-owl", "iron-fox"]);
  });

  it("accepts raw string member ids too", () => {
    expect(
      participantAgentIds({
        isDM: false,
        members: ["ember-owl", "iron-fox"],
        exclude: "local",
      }),
    ).toEqual(["ember-owl", "iron-fox"]);
  });

  it("returns an empty list for no members / no peer", () => {
    expect(participantAgentIds({})).toEqual([]);
    expect(participantAgentIds({ isDM: false, members: [] })).toEqual([]);
  });
});

describe("isSummaryUnavailable", () => {
  it("detects the failure sentinel verbatim", () => {
    expect(isSummaryUnavailable(SUMMARY_UNAVAILABLE_TEXT)).toBe(true);
    expect(isSummaryUnavailable("[interaction summary unavailable]")).toBe(true);
  });

  it("treats a real summary (and a blank) as available/absent, not the sentinel", () => {
    expect(isSummaryUnavailable("A real synthesis.")).toBe(false);
    expect(isSummaryUnavailable("")).toBe(false);
    expect(isSummaryUnavailable(undefined)).toBe(false);
  });
});
