import { describe, it, expect, vi, afterEach } from "vitest";
import { listChannels, ApiError } from "./api.js";

// GET /api/v1/channels pages by 50 in channel-id order and hands back a
// `next_cursor` while more rows exist (ISSUE-0015, channel_handlers.go). DM ids
// (`dm:…`) sort ahead of group ids (`group:…`), so a console that read only the
// first page lost every group channel once a deployment held 50 DMs. The client
// therefore walks the cursor and returns every channel in one envelope.
afterEach(() => {
  vi.restoreAllMocks();
});

function jsonResponse(body, ok = true, status = 200) {
  return { ok, status, json: () => Promise.resolve(body) };
}

const ch = (id) => ({ id, channel_type: id.startsWith("dm:") ? "dm" : "group" });

describe("listChannels", () => {
  it("returns a single page as-is when the server has no more", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse({ channels: [ch("group:general")] })),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await listChannels();

    expect(result).toEqual({ channels: [ch("group:general")] });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // The server's largest page (channelMaxLimit), so one request covers a
    // thousand channels instead of fifty against the console's rate limit.
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/channels?limit=1000");
  });

  it("follows next_cursor until the last page and concatenates every page", async () => {
    const pages = {
      "/api/v1/channels?limit=1000": {
        channels: [ch("dm:a:x"), ch("dm:b:x")],
        next_cursor: "dm:b:x",
      },
      "/api/v1/channels?limit=1000&cursor=dm%3Ab%3Ax": {
        channels: [ch("group:general"), ch("group:ops")],
      },
    };
    const fetchMock = vi.fn((url) => Promise.resolve(jsonResponse(pages[url])));
    vi.stubGlobal("fetch", fetchMock);

    const result = await listChannels();

    expect(result.channels.map((c) => c.id)).toEqual([
      "dm:a:x",
      "dm:b:x",
      "group:general",
      "group:ops",
    ]);
    expect(result.next_cursor).toBeUndefined();
    expect(fetchMock.mock.calls.map((c) => c[0])).toEqual([
      "/api/v1/channels?limit=1000",
      "/api/v1/channels?limit=1000&cursor=dm%3Ab%3Ax",
    ]);
  });

  it("stops rather than loop when the cursor does not advance, listing each channel once", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse({ channels: [ch("group:a")], next_cursor: "group:a" })),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await listChannels();

    // First page, then one follow-up that returns the same cursor: stop there.
    // The repeated page must not list `group:a` twice — the sidebar keys its
    // rows by id.
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.channels.map((c) => c.id)).toEqual(["group:a"]);
  });

  it("bounds the walk for a server that never runs out of pages", async () => {
    let n = 0;
    const fetchMock = vi.fn(() => {
      n += 1;
      return Promise.resolve(
        jsonResponse({ channels: [ch(`group:c${n}`)], next_cursor: `group:c${n}` }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    await listChannels();

    expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(40);
  });

  it("throws an ApiError when any page responds non-2xx", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ channels: [ch("dm:a:x")], next_cursor: "dm:a:x" }))
      .mockResolvedValueOnce(jsonResponse({}, false, 503));
    vi.stubGlobal("fetch", fetchMock);

    await expect(listChannels()).rejects.toBeInstanceOf(ApiError);
  });
});
