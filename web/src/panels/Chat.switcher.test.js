import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/svelte";
import Chat from "./Chat.svelte";
import { selection } from "../lib/selection.svelte.js";

// The persona switcher must stay reachable while a conversation is open. It used
// to live at the BOTTOM of the panel, inside <form class="composer">, below an
// unbounded transcript — so once a chat grew, the only way to switch persona (or
// "exit" the current one) was pushed off-screen and the operator had to scroll
// past the whole transcript to reach it. The channel-timeline panel already puts
// its selector at the top with a bounded message region; Chat must match. These
// specs pin the switcher OUT of the composer so it sits above the transcript and
// is always reachable. (RFC 0048.)
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

import { listAgents, sendChat, getChatHistory, listSessions, createSession } from "../lib/api.js";

const AGENTS = [
  { id: "alice", name: "Alice", status: "healthy" },
  { id: "bob", name: "Bob", status: "healthy" },
];

beforeEach(() => {
  listAgents.mockResolvedValue(AGENTS);
  sendChat.mockResolvedValue({ reply: "Hi.", reply_status: "ok" });
  getChatHistory.mockResolvedValue({ messages: [] });
  listSessions.mockResolvedValue({ sessions: [] });
  createSession.mockResolvedValue({ id: "sess-new", label: "New" });
  selection.chatAgent = "";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Chat panel — persona switcher placement", () => {
  it("renders the persona switcher outside the composer form", async () => {
    // Buried inside the composer (below the transcript) the switcher scrolls off
    // once a chat grows. Lifting it out of the form keeps it at the top of the
    // panel, reachable regardless of transcript length.
    const { container } = render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });

    const picker = screen.getByRole("combobox", { name: /persona/i });
    const composer = container.querySelector("form.composer");
    expect(composer).not.toBeNull();
    expect(composer.contains(picker)).toBe(false);
  });

  it("places the persona switcher above the transcript in DOM order", async () => {
    // The switcher must precede the conversation so a long transcript can't push
    // it below the fold (it sits at the top like the channel-timeline selector).
    const { container } = render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });

    const picker = screen.getByRole("combobox", { name: /persona/i });
    const transcript = container.querySelector(".transcript");
    expect(transcript).not.toBeNull();
    // compareDocumentPosition: FOLLOWING (4) means transcript comes after picker.
    expect(
      picker.compareDocumentPosition(transcript) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});
