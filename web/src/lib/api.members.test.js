import { describe, it, expect, vi, afterEach } from "vitest";
import { ApiError } from "./api.js";
import {
  addChannelMember,
  removeChannelMember,
  updateChannelMember,
} from "./api.members.js";

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

  // The add is idempotent server-side (memberships INSERT ... ON CONFLICT DO
  // NOTHING → 204), so there is NO "already a member" 409 path — a duplicate add
  // simply succeeds. The real non-2xx the client must surface is a 404 when the
  // channel does not exist (the store reports a foreign-key violation as
  // ErrChannelNotFound → NOT_FOUND).
  it("surfaces the server error envelope on a non-2xx (404 no such channel)", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        errorResponse({ error: "channel not found", code: "NOT_FOUND" }, 404),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const err = await addChannelMember("group:ghost", {
      id: "ada",
      respond: "always",
    }).catch((e) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(404);
    expect(err.message).toBe("channel not found");
    expect(err.code).toBe("NOT_FOUND");
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
    // A bodyless DELETE sends no Content-Type, but still carries the console's
    // X-Agent-ID so the write is attributed rather than anonymous (api.js
    // consoleHeaders).
    expect(init.headers).toEqual({ "X-Agent-ID": "web-console" });
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

// updateChannelMember is the RFC 0050 member-config edit
// (PATCH /api/v1/channels/{id}/members/{participant_id} → 204). Like add/remove
// it parses no success body. `respond` is REQUIRED by the server (a threshold-only
// edit is a 400) because salience_gated is derived from the declared disposition
// and is unrecoverable from persisted state — so the client always sends it.
describe("updateChannelMember", () => {
  it("PATCHes {respond, threshold} to the encoded participant route and resolves with no value", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(noContent()));
    vi.stubGlobal("fetch", fetchMock);

    const result = await updateChannelMember("group:planning", "ada", {
      respond: "participant",
      threshold: 0.6,
    });

    expect(result).toBeUndefined();
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/api/v1/channels/group%3Aplanning/members/ada");
    expect(init.method).toBe("PATCH");
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(init.body)).toEqual({
      respond: "participant",
      threshold: 0.6,
    });
  });

  it("sends an explicit null threshold to unset the salience bar", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(noContent()));
    vi.stubGlobal("fetch", fetchMock);

    await updateChannelMember("group:planning", "ada", {
      respond: "participant",
      threshold: null,
    });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body).toEqual({ respond: "participant", threshold: null });
  });

  it("surfaces the server error on a 400 (bad threshold) and a 404 (member absent)", async () => {
    const bad = vi.fn(() =>
      Promise.resolve(
        errorResponse(
          { error: "channels: invalid member threshold", code: "BAD_REQUEST" },
          400,
        ),
      ),
    );
    vi.stubGlobal("fetch", bad);
    const e400 = await updateChannelMember("group:planning", "ada", {
      respond: "participant",
      threshold: 5,
    }).catch((e) => e);
    expect(e400).toBeInstanceOf(ApiError);
    expect(e400.status).toBe(400);

    const missing = vi.fn(() =>
      Promise.resolve(errorResponse({ error: "member not found" }, 404)),
    );
    vi.stubGlobal("fetch", missing);
    const e404 = await updateChannelMember("group:planning", "ghost", {
      respond: "participant",
    }).catch((e) => e);
    expect(e404).toBeInstanceOf(ApiError);
    expect(e404.status).toBe(404);
  });
});
