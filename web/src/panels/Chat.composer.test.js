import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/svelte";
import Chat from "./Chat.svelte";

// Composer + session-scope ergonomics for the chat panel (RFC 0048 amendment §C
// session selector / §D composer idioms): Enter-to-send, abortable turns, and
// the optional session/epoch isolation overrides (the latter rendered by the
// extracted ScopeSelector child). Core panel behaviour lives in Chat.test.js;
// history-seed resume in Chat.history.test.js. Client mocked.
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
  listSessions: vi.fn(),
  createSession: vi.fn(),
}));

import {
  listAgents,
  sendChat,
  getChatHistory,
  listSessions,
  createSession,
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
  // Default to an available-but-empty session registry (§C). Tests that need
  // the free-text degradation reject this instead.
  listSessions.mockResolvedValue({ sessions: [] });
  createSession.mockResolvedValue({ id: "sess-new", label: "New" });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Chat composer & session scope", () => {
  it("passes the optional session and epoch overrides through to the request", async () => {
    // The chat API already accepts session_id / epoch_id (RFC 0031 / ISSUE-0085);
    // surfacing them quietly demonstrates the v0.3.5 isolation story. Degrade the
    // session control to free-text (registry unwired) so this pass-through test
    // is independent of the dropdown wiring (covered separately below).
    listSessions.mockRejectedValue(new ApiError("session registry off", 503));
    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });

    await fireEvent.input(screen.getByRole("textbox", { name: /session id/i }), {
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
      expect(sendChat).toHaveBeenCalledWith(
        "alice",
        expect.objectContaining({
          message: "Hi",
          userId: "local",
          sessionId: "sess-7",
          epochId: "ep-3",
        }),
      );
    });
  });

  it("sends on Enter and inserts a newline on Shift+Enter", async () => {
    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });
    const message = screen.getByRole("textbox", { name: /message/i });

    // Shift+Enter must NOT send (it inserts a newline).
    await fireEvent.input(message, { target: { value: "draft" } });
    await fireEvent.keyDown(message, { key: "Enter", shiftKey: true });
    expect(sendChat).not.toHaveBeenCalled();

    // Plain Enter sends.
    await fireEvent.keyDown(message, { key: "Enter" });
    await waitFor(() => {
      expect(sendChat).toHaveBeenCalledWith(
        "alice",
        expect.objectContaining({ message: "draft" }),
      );
    });
  });

  it("selects a labeled session from the dropdown and passes its id", async () => {
    // §C: the session scope is a dropdown over /api/v1/sessions, so a tester
    // drives isolation from the browser without hunting a session id in the CLI.
    listSessions.mockResolvedValue({
      sessions: [
        { id: "s-123", label: "Acme demo", archived: false },
        { id: "s-arch", label: "Old", archived: true },
      ],
    });
    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });

    // The archived session is filtered out; the labeled one is offered.
    const sessionSelect = await screen.findByRole("combobox", {
      name: /^session$/i,
    });
    expect(screen.queryByRole("option", { name: "Old" })).toBeNull();
    await fireEvent.change(sessionSelect, { target: { value: "s-123" } });

    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "Hi" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => {
      expect(sendChat).toHaveBeenCalledWith(
        "alice",
        expect.objectContaining({ sessionId: "s-123" }),
      );
    });
  });

  it("creates a new labeled session and selects it", async () => {
    listSessions.mockResolvedValue({ sessions: [] });
    createSession.mockResolvedValue({
      id: "s-new",
      label: "Fresh",
      archived: false,
    });
    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });

    await fireEvent.input(
      await screen.findByRole("textbox", { name: /new session/i }),
      { target: { value: "Fresh" } },
    );
    await fireEvent.click(screen.getByRole("button", { name: /create/i }));

    await waitFor(() => expect(createSession).toHaveBeenCalledWith("Fresh"));
    // The created session becomes the selected scope and rides the next send.
    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "Hi" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));
    await waitFor(() => {
      expect(sendChat).toHaveBeenCalledWith(
        "alice",
        expect.objectContaining({ sessionId: "s-new" }),
      );
    });
  });

  it("cancels an in-flight turn without surfacing an error", async () => {
    // §D: a synchronous turn can block up to 30s; Cancel aborts the fetch. The
    // client surfaces an abort as a quiet cancellation, not an error banner.
    let rejectSend;
    sendChat.mockImplementation(
      (_agentID, { signal } = {}) =>
        new Promise((_resolve, reject) => {
          rejectSend = () => {
            const err = new ApiError("network error", 0, {
              cause: Object.assign(new Error("aborted"), {
                name: "AbortError",
              }),
            });
            reject(err);
          };
          if (signal) {
            signal.addEventListener("abort", rejectSend);
          }
        }),
    );
    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });
    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "Hi" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));

    // A Cancel control appears while the turn is in flight; clicking it aborts.
    const cancel = await screen.findByRole("button", { name: /cancel/i });
    await fireEvent.click(cancel);

    await waitFor(() => {
      // Back to a sendable composer, with no error alert from the cancel.
      expect(screen.getByRole("button", { name: /^send$/i })).toBeTruthy();
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("annotates a transcript turn with the scope overrides it was sent under", async () => {
    // The override inputs stay editable between turns, so turns sent under
    // different session/epoch scopes can interleave in one transcript. Record
    // the scope each turn actually used so the isolation story (RFC 0031 /
    // ISSUE-0085) is visible per-turn rather than silent. A turn with no
    // override carries no annotation (covered by the plain-reply tests above).
    // Degrade the session control to free-text (registry unwired) so this test
    // stays focused on the per-turn annotation rather than the dropdown wiring.
    listSessions.mockRejectedValue(new ApiError("session registry off", 503));
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
