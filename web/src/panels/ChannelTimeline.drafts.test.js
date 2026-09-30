import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/svelte";
import ChannelTimeline from "./ChannelTimeline.svelte";

// Moving between conversations must not move what you were writing: each
// channel and each DM keeps its own unsent draft, so a half-written post can
// never land in the wrong room. And a reload returns you to the channel you
// were watching rather than the first one in the list.
vi.mock("../lib/api.js", () => ({
  ApiError: class ApiError extends Error {},
  listAgents: vi.fn(),
  listChannels: vi.fn(),
  getChannelHistory: vi.fn(),
  getChatHistory: vi.fn(),
  sendChat: vi.fn(),
  publishMessage: vi.fn(),
  getClosedInteractions: vi.fn(() => Promise.resolve({ interactions: [] })),
  listSessions: vi.fn(() => Promise.resolve({ sessions: [] })),
  createSession: vi.fn(),
}));

import {
  listAgents,
  listChannels,
  getChannelHistory,
  getChatHistory,
  publishMessage,
} from "../lib/api.js";
import { selection } from "../lib/selection.svelte.js";

const AGENTS = [
  { id: "ada", name: "Ada", status: "healthy" },
  { id: "bob", name: "Bob", status: "healthy" },
];
const CHANNELS = [
  { id: "general", name: "General", channel_type: "group", members: [{ id: "local" }] },
  { id: "ops", name: "Ops", channel_type: "group", members: [{ id: "local" }] },
];

const composer = () => screen.getByRole("textbox", { name: /message/i });

async function type(text) {
  await fireEvent.input(composer(), { target: { value: text } });
}

beforeEach(() => {
  listAgents.mockResolvedValue(AGENTS);
  listChannels.mockResolvedValue({ channels: CHANNELS });
  getChannelHistory.mockResolvedValue({ messages: [] });
  getChatHistory.mockResolvedValue({ messages: [] });
  selection.dmAgent = "";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Channels panel — drafts and the remembered channel", () => {
  it("keeps an unsent draft per channel", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General", selected: true });

    await type("half a thought for general");
    await fireEvent.click(screen.getByRole("option", { name: "Ops" }));
    expect(composer().value).toBe("");

    await type("ops note");
    await fireEvent.click(screen.getByRole("option", { name: "General" }));
    expect(composer().value).toBe("half a thought for general");

    await fireEvent.click(screen.getByRole("option", { name: "Ops" }));
    expect(composer().value).toBe("ops note");
  });

  it("drops a draft once it was posted, even if you left before it landed", async () => {
    let resolvePublish;
    publishMessage.mockReturnValue(
      new Promise((resolve) => {
        resolvePublish = resolve;
      }),
    );
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General", selected: true });

    await type("posted already");
    await fireEvent.click(screen.getByRole("button", { name: /post/i }));
    await fireEvent.click(screen.getByRole("option", { name: "Ops" }));
    resolvePublish({
      id: "m1",
      channel_id: "general",
      sender_id: "local",
      content: "posted already",
      timestamp: "2026-06-02T10:00:00Z",
      mentions: [],
    });
    await screen.findByRole("button", { name: "Post" });

    await fireEvent.click(screen.getByRole("option", { name: "General" }));
    expect(composer().value).toBe("");
  });

  it("keeps an unsent DM draft per persona", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });

    await fireEvent.click(screen.getByRole("option", { name: "Ada" }));
    await screen.findByRole("heading", { name: "Ada" });
    await type("hi Ada");

    await fireEvent.click(screen.getByRole("option", { name: "Bob" }));
    await screen.findByRole("heading", { name: "Bob" });
    expect(composer().value).toBe("");

    await fireEvent.click(screen.getByRole("option", { name: "Ada" }));
    await screen.findByRole("heading", { name: "Ada" });
    expect(composer().value).toBe("hi Ada");
  });

  it("keeps drafts for any valid persona id, even one named like an object property", async () => {
    // `constructor` passes the agent-id pattern; a plain-object draft store
    // would hand back Object's own constructor as the "draft".
    listAgents.mockResolvedValue([{ id: "constructor", name: "Con", status: "healthy" }, ...AGENTS]);
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });

    await fireEvent.click(screen.getByRole("option", { name: "Con" }));
    await screen.findByRole("heading", { name: "Con" });
    expect(composer().value).toBe("");
    await type("for con");

    await fireEvent.click(screen.getByRole("option", { name: "Ada" }));
    await screen.findByRole("heading", { name: "Ada" });
    await fireEvent.click(screen.getByRole("option", { name: "Con" }));
    await screen.findByRole("heading", { name: "Con" });
    expect(composer().value).toBe("for con");
  });

  it("reopens the channel you watched last", async () => {
    const first = render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General", selected: true });
    await fireEvent.click(screen.getByRole("option", { name: "Ops" }));
    first.unmount();

    render(ChannelTimeline, { props: { userId: "local" } });
    expect(await screen.findByRole("option", { name: "Ops", selected: true })).toBeTruthy();
    await waitFor(() =>
      expect(getChannelHistory).toHaveBeenLastCalledWith("ops", expect.anything()),
    );
  });

  it("falls back to the first channel when the remembered one is gone", async () => {
    localStorage.setItem("persatrix.console.lastChannel", JSON.stringify("deleted"));
    render(ChannelTimeline, { props: { userId: "local" } });
    expect(
      await screen.findByRole("option", { name: "General", selected: true }),
    ).toBeTruthy();
  });
});
