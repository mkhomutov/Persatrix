import { describe, it, expect, vi, afterEach } from "vitest";
import { onUnauthorized } from "./auth.js";
import { listAgents, ApiError } from "./api.js";

// The 401 seam (RFC 0039 enabled mode): every console data call flows
// through api.js's errorFromResponse, which reports a 401 through the
// auth.js listener — the single trigger for the shell's login state.
afterEach(() => {
  onUnauthorized(null);
  vi.restoreAllMocks();
});

describe("onUnauthorized seam", () => {
  it("fires the listener when a console call answers 401", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: false,
          status: 401,
          json: () =>
            Promise.resolve({ error: "authentication required" }),
        }),
      ),
    );
    const listener = vi.fn();
    onUnauthorized(listener);

    await expect(listAgents()).rejects.toBeInstanceOf(ApiError);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("stays quiet on non-401 failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: false,
          status: 503,
          json: () => Promise.resolve({ error: "unavailable" }),
        }),
      ),
    );
    const listener = vi.fn();
    onUnauthorized(listener);

    await expect(listAgents()).rejects.toBeInstanceOf(ApiError);
    expect(listener).not.toHaveBeenCalled();
  });
});
