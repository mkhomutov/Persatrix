import { describe, it, expect, vi, afterEach } from "vitest";
import { loadBootstrap, ApiError } from "./api.js";

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

describe("loadBootstrap", () => {
  it("fetches config and context and returns both", async () => {
    const config = { panels: { chat: { enabled: true, available: true } } };
    const context = { principal: "local", authenticated: false };
    const fetchMock = vi.fn((url) =>
      Promise.resolve(
        url.endsWith("/config")
          ? jsonResponse(config)
          : jsonResponse(context),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await loadBootstrap();

    expect(result).toEqual({ config, context });
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/ui/config");
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/ui/context");
  });

  it("throws an ApiError when an endpoint responds non-2xx", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse({}, false, 503)),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(loadBootstrap()).rejects.toBeInstanceOf(ApiError);
  });

  it("wraps a transport failure as an ApiError with status 0 and the underlying cause", async () => {
    // fetch rejecting (DNS failure, offline, CORS) is distinct from a non-2xx
    // response: status 0 marks "couldn't reach the backend at all", and the
    // original error must be preserved as `cause` for diagnosis rather than
    // silently dropped.
    const cause = new TypeError("Failed to fetch");
    const fetchMock = vi.fn(() => Promise.reject(cause));
    vi.stubGlobal("fetch", fetchMock);

    const error = await loadBootstrap().catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(0);
    expect(error.cause).toBe(cause);
  });

  it("wraps a 2xx response with a malformed JSON body as an ApiError", async () => {
    // A reachable backend can still return a 2xx with a non-JSON body (e.g. a
    // proxy or error page served as 200). The raw SyntaxError from .json() must
    // be wrapped so the module's "all client failures are ApiError" contract
    // holds (PRs 4-5 lean on it); the HTTP status is preserved and the parse
    // error threaded through `cause`.
    const cause = new SyntaxError("Unexpected token < in JSON");
    const fetchMock = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.reject(cause),
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const error = await loadBootstrap().catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(200);
    expect(error.cause).toBe(cause);
  });
});
