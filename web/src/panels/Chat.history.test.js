import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/svelte";
import Chat from "./Chat.svelte";

// RFC 0048 amendment §B — the chat panel seeds its transcript from the persisted
// DM history on persona-select, so a console reload resumes the conversation
// rather than presenting as stateless. The backend client is mocked; these cover
// the history-seed wiring specifically (Chat.test.js covers picker/send/errors).
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

import { listAgents, sendChat, getChatHistory } from "../lib/api.js";

const AGENTS = [
  { id: "alice", name: "Alice", status: "healthy" },
  { id: "bob", name: "Bob", status: "healthy" },
];

beforeEach(() => {
  listAgents.mockResolvedValue(AGENTS);
  sendChat.mockResolvedValue({ reply: "ok", reply_status: "ok" });
  // Default to no prior conversation (200-empty fresh start, §B); the resume
  // test overrides this with a seeded history.
  getChatHistory.mockResolvedValue({ messages: [] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Chat panel — history resume (§B)", () => {
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

  it("reseeds when the identity (userId) changes, not just the persona", async () => {
    // The DM is keyed on (userId, agent), so the transcript belongs to the
    // identity as much as the persona. If the shell rethreads a different
    // context-derived userId, the seed must re-resolve for the new principal —
    // otherwise the panel would keep showing the previous identity's history.
    const { rerender } = render(Chat, { props: { userId: "alice" } });

    await waitFor(() => {
      expect(getChatHistory).toHaveBeenCalledWith("alice", { userId: "alice" });
    });
    getChatHistory.mockClear();

    await rerender({ userId: "bob" });

    await waitFor(() => {
      expect(getChatHistory).toHaveBeenCalledWith("alice", { userId: "bob" });
    });
  });
});
