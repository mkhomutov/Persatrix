import { describe, it, expect, vi, afterEach } from "vitest";
import { getChannelConfig, patchChannelConfig, ApiError } from "./api.js";

// Channel governance-config wire-contract tests (RFC 0050 Phase 2 PR 1). Split
// from api.test.js to keep each spec under the review-size cap. These pin the
// two contract details the panel (PR 2) cannot get wrong: PATCH must carry the
// last-read revision in the bare-integer `If-Match` header, and the full status
// set the surface declares (403/409/428/400/404/503) must survive onto ApiError
// with its status intact — the panel branches on 409 (reload, don't overwrite),
// so a status that collapsed to a generic message would break that path.

afterEach(() => {
  vi.restoreAllMocks();
});

function jsonResponse(body, ok = true, status = 200) {
  return { ok, status, json: () => Promise.resolve(body) };
}

// A representative GET/PATCH success body: revision + the eight knobs, each a
// {value, source} pair. interaction_budget_tokens is the inherited-null case
// (RFC 0050 Phase 1 Open item 4 — not router-held).
function configBody(overrides = {}) {
  return {
    revision: 3,
    floor_control: { value: true, source: "channel" },
    salience_max_channel_members: { value: 8, source: "default" },
    max_replies_per_participant_per_interaction: { value: 4, source: "default" },
    end_vote_threshold: { value: 2, source: "default" },
    end_vote_window: { value: 600, source: "default" },
    escalation_chair_id: { value: "ada", source: "channel" },
    interaction_idle_timeout_seconds: { value: 900, source: "default" },
    interaction_budget_tokens: { value: null, source: "default" },
    ...overrides,
  };
}

describe("getChannelConfig", () => {
  it("GETs the encoded config route and returns the effective-config body", async () => {
    const body = configBody();
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(body)));
    vi.stubGlobal("fetch", fetchMock);

    const result = await getChannelConfig("group:planning");

    expect(result).toEqual(body);
    const [path, init] = fetchMock.mock.calls[0];
    // Encode the id (canonical ids carry a type-prefix colon, e.g.
    // "group:planning") so the request stays pinned to the {id}/config route.
    expect(path).toBe("/api/v1/channels/group%3Aplanning/config");
    expect(init.method).toBeUndefined(); // a GET — fetch defaults the method
    // Attributed, never anonymous (api.js consoleHeaders).
    expect(init.headers).toEqual({ "X-Agent-ID": "web-console" });
  });

  it("surfaces a 403 (toggle off) as an ApiError carrying the server wording", async () => {
    const envelope = {
      error: "channel config editing is disabled",
      code: "FORBIDDEN",
    };
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse(envelope, false, 403)),
    );
    vi.stubGlobal("fetch", fetchMock);

    const err = await getChannelConfig("group:planning").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(403);
    expect(err.code).toBe("FORBIDDEN");
    expect(err.message).toMatch(/disabled/);
  });
});

