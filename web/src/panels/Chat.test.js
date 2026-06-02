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
}));

import { listAgents, sendChat, ApiError } from "../lib/api.js";

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
});
