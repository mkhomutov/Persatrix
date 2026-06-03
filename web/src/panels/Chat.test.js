import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/svelte";
import Chat from "./Chat.svelte";

// The chat panel renders over today's synchronous chat API (RFC 0048 PR 4): it
// lists personas, sends a message as the context-derived user, and shows the
// reply. The backend client is mocked so the panel's wiring — picker, send,
// thinking state, error surfacing, session/epoch pass-through, and the §F
// identity rule — is exercised without a running orchestrator.
vi.mock("../lib/api.js", () => ({
  ApiError: class ApiError extends Error {
    constructor(message, status, options) {
      super(message, options);
      this.name = "ApiError";
      this.status = status;
    }
  },
  listAgents: vi.fn(),
  sendChat: vi.fn(),
  getChatHistory: vi.fn(),
}));

import {
  listAgents,
  sendChat,
  getChatHistory,
  ApiError,
} from "../lib/api.js";

const AGENTS = [
  { id: "alice", name: "Alice", status: "healthy" },
  { id: "bob", name: "Bob", status: "healthy" },
];

function reply(overrides = {}) {
  return {
    reply: "Hello, human.",
    chat_session_id: "cs-1",
    agent_id: "alice",
    timestamp: 1717000000,
    agent_display_name: "Alice",
    reply_status: "ok",
    ...overrides,
  };
}

