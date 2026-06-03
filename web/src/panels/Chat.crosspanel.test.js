import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/svelte";
import Chat from "./Chat.svelte";
import { nav } from "../lib/nav.svelte.js";

// RFC 0048 amendment §F — the chat panel's first-contact and cross-panel
// surfaces: the no-personas empty state is an on-ramp (guidance + Refresh +
// docs link) rather than a dead end, and once a conversation exists the panel
// offers a "view in timeline" hand-off that records the DM channel via the
// shared nav intent and switches the hash route. Split out of Chat.test.js to
// keep each spec under the review-size cap; the core panel behaviour stays
// there and the history-seed/composer specs in their own files.
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
} from "../lib/api.js";

const AGENTS = [
  { id: "alice", name: "Alice", status: "healthy" },
  { id: "bob", name: "Bob", status: "healthy" },
];

beforeEach(() => {
  listAgents.mockResolvedValue(AGENTS);
  sendChat.mockResolvedValue({ reply: "Hello, human.", reply_status: "ok" });
  // Default to no prior conversation (200-empty fresh start, §B). The hand-off
  // test overrides this with a seeded history that carries a DM channel id.
  getChatHistory.mockResolvedValue({ messages: [] });
  listSessions.mockResolvedValue({ sessions: [] });
  createSession.mockResolvedValue({ id: "sess-new", label: "New" });
  // Reset the shared cross-panel nav intent (§F) so tests don't leak it.
  nav.targetChannel = "";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Chat panel — §F onboarding + cross-panel continuity", () => {
  it("makes the no-personas empty state an on-ramp, not a dead end (§F)", async () => {
    listAgents.mockResolvedValue([]);
    render(Chat, { props: { userId: "local" } });

    await screen.findByText(/no personas/i);
    // Guidance + a re-check that doesn't need a full reload + a docs link.
    expect(screen.getByText(/agent register/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /refresh/i })).toBeTruthy();
    expect(screen.getByRole("link", { name: /quick-start/i })).toBeTruthy();
  });

  it("offers 'view in timeline' once a conversation exists and hands it off (§F)", async () => {
    // A seeded history carries the DM channel id; the cross-panel link records it
    // as the pending selection and switches the hash route to the timeline.
    getChatHistory.mockResolvedValue({
      messages: [
        {
          id: "h1",
          channel_id: "dm:alice:local",
          sender_id: "local",
          content: "hi",
          timestamp: "2026-06-02T10:00:00Z",
        },
      ],
    });
    window.location.hash = "#/chat";
    render(Chat, { props: { userId: "local" } });

    const link = await screen.findByRole("button", { name: /view in timeline/i });
    await fireEvent.click(link);

    expect(nav.targetChannel).toBe("dm:alice:local");
    expect(window.location.hash).toBe("#/channels");
  });

  it("shows no 'view in timeline' link before a conversation exists (§F)", async () => {
    getChatHistory.mockResolvedValue({ messages: [] });
    render(Chat, { props: { userId: "local" } });

    await screen.findByRole("option", { name: "Alice" });
    expect(
      screen.queryByRole("button", { name: /view in timeline/i }),
    ).toBeNull();
  });
});
