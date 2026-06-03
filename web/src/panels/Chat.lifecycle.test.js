import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  cleanup,
  fireEvent,
  waitFor,
} from "@testing-library/svelte";
import Chat from "./Chat.svelte";
import { selection } from "../lib/selection.svelte.js";

// True "exit chat" and "start new" (mirrors the CLI exit/restart used for memory
// testing). Two affordances beside the persona switcher:
//   - Exit     → leave the conversation entirely, back to a persona lobby (no
//                conversation open). Survives a tab round-trip (selection sentinel).
//   - New chat → clear the on-screen conversation but keep the SAME persona and
//                identity/epoch, so the next turn is a fresh conversation while
//                the agent's persisted memory is intact — the persistence test
//                ("greet me" → New chat → "do you remember me?").
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
  sendChat.mockResolvedValue({ reply: "Remembered reply.", reply_status: "ok" });
  getChatHistory.mockResolvedValue({ messages: [] });
  listSessions.mockResolvedValue({ sessions: [] });
  createSession.mockResolvedValue({ id: "sess-new", label: "New" });
  // Start each spec from a clean, never-chosen sticky selection.
  selection.chatAgent = "";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  selection.chatAgent = "";
});

describe("Chat panel — exit to lobby", () => {
  it("leaves the conversation and shows the persona lobby on Exit", async () => {
    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });
    // In a conversation: the composer (Send) is present.
    expect(screen.getByRole("button", { name: /send/i })).toBeTruthy();

    await fireEvent.click(screen.getByRole("button", { name: /exit/i }));

    // The conversation is gone: no composer, no header — just the lobby prompt
    // and the persona picker so the operator can start a new conversation.
    expect(screen.queryByRole("button", { name: /send/i })).toBeNull();
    // The lobby prompt — distinct from the picker's "Select a persona…"
    // placeholder option (which also matches /select a persona/).
    expect(screen.getByText(/start a conversation/i)).toBeTruthy();
    expect(screen.getByRole("combobox", { name: /persona/i })).toBeTruthy();
    expect(screen.getByRole("combobox", { name: /persona/i }).value).toBe("");
  });

  it("stays in the lobby after a remount once exited (survives a tab switch)", async () => {
    const first = render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });
    await fireEvent.click(screen.getByRole("button", { name: /exit/i }));
    first.unmount();

    // Switching back must not silently re-enter a conversation: a deliberate exit
    // is remembered across the unmount, so the lobby persists.
    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });
    expect(screen.getByRole("combobox", { name: /persona/i }).value).toBe("");
    expect(screen.queryByRole("button", { name: /send/i })).toBeNull();
  });

  it("starts a conversation when a persona is chosen from the lobby", async () => {
    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });
    await fireEvent.click(screen.getByRole("button", { name: /exit/i }));
    expect(screen.queryByRole("button", { name: /send/i })).toBeNull();

    await fireEvent.change(screen.getByRole("combobox", { name: /persona/i }), {
      target: { value: "bob" },
    });

    // Picking a persona enters the conversation: the composer returns.
    expect(await screen.findByRole("button", { name: /send/i })).toBeTruthy();
    expect(screen.getByRole("combobox", { name: /persona/i }).value).toBe("bob");
  });
});

describe("Chat panel — start a new conversation", () => {
  it("clears the transcript but keeps the persona and composer on New chat", async () => {
    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });

    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "Hi" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));
    await screen.findByText(/remembered reply\./i);

    await fireEvent.click(screen.getByRole("button", { name: /new chat/i }));

    // The prior turn is cleared from view, but the persona and composer stay —
    // it's a fresh conversation with the same persona, not an exit.
    await waitFor(() =>
      expect(screen.queryByText(/remembered reply\./i)).toBeNull(),
    );
    expect(screen.getByRole("combobox", { name: /persona/i }).value).toBe(
      "alice",
    );
    expect(screen.getByRole("button", { name: /send/i })).toBeTruthy();
  });

  it("keeps the same identity and does not reseed server history on New chat", async () => {
    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });

    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "Hi" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));
    await screen.findByText(/remembered reply\./i);

    getChatHistory.mockClear();
    await fireEvent.click(screen.getByRole("button", { name: /new chat/i }));

    // A fresh conversation is a clean slate, not a reload of the persisted DM —
    // re-seeding would repaint the very turns we just cleared. Same persona, so
    // the selected-agent effect must not re-fire a history fetch.
    await waitFor(() =>
      expect(screen.queryByText(/remembered reply\./i)).toBeNull(),
    );
    expect(getChatHistory).not.toHaveBeenCalled();
  });
});
