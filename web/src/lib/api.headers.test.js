import { describe, it, expect, vi, afterEach } from "vitest";
import { publishMessage, removeChannelMember } from "./api.js";

// The console attaches its X-Agent-ID on every request so the orchestrator
// buckets the UI's rate limit under `web-console` rather than the shared
// `anonymous` bucket (see api.js CONSOLE_AGENT_ID). The read-path coverage
// lives in api.test.js (the GET assertions also pin the header); this file
// covers the write paths, where the header must merge with an existing
// Content-Type and survive a bodyless request.
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

describe("X-Agent-ID console identity", () => {
  it("POSTs carry both Content-Type and the console agent id", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse({ id: "m1" }, true, 201)),
    );
    vi.stubGlobal("fetch", fetchMock);

    await publishMessage("general", { senderId: "local", content: "hi" });

    const init = fetchMock.mock.calls[0][1];
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(init.headers["X-Agent-ID"]).toBe("web-console");
  });

  it("bodyless DELETEs carry the console agent id and no Content-Type", async () => {
    const fetchMock = vi.fn(() => Promise.resolve({ ok: true, status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await removeChannelMember("general", "alice");

    const init = fetchMock.mock.calls[0][1];
    expect(init.method).toBe("DELETE");
    expect(init.headers["X-Agent-ID"]).toBe("web-console");
    expect("Content-Type" in init.headers).toBe(false);
  });
});
