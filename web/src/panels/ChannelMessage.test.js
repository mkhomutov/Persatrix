import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/svelte";
import ChannelMessage from "./ChannelMessage.svelte";

// ChannelMessage renders one timeline row. Beyond sender/timestamp decoration it
// highlights the `@id` tokens that resolved to one of the message's stored
// mentions (RFC 0011 over the console), so the operator sees who a post pinged.
afterEach(cleanup);

function message(overrides = {}) {
  return {
    id: "m1",
    channel_id: "group:planning",
    sender_id: "local",
    content: "hello",
    timestamp: "2026-06-04T10:00:00Z",
    mentions: [],
    ...overrides,
  };
}

function renderRow(overrides) {
  return render(ChannelMessage, {
    props: {
      message: message(overrides),
      userId: "local",
      agentsById: {},
    },
  });
}

describe("ChannelMessage mention highlighting", () => {
  it("wraps a resolved @mention in a .mention element", () => {
    const { container } = renderRow({
      content: "what's your read @ember-owl?",
      mentions: ["ember-owl"],
    });

    const mention = container.querySelector(".content .mention");
    expect(mention).toBeTruthy();
    expect(mention.textContent).toBe("@ember-owl");
    // The surrounding prose is preserved verbatim around the highlight.
    expect(container.querySelector(".content").textContent).toBe(
      "what's your read @ember-owl?",
    );
  });

  it("does not highlight an @token that isn't in the stored mentions", () => {
    const { container } = renderRow({
      content: "ping @ember-owl",
      mentions: [],
    });

    expect(container.querySelector(".mention")).toBeNull();
    expect(container.querySelector(".content").textContent).toBe(
      "ping @ember-owl",
    );
  });

  it("renders plain content unchanged with no highlight", () => {
    const { container } = renderRow({ content: "just text" });
    expect(container.querySelector(".mention")).toBeNull();
    expect(container.querySelector(".content").textContent).toBe("just text");
  });
});