beforeEach(() => {
  listAgents.mockResolvedValue(AGENTS);
  sendChat.mockResolvedValue(reply());
  // Default to no prior conversation (200-empty fresh start, §B). Tests that
  // exercise resume override this with a seeded history.
  getChatHistory.mockResolvedValue({ messages: [] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Chat panel", () => {
  it("populates the persona picker from GET /api/v1/agents on mount", async () => {
    render(Chat, { props: { userId: "local" } });

    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Alice" })).toBeTruthy();
    });
    expect(screen.getByRole("option", { name: "Bob" })).toBeTruthy();
    expect(listAgents).toHaveBeenCalledOnce();
  });

  it("shows a persona header with role and capabilities for the selected persona", async () => {
    listAgents.mockResolvedValue([
      {
        id: "ada",
        name: "Ada",
        role: "Researcher",
        capabilities: ["search", "summarize"],
        status: "healthy",
      },
    ]);
    render(Chat, { props: { userId: "local" } });

    // The header gives the conversation a face: name, role, and capability chips
    // — all from fields the agent DTO already serves (RFC 0048 amendment §A).
    await screen.findByText("Ada");
    expect(screen.getByText("Researcher")).toBeTruthy();
    expect(screen.getByText("search")).toBeTruthy();
    expect(screen.getByText("summarize")).toBeTruthy();
    // The picker option folds the role in too.
    expect(
      screen.getByRole("option", { name: "Ada — Researcher" }),
    ).toBeTruthy();
  });

  it("seeds the transcript from persisted history on persona-select (resume)", async () => {
    // The history endpoint returns newest-first; the panel renders oldest-top
    // (conversational), seeded so a reload resumes the conversation (§B).
    getChatHistory.mockResolvedValue({
      messages: [
        {
          id: "h2",
          channel_id: "dm:alice:local",
          sender_id: "alice",
          content: "earlier reply",
          timestamp: "2026-06-02T10:00:01Z",
        },
        {
          id: "h1",
          channel_id: "dm:alice:local",
          sender_id: "local",
          content: "earlier question",
          timestamp: "2026-06-02T10:00:00Z",
        },
      ],
    });
    render(Chat, { props: { userId: "local" } });

    // Seeded from the (user, agent) DM — fetched read-only for the default
    // persona with the context-derived user.
    await waitFor(() => {
      expect(getChatHistory).toHaveBeenCalledWith("alice", { userId: "local" });
    });
    const items = await screen.findAllByRole("listitem");
    // Oldest at top: the question (h1) precedes the reply (h2).
    expect(items[0].textContent).toMatch(/earlier question/);
    expect(items[1].textContent).toMatch(/earlier reply/);
    // The operator's own message reads as "You".
    expect(items[0].textContent).toMatch(/^You:/);
  });

  it("starts with an empty transcript when the persona has no history", async () => {
    getChatHistory.mockResolvedValue({ messages: [] });
    render(Chat, { props: { userId: "local" } });

    await screen.findByRole("option", { name: "Alice" });
    // No prior messages — a clean (empty) transcript, not an error.
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders duplicate capabilities without crashing the persona header", async () => {
    // The registry doesn't dedupe capabilities, so ["search","search"] is a
    // reachable DTO. A value-keyed chip list would throw each_key_duplicate and
    // crash the panel; both chips must render and the panel must mount.
    listAgents.mockResolvedValue([
      { id: "ada", name: "Ada", capabilities: ["search", "search"], status: "healthy" },
    ]);
    render(Chat, { props: { userId: "local" } });

    expect(await screen.findAllByText("search")).toHaveLength(2);
  });

  it("sends the message for the selected persona as the context-derived user", async () => {
    render(Chat, { props: { userId: "local" } });

    await screen.findByRole("option", { name: "Alice" });
    const picker = screen.getByRole("combobox", { name: /persona/i });
    await fireEvent.change(picker, { target: { value: "bob" } });
    const message = screen.getByRole("textbox", { name: /message/i });
    await fireEvent.input(message, { target: { value: "Hi Bob" } });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => {
      expect(sendChat).toHaveBeenCalledWith("bob", {
        message: "Hi Bob",
        userId: "local",
      });
    });
  });

  it("renders the agent's reply after a send", async () => {
    render(Chat, { props: { userId: "local" } });

    await screen.findByRole("option", { name: "Alice" });
    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "Hi" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => {
      expect(screen.getByText(/hello, human\./i)).toBeTruthy();
    });
  });

  it("shows a thinking state while the reply is in flight, then clears it", async () => {
    // Slice 1 chat is synchronous (no streaming, OQ5); the panel must show a
    // pending affordance until the reply lands so a slow agent doesn't read as a
    // frozen UI. Hold the promise open to observe the state, then resolve it.
    let resolveReply;
    sendChat.mockReturnValue(
      new Promise((resolve) => {
        resolveReply = resolve;
      }),
    );

    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });
    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "Hi" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => expect(screen.getByRole("status")).toBeTruthy());

    resolveReply(reply());
    await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
  });

  it("ignores a re-entrant submit while a reply is already in flight", async () => {
    // The Send button is disabled mid-flight, but pressing Enter inside a
    // single-line override input (Session/Epoch ID) still submits the form —
    // canSend only gates the button's disabled attribute, not send() itself.
    // send() must guard on the in-flight state so a second turn can't race the
    // first; otherwise it collides on the server's replyWaiter (409) and burns a
    // round-trip. Hold the first reply open, submit again, and assert the second
    // submit is a no-op.
    let resolveReply;
    sendChat.mockReturnValue(
      new Promise((resolve) => {
        resolveReply = resolve;
      }),
    );

    const { container } = render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });
    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "Hi" },
    });

    const form = container.querySelector("form.composer");
    await fireEvent.submit(form);
    await waitFor(() => expect(screen.getByRole("status")).toBeTruthy());
    await fireEvent.submit(form);

    expect(sendChat).toHaveBeenCalledTimes(1);

    resolveReply(reply());
    await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
  });

  it("locks the composer while a reply is in flight and keeps the pending text", async () => {
    // The turn is synchronous; the composer stays editable today, so a message
    // the operator keeps typing while "Waiting for a reply…" is silently wiped
    // by the post-send `message = ""`, and the persona could be switched out from
    // under the in-flight turn. Disable the composer inputs for the duration so
    // the pending text survives and the reply can't be misattributed, then
    // re-enable once the reply lands.
    let resolveReply;
    sendChat.mockReturnValue(
      new Promise((resolve) => {
        resolveReply = resolve;
      }),
    );

    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });
    const message = screen.getByRole("textbox", { name: /message/i });
    await fireEvent.input(message, { target: { value: "Hi" } });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => expect(message.disabled).toBe(true));
    expect(screen.getByRole("combobox", { name: /persona/i }).disabled).toBe(
      true,
    );
    // The in-flight prompt is still in the box, not cleared early.
    expect(message.value).toBe("Hi");

    resolveReply(reply());
    await waitFor(() => expect(message.disabled).toBe(false));
    // Cleared only after a successful turn.
    expect(message.value).toBe("");
  });

  it("shows a placeholder for an empty reply instead of a blank line", async () => {
    // The server can answer with reply_status:"empty" and an empty `reply`
    // (chat_handler.go) — a valid turn where the agent had nothing to say.
    // Rendering just the agent name with no text reads as a broken UI, so the
    // panel surfaces an explicit placeholder.
    sendChat.mockResolvedValue(reply({ reply: "", reply_status: "empty" }));

    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });
    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "Hi" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));

    expect(await screen.findByText(/no reply/i)).toBeTruthy();
  });

  it("surfaces the server error envelope without crashing the panel", async () => {
    sendChat.mockRejectedValue(
      new ApiError("message exceeds maximum length of 4000 characters", 400),
    );

    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });
    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "x" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/maximum length/i);
    // The panel stays usable: the message box and send control are still there.
    expect(screen.getByRole("textbox", { name: /message/i })).toBeTruthy();
  });

  it("passes the optional session and epoch overrides through to the request", async () => {
    // The chat API already accepts session_id / epoch_id (RFC 0031 / ISSUE-0085);
    // surfacing them quietly demonstrates the v0.3.5 isolation story.
    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });

    await fireEvent.input(screen.getByRole("textbox", { name: /session/i }), {
      target: { value: "sess-7" },
    });
    await fireEvent.input(screen.getByRole("textbox", { name: /epoch/i }), {
      target: { value: "ep-3" },
    });
    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "Hi" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => {
      expect(sendChat).toHaveBeenCalledWith("alice", {
        message: "Hi",
        userId: "local",
        sessionId: "sess-7",
        epochId: "ep-3",
      });
    });
  });

  it("acts as the context principal and offers no free-text user field", async () => {
    // RFC §F rule 1: identity is the /ui/context principal threaded in as a prop;
    // the panel must never expose a user_id input the operator could type into.
    const { container } = render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });

    expect(screen.getByText(/local/)).toBeTruthy();
    // Defend the rule itself, not one spelling of a violation: assert there is
    // no user-identity textbox at all (the panel's textboxes are Message /
    // Session ID / Epoch ID — none names a user), rather than probing for one
    // exact input[name="user_id"] a regression could trivially sidestep.
    expect(screen.queryByRole("textbox", { name: /user/i })).toBeNull();
    expect(container.querySelector('input[name="user_id"]')).toBeNull();
  });

  it("guards over-length messages client-side before any round-trip", async () => {
    // Mirror the server's 4000-char rejection so the user gets immediate
    // feedback; the server still enforces, but the panel shouldn't burn a
    // round-trip on input it knows is invalid.
    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });
    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "a".repeat(4001) },
    });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(sendChat).not.toHaveBeenCalled();
  });

  it("disables send until a message is entered", async () => {
    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });

    expect(screen.getByRole("button", { name: /send/i }).disabled).toBe(true);
    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "Hi" },
    });
    expect(screen.getByRole("button", { name: /send/i }).disabled).toBe(false);
  });

  it("surfaces a persona-load failure as an error state", async () => {
    listAgents.mockRejectedValue(new ApiError("backend down", 503));

    render(Chat, { props: { userId: "local" } });

    expect(await screen.findByRole("alert")).toBeTruthy();
  });

  it("retries persona loading after a load failure", async () => {
    // The persona load is the panel's only hard dependency; a transient backend
    // hiccup shouldn't strand the operator on a dead screen with no recourse but
    // a full reload. A Retry control re-runs listAgents in place.
    listAgents.mockRejectedValueOnce(new ApiError("backend down", 503));

    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("alert");

    await fireEvent.click(screen.getByRole("button", { name: /retry/i }));

    await screen.findByRole("option", { name: "Alice" });
    expect(listAgents).toHaveBeenCalledTimes(2);
  });

  it("shows an empty state — not a blank picker — when no personas are registered", async () => {
    // A successful-but-empty list is distinct from a load failure: there's
    // nothing to talk to, so the composer would be a dead end. Tell the operator
    // why rather than rendering an empty dropdown beside a disabled Send.
    listAgents.mockResolvedValue([]);

    render(Chat, { props: { userId: "local" } });

    expect(await screen.findByText(/no personas/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /send/i })).toBeNull();
  });

  it("annotates a non-healthy persona with its status in the picker", async () => {
    // GET /api/v1/agents returns agents of any status, but only a healthy one
    // can actually reply (chat_handler.go → 503 otherwise). Surface the status
    // up front so the operator isn't blind-sided after a wasted send.
    listAgents.mockResolvedValue([
      { id: "alice", name: "Alice", status: "healthy" },
      { id: "carol", name: "Carol", status: "offline" },
    ]);

    render(Chat, { props: { userId: "local" } });

    await screen.findByRole("option", { name: "Alice" });
    expect(screen.getByRole("option", { name: "Carol (offline)" })).toBeTruthy();
  });

  it("defaults the picker to the first healthy persona, skipping a non-healthy one", async () => {
    // Only a healthy persona can reply (chat_handler.go → 503 otherwise). When
    // the first-listed persona is offline, defaulting to it strands a newcomer on
    // a guaranteed-503 dead end. Skip to the first healthy one so the default
    // selection is always sendable; an all-unhealthy list still falls back to the
    // first entry (nothing better to offer).
    listAgents.mockResolvedValue([
      { id: "carol", name: "Carol", status: "offline" },
      { id: "alice", name: "Alice", status: "healthy" },
    ]);

    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });

    expect(screen.getByRole("combobox", { name: /persona/i }).value).toBe(
      "alice",
    );
  });

  it("falls back to the first persona when none are healthy", async () => {
    listAgents.mockResolvedValue([
      { id: "carol", name: "Carol", status: "offline" },
      { id: "dave", name: "Dave", status: "starting" },
    ]);

    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Carol (offline)" });

    expect(screen.getByRole("combobox", { name: /persona/i }).value).toBe(
      "carol",
    );
  });

  it("measures message length by code point, matching the server's rune limit", async () => {
    // The server caps at 4000 *runes* (utf8.RuneCountInString); a UTF-16 .length
    // guard over-counts astral characters and would falsely block a valid
    // message. 2001 astral chars = 4002 UTF-16 units but only 2001 code points,
    // comfortably under the limit, so the send must go through.
    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });

    const astral = "😀".repeat(2001);
    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: astral },
    });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => expect(sendChat).toHaveBeenCalled());
  });

  it("shows a loading affordance until the persona list settles", async () => {
    // During the in-flight initial load the panel must not fall through to the
    // composer beside an empty picker (a flash of a blank dropdown). Hold
    // listAgents open: assert a loading state and no composer, then resolve and
    // assert the picker is populated and the loading state is gone.
    let resolveAgents;
    listAgents.mockReturnValue(
      new Promise((resolve) => {
        resolveAgents = resolve;
      }),
    );

    render(Chat, { props: { userId: "local" } });

    expect(await screen.findByText(/loading personas/i)).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: /persona/i })).toBeNull();

    resolveAgents(AGENTS);
    await screen.findByRole("option", { name: "Alice" });
    expect(screen.queryByText(/loading personas/i)).toBeNull();
  });

  it("clears a prior send error when the persona is switched", async () => {
    // A send error refers to the attempt that just failed; switching persona is
    // a fresh intent, so the stale red alert must not linger over the new
    // selection. (The message itself survives — a failed send doesn't clear it.)
    sendChat.mockRejectedValueOnce(new ApiError("agent is not healthy", 503));

    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });
    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "Hi" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));
    await screen.findByRole("alert");

    await fireEvent.change(screen.getByRole("combobox", { name: /persona/i }), {
      target: { value: "bob" },
    });

    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("annotates a transcript turn with the scope overrides it was sent under", async () => {
    // The override inputs stay editable between turns, so turns sent under
    // different session/epoch scopes can interleave in one transcript. Record
    // the scope each turn actually used so the isolation story (RFC 0031 /
    // ISSUE-0085) is visible per-turn rather than silent. A turn with no
    // override carries no annotation (covered by the plain-reply tests above).
    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });

    await fireEvent.input(screen.getByRole("textbox", { name: /session/i }), {
      target: { value: "sess-7" },
    });
    await fireEvent.input(screen.getByRole("textbox", { name: /epoch/i }), {
      target: { value: "ep-3" },
    });
    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "Hi" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));

    expect(await screen.findByText(/session: sess-7/i)).toBeTruthy();
    expect(screen.getByText(/epoch: ep-3/i)).toBeTruthy();
  });
});
