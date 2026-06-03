import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/svelte";
import Chat from "./Chat.svelte";

// Task agents (agents.yaml `type: "task"`) execute workflow steps and never hold
// a conversation, so a chat turn dead-ends in a timeout. The picker shows them but
// disabled, with an explanation (extends the §A agent DTO): visible roster, no
// dead-end selection. Anything other than "task" — including an unset type — stays
// chattable so the guard can't regress an existing conversation.
vi.mock("../lib/api.js", () => ({
  ApiError: class ApiError extends Error {},
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

beforeEach(() => {
  sendChat.mockResolvedValue({ reply: "hi", reply_status: "ok" });
  getChatHistory.mockResolvedValue({ messages: [] });
  listSessions.mockResolvedValue({ sessions: [] });
  createSession.mockResolvedValue({ id: "s", label: "s" });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Chat panel — task agents", () => {
  it("renders a task agent's option disabled and labeled not-chattable", async () => {
    listAgents.mockResolvedValue([
      { id: "ada", name: "Ada", type: "persona", status: "healthy" },
      { id: "planner", name: "Planner", type: "task", status: "healthy" },
    ]);
    render(Chat, { props: { userId: "local" } });

    const taskOption = await screen.findByRole("option", {
      name: /Planner \(task agent — not chattable\)/,
    });
    expect(taskOption.disabled).toBe(true);
    // The persona stays enabled.
    expect(screen.getByRole("option", { name: "Ada" }).disabled).toBe(false);
  });

  it("defaults the picker to a persona, never a task agent", async () => {
    // Task agent listed first: a naive "first healthy" default would land on it.
    listAgents.mockResolvedValue([
      { id: "planner", name: "Planner", type: "task", status: "healthy" },
      { id: "ada", name: "Ada", type: "persona", status: "healthy" },
    ]);
    render(Chat, { props: { userId: "local" } });

    const picker = await screen.findByRole("combobox", { name: /persona/i });
    await waitFor(() => expect(picker.value).toBe("ada"));
  });

  it("treats an agent with no type as chattable (backward compatible)", async () => {
    listAgents.mockResolvedValue([{ id: "ada", name: "Ada", status: "healthy" }]);
    render(Chat, { props: { userId: "local" } });

    const option = await screen.findByRole("option", { name: "Ada" });
    expect(option.disabled).toBe(false);
    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "Hi" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));
    await waitFor(() => expect(sendChat).toHaveBeenCalled());
  });

  it("treats an unknown non-task type as chattable (only `task` is gated)", async () => {
    // The contract is "anything other than `task` is chattable", not an allow-list
    // of known kinds — a future agent kind the console hasn't heard of must degrade
    // to chattable, not silently lock. The backward-compat test above covers the
    // *unset* path; this pins the *typed-but-unknown* path.
    listAgents.mockResolvedValue([
      { id: "ada", name: "Ada", type: "swarm", status: "healthy" },
    ]);
    render(Chat, { props: { userId: "local" } });

    const option = await screen.findByRole("option", { name: "Ada" });
    expect(option.disabled).toBe(false);
    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "Hi" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));
    await waitFor(() => expect(sendChat).toHaveBeenCalled());
  });

  it("locks the composer and explains when only task agents exist", async () => {
    listAgents.mockResolvedValue([
      { id: "planner", name: "Planner", type: "task", status: "healthy" },
    ]);
    render(Chat, { props: { userId: "local" } });

    // The lone task agent is the forced selection, so the composer must explain
    // why it's locked rather than leaving a dead Send button.
    await screen.findByText(/don't hold conversations/i);
    expect(screen.getByRole("button", { name: /send/i }).disabled).toBe(true);
  });

  it("ignores Enter-to-send when a task agent is the only selection", async () => {
    listAgents.mockResolvedValue([
      { id: "planner", name: "Planner", type: "task", status: "healthy" },
    ]);
    render(Chat, { props: { userId: "local" } });

    await screen.findByText(/don't hold conversations/i);
    const message = screen.getByRole("textbox", { name: /message/i });
    await fireEvent.input(message, { target: { value: "Hi" } });
    // Enter bypasses the disabled button; send() must still no-op for a task agent.
    await fireEvent.keyDown(message, { key: "Enter" });
    expect(sendChat).not.toHaveBeenCalled();
  });
});
