import { describe, it, expect, vi, afterEach } from "vitest";
import { addChannelMember, removeChannelMember, ApiError } from "./api.js";

// Member add/remove wire-contract tests (RFC 0011 §C). Split from api.test.js
// to keep each spec under the review-size cap. Both endpoints answer 204 No
// Content, so the client must not parse a success body — it resolves with no
// value and only reads a body on the non-2xx error path.

afterEach(() => {
  vi.restoreAllMocks();
});

function noContent() {
  // 204: ok, no body. .json() is present only so a regression that tries to
  // parse the success body would resolve to null rather than throw — the test
  // asserts the resolved value is undefined, proving we don't read it.
  return { ok: true, status: 204, json: () => Promise.resolve(null) };
}

function errorResponse(body, status) {
  return { ok: false, status, json: () => Promise.resolve(body) };
}

describe("addChannelMember", () => {
  it("POSTs {id, respond} to the members route and resolves with no value", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(noContent()));
    vi.stubGlobal("fetch", fetchMock);

    const result = await addChannelMember("group:planning", {
      id: "ada",
      respond: "chair",
    });

    expect(result).toBeUndefined();
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/api/v1/channels/group%3Aplanning/members");
    expect(init.method).toBe("POST");
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(init.body)).toEqual({ id: "ada", respond: "chair" });
  });

  it("surfaces the server error envelope on a non-2xx (409 already a member)", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        errorResponse({ error: "already a member", code: "CONFLICT" }, 409),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const err = await addChannelMember("group:planning", {
      id: "ada",
      respond: "always",
    }).catch((e) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(409);
    expect(err.message).toBe("already a member");
    expect(err.code).toBe("CONFLICT");
  });
});

describe("removeChannelMember", () => {
  it("DELETEs the encoded participant path with no body or Content-Type", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(noContent()));
    vi.stubGlobal("fetch", fetchMock);

    const result = await removeChannelMember("group:planning", "ada");

    expect(result).toBeUndefined();
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/api/v1/channels/group%3Aplanning/members/ada");
    expect(init.method).toBe("DELETE");
    expect(init.body).toBeUndefined();
    expect(init.headers).toBeUndefined();
  });

  it("surfaces an ApiError on a 404 (channel or member absent)", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(errorResponse({ error: "no such channel" }, 404)),
    );
    vi.stubGlobal("fetch", fetchMock);

    const err = await removeChannelMember("group:x", "ada").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(404);
  });
});
