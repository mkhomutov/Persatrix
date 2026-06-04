import { describe, it, expect, vi, afterEach } from "vitest";
import { publishMessage } from "./api.js";

// publishMessage's mention wiring (RFC 0011 over the console): the composer lifts
// `@id` tokens into a `mentions` array and the client forwards them — but only
// when non-empty, so a plain publish keeps the pre-feature wire shape (the server
// field is `omitempty`). Split from api.test.js to keep each spec under the
// review-size cap, mirroring api.create.test.js.

afterEach(() => {
  vi.restoreAllMocks();
});

function jsonResponse(body, ok = true, status = 200) {
  return {
    ok,
    status,
    json: () => Promise.resolve(body),
  };
}

function published(overrides = {}) {
  return {
    id: "m3",
    channel_id: "general",
    sender_id: "local",
    content: "hello channel",
    timestamp: "2026-06-02T10:00:03Z",
    mentions: [],
    ...overrides,
  };
}

describe("publishMessage mentions", () => {
  it("sends the mentions array when the publish carries resolved mentions", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse(published(), true, 201)),
    );
    vi.stubGlobal("fetch", fetchMock);

    await publishMessage("general", {
      senderId: "local",
      content: "@ember-owl your read?",
      mentions: ["ember-owl"],
    });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.mentions).toEqual(["ember-owl"]);
  });

  it("omits the mentions key entirely when there are none", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse(published(), true, 201)),
    );
    vi.stubGlobal("fetch", fetchMock);

    await publishMessage("general", { senderId: "local", content: "hi" });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body).not.toHaveProperty("mentions");
  });
});
