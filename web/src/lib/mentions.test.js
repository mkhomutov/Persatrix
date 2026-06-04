import { describe, it, expect } from "vitest";
import {
  extractMentions,
  findActiveMention,
  applyMention,
  mentionCandidates,
  segmentMentions,
  buildPublishPayload,
  MAX_MENTIONS,
} from "./mentions.js";

const MEMBERS = [
  { id: "ember-owl" },
  { id: "iron-fox" },
  { id: "nova-sparrow" },
];

describe("extractMentions", () => {
  it("lifts @id tokens that resolve to a member", () => {
    const out = extractMentions(
      "what's your read @ember-owl? and @iron-fox?",
      MEMBERS,
    );
    expect(out).toEqual(["ember-owl", "iron-fox"]);
  });

  it("drops tokens that match no member (typo / @everyone)", () => {
    expect(extractMentions("hey @everyone and @ember-owl", MEMBERS)).toEqual([
      "ember-owl",
    ]);
  });

  it("de-dupes a repeated mention in first-seen order", () => {
    expect(
      extractMentions("@iron-fox @ember-owl @iron-fox", MEMBERS),
    ).toEqual(["iron-fox", "ember-owl"]);
  });

  it("matches at the start of the string", () => {
    expect(extractMentions("@ember-owl hi", MEMBERS)).toEqual(["ember-owl"]);
  });

  it("does not treat a mid-word or email @ as a mention", () => {
    expect(extractMentions("mail me at local@ember-owl.io", MEMBERS)).toEqual(
      [],
    );
  });

  it("accepts a Set of ids as the member source", () => {
    expect(extractMentions("@iron-fox", new Set(["iron-fox"]))).toEqual([
      "iron-fox",
    ]);
  });

  it("caps the lifted array at MAX_MENTIONS", () => {
    const many = Array.from({ length: MAX_MENTIONS + 5 }, (_, i) => ({
      id: `a${i}`,
    }));
    const content = many.map((m) => `@${m.id}`).join(" ");
    expect(extractMentions(content, many)).toHaveLength(MAX_MENTIONS);
  });

  it("returns [] for empty / nullish content", () => {
    expect(extractMentions("", MEMBERS)).toEqual([]);
    expect(extractMentions(undefined, MEMBERS)).toEqual([]);
  });
});

describe("findActiveMention", () => {
  it("returns the partial token the caret sits in", () => {
    const text = "hi @emb";
    expect(findActiveMention(text, text.length)).toEqual({
      start: 3,
      query: "emb",
    });
  });

  it("opens an empty-query menu right after a bare @", () => {
    const text = "hi @";
    expect(findActiveMention(text, text.length)).toEqual({
      start: 3,
      query: "",
    });
  });

  it("returns null when the caret is after a completed word", () => {
    const text = "hi @ember-owl now";
    expect(findActiveMention(text, text.length)).toBeNull();
  });

  it("returns null for a mid-word @ (email)", () => {
    const text = "local@ember";
    expect(findActiveMention(text, text.length)).toBeNull();
  });

  it("uses the caret, not the end of the string", () => {
    const text = "@ember-owl and more";
    // caret right after "@ember"
    expect(findActiveMention(text, 6)).toEqual({ start: 0, query: "ember" });
  });
});

describe("applyMention", () => {
  it("replaces the active partial with `@id ` and reports the caret", () => {
    const text = "hi @emb";
    const active = findActiveMention(text, text.length);
    const result = applyMention(text, active.start, text.length, "ember-owl");
    expect(result.text).toBe("hi @ember-owl ");
    expect(result.caret).toBe(result.text.length);
  });

  it("rewrites a token even when the caret is inside it, without doubling spaces", () => {
    const text = "@iron more";
    // caret after "@iron"; picking ember-owl consumes the whole token and reuses
    // the existing space rather than inserting a second one.
    const result = applyMention(text, 0, 5, "ember-owl");
    expect(result.text).toBe("@ember-owl more");
    expect(result.text[result.caret]).toBe("m"); // caret lands before "more"
  });
});

describe("mentionCandidates", () => {
  const agentsById = {
    "ember-owl": { id: "ember-owl", name: "Ember Owl", role: "Engineering" },
    "iron-fox": { id: "iron-fox", name: "Iron Fox", role: "Infra" },
  };

  it("returns the whole roster for an empty query, decorated", () => {
    const out = mentionCandidates("", MEMBERS, { agentsById });
    expect(out.map((c) => c.id)).toEqual([
      "ember-owl",
      "iron-fox",
      "nova-sparrow",
    ]);
    expect(out[0]).toMatchObject({ name: "Ember Owl", role: "Engineering" });
  });

  it("filters by id substring (case-insensitive)", () => {
    expect(mentionCandidates("FOX", MEMBERS, { agentsById })).toEqual([
      { id: "iron-fox", name: "Iron Fox", role: "Infra" },
    ]);
  });

  it("filters by display name too", () => {
    expect(
      mentionCandidates("ember", MEMBERS, { agentsById }).map((c) => c.id),
    ).toEqual(["ember-owl"]);
  });

  it("excludes the operator's own id", () => {
    const out = mentionCandidates("", MEMBERS, {
      agentsById,
      exclude: "ember-owl",
    });
    expect(out.map((c) => c.id)).not.toContain("ember-owl");
  });
});

describe("buildPublishPayload", () => {
  it("attaches resolved mentions alongside sender + content", () => {
    expect(
      buildPublishPayload("local", "@iron-fox ping", MEMBERS),
    ).toEqual({
      senderId: "local",
      content: "@iron-fox ping",
      mentions: ["iron-fox"],
    });
  });

  it("omits the mentions key for a plain post", () => {
    expect(buildPublishPayload("local", "status update", MEMBERS)).toEqual({
      senderId: "local",
      content: "status update",
    });
  });
});

describe("segmentMentions", () => {
  it("marks resolved mention tokens and leaves the rest plain", () => {
    const segs = segmentMentions("ping @ember-owl now", ["ember-owl"]);
    expect(segs).toEqual([
      { text: "ping ", mention: false },
      { text: "@ember-owl", mention: true },
      { text: " now", mention: false },
    ]);
  });

  it("does not highlight an @token absent from the stored mentions", () => {
    const segs = segmentMentions("ping @ember-owl", []);
    expect(segs).toEqual([{ text: "ping @ember-owl", mention: false }]);
  });

  it("returns a single plain segment for mention-free content", () => {
    expect(segmentMentions("just text", ["ember-owl"])).toEqual([
      { text: "just text", mention: false },
    ]);
  });

  it("handles a mention at the very start", () => {
    expect(segmentMentions("@iron-fox hi", ["iron-fox"])).toEqual([
      { text: "@iron-fox", mention: true },
      { text: " hi", mention: false },
    ]);
  });
});