describe("patchChannelConfig", () => {
  it("PATCHes the sparse body with the revision in a bare-integer If-Match header", async () => {
    const body = configBody({ revision: 4 });
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(body)));
    vi.stubGlobal("fetch", fetchMock);

    const result = await patchChannelConfig(
      "group:planning",
      { floor_control: false },
      3,
    );

    // A successful apply returns the new effective config (with the bumped
    // revision) so the caller can use it as the next If-Match without a second
    // round-trip.
    expect(result).toEqual(body);
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/api/v1/channels/group%3Aplanning/config");
    expect(init.method).toBe("PATCH");
    expect(init.headers["Content-Type"]).toBe("application/json");
    // The bare integer revision — not a quoted ETag, not a string with extra
    // chars (the server tolerates a quoted form but the contract is the bare int).
    expect(init.headers["If-Match"]).toBe("3");
    expect(init.headers["X-Agent-ID"]).toBe("web-console");
    expect(JSON.parse(init.body)).toEqual({ floor_control: false });
  });

  it("preserves an explicit null in the body (unset→inherit), not dropping the key", async () => {
    // The sparse-patch tri-state: an explicit null means unset→inherit, which is
    // distinct from an absent key (leave unchanged). JSON.stringify keeps null,
    // so the revert path must round-trip the key with a literal null value.
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(configBody())));
    vi.stubGlobal("fetch", fetchMock);

    await patchChannelConfig("group:planning", { escalation_chair_id: null }, 3);

    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body).toEqual({ escalation_chair_id: null });
    expect("escalation_chair_id" in body).toBe(true);
  });

  it("surfaces a 409 (revision conflict) as an ApiError with status 409 intact", async () => {
    // The panel branches on 409 to reload-not-overwrite, so the status MUST
    // survive onto ApiError rather than collapsing into a generic message.
    const envelope = {
      error: "config revision conflict",
      code: "CONFLICT",
    };
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse(envelope, false, 409)),
    );
    vi.stubGlobal("fetch", fetchMock);

    const err = await patchChannelConfig(
      "group:planning",
      { floor_control: false },
      2,
    ).catch((e) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(409);
    expect(err.code).toBe("CONFLICT");
  });

  it("surfaces a 428 (If-Match missing) as an ApiError with status 428 intact", async () => {
    // The client always sends If-Match, so a 428 should not normally occur — but
    // the status must still survive so a degraded path is diagnosable rather
    // than collapsing into a generic transport-style failure.
    const envelope = {
      error: "If-Match header with the current config revision is required",
      code: "PRECONDITION_REQUIRED",
    };
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse(envelope, false, 428)),
    );
    vi.stubGlobal("fetch", fetchMock);

    const err = await patchChannelConfig(
      "group:planning",
      { floor_control: false },
      3,
    ).catch((e) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(428);
    expect(err.code).toBe("PRECONDITION_REQUIRED");
  });

  it("surfaces a 400 (unknown knob / wrong type / unparseable If-Match)", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        jsonResponse(
          { error: "unknown config knob: floor", code: "BAD_REQUEST" },
          false,
          400,
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const err = await patchChannelConfig("group:planning", { floor: 1 }, 3).catch(
      (e) => e,
    );
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(400);
  });

  it("surfaces a 503 (channel store/router not wired) rather than crashing", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        jsonResponse(
          { error: "channel config surface not configured", code: "UNAVAILABLE" },
          false,
          503,
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const err = await patchChannelConfig(
      "group:planning",
      { floor_control: true },
      3,
    ).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(503);
  });

  it("surfaces a 404 (no such channel) as an ApiError with status 404 intact", async () => {
    // The merge precedes the write, so a PATCH against a missing channel 404s on
    // the current-overrides load (channel_config_handlers.go). The status must
    // survive so the panel can distinguish "channel gone" from a config error.
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        jsonResponse(
          { error: "channel not found", code: "NOT_FOUND" },
          false,
          404,
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const err = await patchChannelConfig(
      "group:missing",
      { floor_control: true },
      3,
    ).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(404);
    expect(err.code).toBe("NOT_FOUND");
  });

  it("accepts a revision of 0 and still sends the If-Match header", async () => {
    // A freshly-seeded channel can read back revision 0; the If-Match must be
    // sent as the literal "0", not omitted by a falsy-revision guard.
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(configBody())));
    vi.stubGlobal("fetch", fetchMock);

    await patchChannelConfig("group:planning", { floor_control: true }, 0);

    expect(fetchMock.mock.calls[0][1].headers["If-Match"]).toBe("0");
  });

  it("rejects a non-integer revision before issuing any request", async () => {
    // `revision` is REQUIRED and must be the integer last read. A missing or
    // garbage value would otherwise stringify to "undefined"/"NaN" in the
    // If-Match header and burn a round-trip on a guaranteed 400 — guard at the
    // call site instead, mirroring getChatHistory's missing-userId guard. (0 is
    // a legitimate revision and is covered above, so the guard must admit it.)
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    for (const bad of [undefined, null, NaN, 1.5, "3"]) {
      await expect(
        patchChannelConfig("group:planning", { floor_control: true }, bad),
      ).rejects.toThrow(/revision/);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
