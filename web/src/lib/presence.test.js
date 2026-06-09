import { describe, it, expect } from "vitest";
import {
  shortAgentName,
  nameList,
  thinkingPhrase,
  pruneThinking,
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

describe("pruneThinking", () => {
  const msg = (sender) => ({ id: `m-${sender}`, sender_id: sender });

  it("drops a persona that has since posted", () => {
    expect(
      pruneThinking(["ember-owl", "crimson-fox"], [msg("ember-owl")]),
    ).toEqual(["crimson-fox"]);
  });

  it("keeps everyone when no addressed persona has posted", () => {
    expect(pruneThinking(["ember-owl"], [msg("local"), msg("crimson-fox")])).toEqual([
      "ember-owl",
    ]);
  });

  it("returns empty once every addressed persona has replied", () => {
    expect(
      pruneThinking(["ember-owl"], [msg("ember-owl")]),
    ).toEqual([]);
  });

  it("tolerates empty inputs", () => {
    expect(pruneThinking([], [msg("ember-owl")])).toEqual([]);
    expect(pruneThinking(["ember-owl"], [])).toEqual(["ember-owl"]);
    expect(pruneThinking(["ember-owl"], undefined)).toEqual(["ember-owl"]);
  });
});
