import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  cleanup,
  fireEvent,
} from "@testing-library/svelte";
import Chat from "./Chat.svelte";
import { selection } from "../lib/selection.svelte.js";

// Sticky persona selection across a tab switch. The console mounts only the
// active panel (App.svelte), so switching Chat→Channels unmounts the chat panel
// and destroys its local `selectedAgent` state; switching back remounts it and
// re-runs the default-selection logic. Without a cross-mount memory, a deliberate
// persona choice is silently reset to the default on every round-trip — the bug
// these specs pin. selection.svelte.js survives the unmount (module-level
// $state); loadAgents honours a remembered persona.
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
  // Reset the sticky selection so specs don't leak it into one another (the
  // module-level $state outlives a single render, which is the whole point).
  selection.chatAgent = "";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Chat panel — sticky persona across a tab switch", () => {
  it("resumes the last deliberately-selected persona after a remount", async () => {
    // First visit: pick Bob (not the default Alice), then leave the panel
    // (cleanup() stands in for the App switching to Channels and unmounting Chat).
    const first = render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });
    await fireEvent.change(screen.getByRole("combobox", { name: /persona/i }), {
      target: { value: "bob" },
    });
    first.unmount();

    // Switch back: a fresh mount must reopen on Bob, not snap back to the default.
    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });
    expect(screen.getByRole("combobox", { name: /persona/i }).value).toBe("bob");
  });

  it("still applies the healthy-first default when nothing was chosen", async () => {
    // No deliberate choice means no remembered persona, so the default logic must
    // still run: don't let an empty sticky value strand the picker on a blank.
    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });
    expect(screen.getByRole("combobox", { name: /persona/i }).value).toBe("alice");
  });

  it("falls back to the default when the remembered persona is gone", async () => {
    // A remembered persona that has since deregistered isn't in the new list, so
    // honouring it would select a phantom. Degrade to the healthy-first default.
    selection.chatAgent = "ghost";
    render(Chat, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "Alice" });
    expect(screen.getByRole("combobox", { name: /persona/i }).value).toBe("alice");
  });
});
