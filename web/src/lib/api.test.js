import { describe, it, expect, vi, afterEach } from "vitest";
import {
  loadBootstrap,
  listAgents,
  sendChat,
  listChannels,
  getChannelHistory,
  publishMessage,
  ApiError,
} from "./api.js";

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

describe("listAgents", () => {
  it("fetches the agent list and returns the array", async () => {
    const agents = [
      { id: "alice", name: "Alice", status: "healthy" },
      { id: "bob", name: "Bob", status: "healthy" },
    ];
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(agents)));
    vi.stubGlobal("fetch", fetchMock);

    const result = await listAgents();

    expect(result).toEqual(agents);
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/agents");
  });

  it("throws an ApiError when the list endpoint responds non-2xx", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({}, false, 500)));
    vi.stubGlobal("fetch", fetchMock);

    await expect(listAgents()).rejects.toBeInstanceOf(ApiError);
  });
});

describe("sendChat", () => {
  // The chat endpoint is the one write Slice 1's chat panel issues. The client
  // owns the wire contract the panel must not get wrong: the path, the JSON
  // content-type the handler's requireJSON guard demands, the
  // participant_type:"user" tag (RFC 0011 amendment — omitting it would record
  // the human peer as an agent), and the /ui/context-derived user_id (RFC §F
  // rule 1 — never free-text). The panel passes intent; the client serialises it.
  function chatReply(overrides = {}) {
    return {
      reply: "Hello there.",
      chat_session_id: "cs-1",
      agent_id: "alice",
      timestamp: 1717000000,
      agent_display_name: "Alice",
      reply_status: "ok",
      ...overrides,
    };
  }

  it("POSTs JSON to the agent's chat route and returns the parsed reply", async () => {
    const reply = chatReply();
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(reply)));
    vi.stubGlobal("fetch", fetchMock);

    const result = await sendChat("alice", { message: "Hi", userId: "local" });

    expect(result).toEqual(reply);
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/api/v1/agents/alice/chat");
    expect(init.method).toBe("POST");
    expect(init.headers["Content-Type"]).toBe("application/json");
  });

  it("encodes the agent id into the request path", async () => {
    // The id is interpolated into the URL path; in practice it comes from the
    // server's own agent list (a constrained registry key), but encoding it
    // keeps the client robust against any id carrying a path-significant
    // character rather than leaning on that assumption.
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(chatReply())));
    vi.stubGlobal("fetch", fetchMock);

    await sendChat("a/b c", { message: "Hi", userId: "local" });

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/agents/a%2Fb%20c/chat");
  });

  it("sends the message, the context user_id, and participant_type:user", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(chatReply())));
    vi.stubGlobal("fetch", fetchMock);

    await sendChat("alice", { message: "Hi", userId: "local" });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.message).toBe("Hi");
    expect(body.user_id).toBe("local");
    expect(body.participant_type).toBe("user");
  });

  it("passes session_id and epoch_id through only when supplied", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(chatReply())));
    vi.stubGlobal("fetch", fetchMock);

    await sendChat("alice", {
      message: "Hi",
      userId: "local",
      sessionId: "sess-7",
      epochId: "ep-3",
    });
    const withOverrides = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(withOverrides.session_id).toBe("sess-7");
    expect(withOverrides.epoch_id).toBe("ep-3");

    await sendChat("alice", { message: "Hi", userId: "local" });
    const without = JSON.parse(fetchMock.mock.calls[1][1].body);
    expect("session_id" in without).toBe(false);
    expect("epoch_id" in without).toBe(false);
  });

  it("surfaces the server error envelope (message + code) on a 4xx", async () => {
    // chat_handler.go rejects an over-length message with a {error, code}
    // envelope; the panel must show the server's own wording, not a generic
    // failure, so the client lifts `error` onto the ApiError message and `code`
    // onto the instance.
    const envelope = {
      error: "message exceeds maximum length of 4000 characters",
      code: "BAD_REQUEST",
    };
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse(envelope, false, 400)),
    );
    vi.stubGlobal("fetch", fetchMock);

    const error = await sendChat("alice", { message: "x", userId: "local" }).catch(
      (e) => e,
    );

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(400);
    expect(error.code).toBe("BAD_REQUEST");
    expect(error.message).toContain("maximum length");
  });

  it("wraps a transport failure as an ApiError with status 0", async () => {
    const cause = new TypeError("Failed to fetch");
    const fetchMock = vi.fn(() => Promise.reject(cause));
    vi.stubGlobal("fetch", fetchMock);

    const error = await sendChat("alice", { message: "Hi", userId: "local" }).catch(
      (e) => e,
    );

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(0);
    expect(error.cause).toBe(cause);
  });
});

