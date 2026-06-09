import { describe, it, expect, vi, afterEach } from "vitest";
import { getClosedInteractions, ApiError } from "./api.js";

// getClosedInteractions reads the v0.3.8 interaction-summary surface
// (RFC 0020 §C/§D) — GET /api/v1/agents/{id}/interactions/closed. The
// summary affordance in the conversation view consumes it. The endpoint is
// per-agent (each persona persists its own episodic summary at close), so the
// web surface queries the channel's participating agents and merges.
afterEach(() => {
  vi.restoreAllMocks();
});

function jsonResponse(body, ok = true, status = 200) {
  return { ok, status, json: () => Promise.resolve(body) };
}

function envelope(overrides = {}) {
  return {
    interactions: [
      {
        interaction_id: "int-1",
        scope: "group:planning",
        started_at: 1717500000,
        closed_at: 1717500600,
        turn_count: 6,
        close_reason: "structural",
        summary: "The group agreed to ship the cache layer first.",
        participants: ["ember-owl", "iron-fox"],
      },
    ],
    ...overrides,
  };
}

describe("getClosedInteractions", () => {
  it("fetches the agent's closed interactions and returns the envelope", async () => {
    const body = envelope();
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(body)));
    vi.stubGlobal("fetch", fetchMock);

    const result = await getClosedInteractions("ember-owl");

    expect(result).toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/agents/ember-owl/interactions/closed",
      expect.objectContaining({
        headers: expect.objectContaining({ "X-Agent-ID": "web-console" }),
      }),
    );
  });

  it("appends scope, interaction_id, limit and min_turns only when supplied", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(envelope())));
    vi.stubGlobal("fetch", fetchMock);

    await getClosedInteractions("ember-owl", {
      scope: "group:planning",
      interactionId: "int-1",
      limit: 1,
      minTurns: 2,
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/agents/ember-owl/interactions/closed" +
        "?scope=group%3Aplanning&interaction_id=int-1&limit=1&min_turns=2",
    );

    await getClosedInteractions("ember-owl", { scope: "group:planning" });
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/v1/agents/ember-owl/interactions/closed?scope=group%3Aplanning",
    );
  });

  it("encodes the agent id into the request path", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(envelope())));
    vi.stubGlobal("fetch", fetchMock);

    await getClosedInteractions("dm:alice:bob");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/agents/dm%3Aalice%3Abob/interactions/closed",
    );
  });

  it("throws an ApiError when the endpoint responds non-2xx", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({}, false, 404)));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getClosedInteractions("missing")).rejects.toBeInstanceOf(
      ApiError,
    );
  });
});
