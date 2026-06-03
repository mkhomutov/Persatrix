import { describe, it, expect, vi, afterEach } from "vitest";
import { createChannel, ApiError } from "./api.js";

// createChannel wire-contract tests (RFC 0048 channel-creation amendment §B).
// Split from api.test.js to keep each spec under the review-size cap.

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

describe("createChannel", () => {
  // The server derives the canonical id group:<name> from the name and returns
  // the created channel with 201 (channel_handlers.go handleCreateChannel). The
  // client passes name/description/members verbatim — never prepending group:,
  // which would yield group:group:<name> (RFC 0048 channel-creation amendment §B).
  function created(overrides = {}) {
    return {
      id: "group:standup",
      name: "standup",
      channel_type: "group",
      description: "",
      ...overrides,
    };
  }

  it("POSTs JSON to the channels route and returns the created channel", async () => {
    const channel = created();
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse(channel, true, 201)),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await createChannel({
      name: "standup",
      members: [{ id: "ada", respond: "when_mentioned" }],
    });

    expect(result).toEqual(channel);
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/api/v1/channels");
    expect(init.method).toBe("POST");
    expect(init.headers["Content-Type"]).toBe("application/json");
  });

  it("sends name, description, and the members array verbatim (no group: prefix)", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse(created(), true, 201)),
    );
    vi.stubGlobal("fetch", fetchMock);

    await createChannel({
      name: "standup",
      description: "daily sync",
      members: [
        { id: "ada", respond: "always" },
        { id: "bob", respond: "when_mentioned" },
      ],
    });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.name).toBe("standup");
    expect(body.description).toBe("daily sync");
    expect(body.members).toEqual([
      { id: "ada", respond: "always" },
      { id: "bob", respond: "when_mentioned" },
    ]);
  });

  it("omits description when not supplied", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse(created(), true, 201)),
    );
    vi.stubGlobal("fetch", fetchMock);

    await createChannel({
      name: "standup",
      members: [{ id: "ada", respond: "when_mentioned" }],
    });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect("description" in body).toBe(false);
  });

  it("surfaces the server conflict envelope on a 409 (duplicate name)", async () => {
    const envelope = {
      error: "channel group:standup already exists",
      code: "CONFLICT",
    };
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse(envelope, false, 409)),
    );
    vi.stubGlobal("fetch", fetchMock);

    const error = await createChannel({
      name: "standup",
      members: [{ id: "ada", respond: "when_mentioned" }],
    }).catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(409);
    expect(error.code).toBe("CONFLICT");
    expect(error.message).toMatch(/already exists/);
  });
});
