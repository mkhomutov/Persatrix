import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/svelte";
import ChannelTimeline from "./ChannelTimeline.svelte";

// Reading a long conversation: the timeline marks each calendar day with a
// divider, and the first page (the newest 50) can be extended backwards with
// the history endpoint's `before` cursor, so a busy channel's past stays
// reachable without loading all of it up front.
vi.mock("../lib/api.js", () => ({
  ApiError: class ApiError extends Error {},
  listAgents: vi.fn(),
  listChannels: vi.fn(),
  getChannelHistory: vi.fn(),
  getChatHistory: vi.fn(),
  sendChat: vi.fn(),
  publishMessage: vi.fn(),
  getClosedInteractions: vi.fn(() => Promise.resolve({ interactions: [] })),
}));

import { listAgents, listChannels, getChannelHistory } from "../lib/api.js";
import { selection } from "../lib/selection.svelte.js";

const CHANNELS = [{ id: "general", name: "General", channel_type: "group" }];

function msg(id, content, ts) {
  return {
    id,
    channel_id: "general",
    sender_id: "alice",
    content,
    timestamp: ts,
    mentions: [],
  };
}

// A newest-first page of `count` messages one minute apart, ending at `end`.
function page(count, end, prefix) {
  const out = [];
  for (let i = 0; i < count; i++) {
    const ts = new Date(Date.parse(end) - i * 60000).toISOString();
    out.push(msg(`${prefix}${i}`, `${prefix} message ${i}`, ts));
  }
  return out;
}

beforeEach(() => {
  listAgents.mockResolvedValue([]);
  listChannels.mockResolvedValue({ channels: CHANNELS });
  selection.dmAgent = "";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Channels panel — reading history", () => {
  it("draws one day divider per calendar day, outside the message list items", async () => {
    // 24 hours apart at the same UTC time: two different local days anywhere.
    getChannelHistory.mockResolvedValue({
      messages: [
        msg("b", "second day", "2026-06-02T10:00:00Z"),
        msg("a", "first day", "2026-06-01T10:00:00Z"),
      ],
    });

    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByText(/second day/);

    expect(screen.getAllByRole("separator")).toHaveLength(2);
    // Dividers are not list items, so the timeline's items are the messages.
    const items = screen.getAllByRole("listitem");
    expect(items[0].textContent).toMatch(/first day/);
  });

  it("offers older messages when the first page is full, and loads them before the oldest", async () => {
    const newest = page(50, "2026-06-02T12:00:00Z", "new");
    const oldestShown = newest.at(-1).timestamp;
    getChannelHistory.mockImplementation((id, { before } = {}) =>
      Promise.resolve({
        messages: before ? [msg("old1", "an older message", "2026-06-02T10:00:00Z")] : newest,
      }),
    );

    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByText("new message 0");

    await fireEvent.click(screen.getByRole("button", { name: /show older messages/i }));

    await waitFor(() =>
      expect(getChannelHistory).toHaveBeenCalledWith("general", {
        limit: 50,
        before: oldestShown,
      }),
    );
    expect(await screen.findByText("an older message")).toBeTruthy();
    // The older page was short, so there is nothing further back.
    expect(screen.queryByRole("button", { name: /show older messages/i })).toBeNull();
    // Oldest first: the loaded message renders above the first page.
    expect(screen.getAllByRole("listitem")[0].textContent).toMatch(/an older message/);
  });

  it("does not offer older messages when the first page is short", async () => {
    getChannelHistory.mockResolvedValue({
      messages: page(3, "2026-06-02T12:00:00Z", "new"),
    });

    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByText("new message 0");

    expect(screen.queryByRole("button", { name: /show older messages/i })).toBeNull();
  });
});