describe("listChannels", () => {
  it("fetches the channel list and returns the parsed envelope", async () => {
    // GET /api/v1/channels returns {channels, next_cursor} (channel_types.go
    // listChannelsResponse), not a bare array — the panel reads `.channels`, so
    // the client returns the envelope verbatim rather than unwrapping it (and
    // discarding the cursor a later slice may paginate on).
    const envelope = {
      channels: [
        { id: "general", name: "General", channel_type: "group" },
        { id: "ops", name: "Ops", channel_type: "group" },
      ],
      next_cursor: "ops",
    };
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(envelope)));
    vi.stubGlobal("fetch", fetchMock);

    const result = await listChannels();

    expect(result).toEqual(envelope);
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/channels");
  });

  it("throws an ApiError when the list endpoint responds non-2xx", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({}, false, 503)));
    vi.stubGlobal("fetch", fetchMock);

    await expect(listChannels()).rejects.toBeInstanceOf(ApiError);
  });
});

describe("getChannelHistory", () => {
  function history(overrides = {}) {
    return {
      messages: [
        {
          id: "m2",
          channel_id: "general",
          sender_id: "alice",
          content: "second",
          timestamp: "2026-06-02T10:00:02Z",
          mentions: [],
        },
        {
          id: "m1",
          channel_id: "general",
          sender_id: "bob",
          content: "first",
          timestamp: "2026-06-02T10:00:01Z",
          mentions: [],
        },
      ],
      ...overrides,
    };
  }

  it("fetches a channel's history and returns the parsed envelope", async () => {
    const body = history();
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(body)));
    vi.stubGlobal("fetch", fetchMock);

    const result = await getChannelHistory("general");

    expect(result).toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/channels/general/messages");
  });

  it("appends limit and before as query params only when supplied", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(history())));
    vi.stubGlobal("fetch", fetchMock);

    await getChannelHistory("general", {
      limit: 50,
      before: "2026-06-02T10:00:00Z",
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/channels/general/messages?limit=50&before=2026-06-02T10%3A00%3A00Z",
    );

    await getChannelHistory("general", { limit: 25 });
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/v1/channels/general/messages?limit=25",
    );
  });

  it("encodes the channel id into the request path", async () => {
    // DM channel ids carry colons (`dm:a:b`); encoding keeps the request pinned
    // to the {id}/messages route for any id rather than leaning on ids being
    // path-safe slugs.
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(history())));
    vi.stubGlobal("fetch", fetchMock);

    await getChannelHistory("dm:alice:bob");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/channels/dm%3Aalice%3Abob/messages",
    );
  });

  it("throws an ApiError when history responds non-2xx", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({}, false, 404)));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getChannelHistory("missing")).rejects.toBeInstanceOf(ApiError);
  });
});

describe("publishMessage", () => {
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

  it("POSTs JSON to the channel's messages route and returns the stored message", async () => {
    const msg = published();
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(msg, true, 201)));
    vi.stubGlobal("fetch", fetchMock);

    const result = await publishMessage("general", {
      senderId: "local",
      content: "hello channel",
    });

    expect(result).toEqual(msg);
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/api/v1/channels/general/messages");
    expect(init.method).toBe("POST");
    expect(init.headers["Content-Type"]).toBe("application/json");
  });

  it("sends the content and the context-derived sender_id", async () => {
    // publishMessageRequest requires sender_id (channel_handlers.go); the console
    // publishes as the /ui/context principal threaded in as userId, never a
    // free-text sender (RFC §F rule 1).
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse(published(), true, 201)),
    );
    vi.stubGlobal("fetch", fetchMock);

    await publishMessage("general", { senderId: "local", content: "hi" });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.sender_id).toBe("local");
    expect(body.content).toBe("hi");
  });

  it("encodes the channel id into the request path", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse(published(), true, 201)),
    );
    vi.stubGlobal("fetch", fetchMock);

    await publishMessage("dm:alice:bob", { senderId: "local", content: "hi" });

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/channels/dm%3Aalice%3Abob/messages",
    );
  });

  it("surfaces the server error envelope on a 4xx", async () => {
    const envelope = { error: "content is required", code: "BAD_REQUEST" };
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse(envelope, false, 400)),
    );
    vi.stubGlobal("fetch", fetchMock);

    const error = await publishMessage("general", {
      senderId: "local",
      content: "",
    }).catch((e) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(400);
    expect(error.code).toBe("BAD_REQUEST");
  });
});
