import { describe, it, expect } from "vitest";
import {
  shortAgentName,
  nameList,
  thinkingPhrase,
  mergeThinking,
} from "./presence.js";

const AGENTS = {
  "ember-owl": { id: "ember-owl", name: "Ember Owl", role: "Strategist" },
  "crimson-fox": { id: "crimson-fox", name: "Crimson Fox" },
};

describe("shortAgentName", () => {
  it("uses the bare persona name, not the name — role decoration", () => {
    // The bar is glanceable; the role is noise here (unlike the picker label).
    expect(shortAgentName("ember-owl", AGENTS)).toBe("Ember Owl");
  });

  it("falls back to the raw id for an agent absent from the map", () => {
    expect(shortAgentName("ghost", AGENTS)).toBe("ghost");
    expect(shortAgentName("ghost", {})).toBe("ghost");
  });
});

describe("nameList", () => {
  it("is empty for no ids", () => {
    expect(nameList([], AGENTS)).toBe("");
  });

  it("renders a single name", () => {
    expect(nameList(["ember-owl"], AGENTS)).toBe("Ember Owl");
  });

  it("joins two names with 'and'", () => {
    expect(nameList(["ember-owl", "crimson-fox"], AGENTS)).toBe(
      "Ember Owl and Crimson Fox",
    );
  });

  it("collapses three or more to an 'N agents' tally", () => {
    expect(nameList(["ember-owl", "crimson-fox", "ghost"], AGENTS)).toBe(
      "3 agents",
    );
  });

  it("ignores blank ids", () => {
    expect(nameList(["ember-owl", "", null], AGENTS)).toBe("Ember Owl");
  });
});

describe("thinkingPhrase", () => {
  it("is empty when nobody is working", () => {
    expect(thinkingPhrase([], AGENTS)).toBe("");
  });

  it("agrees the verb with a single subject", () => {
    expect(thinkingPhrase(["ember-owl"], AGENTS)).toBe(
      "Ember Owl is thinking…",
    );
  });

  it("agrees the verb with a plural subject", () => {
    expect(thinkingPhrase(["ember-owl", "crimson-fox"], AGENTS)).toBe(
      "Ember Owl and Crimson Fox are thinking…",
    );
  });

  it("softens the tail once a turn passes the slow threshold", () => {
    expect(thinkingPhrase(["ember-owl"], AGENTS, { slow: true })).toBe(
      "Ember Owl is taking a while…",
    );
  });

  it("pluralises the slow tail too", () => {
    expect(
      thinkingPhrase(["ember-owl", "crimson-fox"], AGENTS, { slow: true }),
    ).toBe("Ember Owl and Crimson Fox are taking a while…");
  });
});

describe("mergeThinking", () => {
  // The two-source reconciliation behind Tier 1: fold the server's authoritative
  // /activity set together with the still-in-grace optimistic ids the console
  // added for its own just-fired turns, and report which optimistic entries have
  // expired so the controller can drop them.
  const grace = (id, expiresAt) => ({ id, expiresAt });

  it("returns the server set alone, sorted, when there is no optimism", () => {
    expect(mergeThinking(["ember-owl", "crimson-fox"], [], 100)).toEqual({
      ids: ["crimson-fox", "ember-owl"],
      expired: [],
    });
  });

  it("unions an unexpired optimistic id the server has not yet confirmed", () => {
    expect(mergeThinking([], [grace("ember-owl", 200)], 100)).toEqual({
      ids: ["ember-owl"],
      expired: [],
    });
  });

  it("drops and reports an optimistic id whose grace has lapsed", () => {
    expect(mergeThinking([], [grace("ember-owl", 50)], 100)).toEqual({
      ids: [],
      expired: ["ember-owl"],
    });
  });

  it("keeps a sticky (Infinity) optimistic id forever", () => {
    // A DM add is sticky — no /activity poll confirms it, so it must not lapse.
    expect(mergeThinking([], [grace("ada", Infinity)], 1e12)).toEqual({
      ids: ["ada"],
      expired: [],
    });
  });

  it("de-dupes an id present in both the server set and grace", () => {
    expect(mergeThinking(["ember-owl"], [grace("ember-owl", 200)], 100)).toEqual({
      ids: ["ember-owl"],
      expired: [],
    });
  });

  it("tolerates empty / missing inputs", () => {
    expect(mergeThinking()).toEqual({ ids: [], expired: [] });
    expect(mergeThinking(null, null, 0)).toEqual({ ids: [], expired: [] });
  });
});
