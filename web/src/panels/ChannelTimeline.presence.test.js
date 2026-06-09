import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/svelte";
import ChannelTimeline from "./ChannelTimeline.svelte";

// Live presence wiring (RFC 0048 console, Tier 0): a group publish that
// @-addresses a persona lights the "thinking" indicator for that persona until
// its reply lands; a broadcast that names nobody shows nothing (the optimistic
// signal only knows whom THIS console addressed). The DM lifecycle —
// thinking-while-sending, cleared on reply — is pinned in ChannelTimeline.dm.test.js;
// this spec covers the group path and the no-guess case. The backend client is
// mocked so the wiring runs without an orchestrator.
vi.mock("../lib/api.js", () => ({
  ApiError: class ApiError extends Error {
    constructor(message, status, options) {
      super(message, options);
      this.name = "ApiError";
      this.status = status;
      this.code = options?.code;
    }
  },
  listAgents: vi.fn(),
  listChannels: vi.fn(),
  getChannelHistory: vi.fn(),
  getChatHistory: vi.fn(),
  sendChat: vi.fn(),
  publishMessage: vi.fn(),
  getClosedInteractions: vi.fn(() => Promise.resolve({ interactions: [] })),
}));

import {
  listAgents,
  listChannels,
  getChannelHistory,
  getChatHistory,
  publishMessage,
} from "../lib/api.js";
import { selection } from "../lib/selection.svelte.js";

const AGENTS = [{ id: "ember-owl", name: "Ember Owl", role: "Strategist", status: "healthy" }];

function channelWithMembers(...ids) {
  return {
    id: "general",
    name: "General",
    channel_type: "group",
    members: ids.map((id) => ({
      id,
      respond: id === "local" ? "when_mentioned" : "always",
    })),
  };
}

function historyOf(...messages) {
  return { messages };
}

beforeEach(() => {
  listAgents.mockResolvedValue(AGENTS);
  listChannels.mockResolvedValue({
    channels: [channelWithMembers("local", "ember-owl")],
  });
  getChannelHistory.mockResolvedValue(historyOf());
  getChatHistory.mockResolvedValue(historyOf());
  publishMessage.mockResolvedValue({
    id: "m3",
    channel_id: "general",
    sender_id: "local",
    content: "posted",
    timestamp: "2026-06-04T10:00:03Z",
    mentions: [],
  });
  selection.dmAgent = "";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("Channel timeline — live presence (Tier 0)", () => {
  it("lights the thinking indicator for an @-addressed persona on publish", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });

    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "@ember-owl your read?" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /post/i }));

    expect(await screen.findByText(/ember owl is thinking/i)).toBeTruthy();
  });

  it("shows no indicator for a broadcast that addresses nobody", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });

    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "just a status update" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /post/i }));

    await waitFor(() => expect(publishMessage).toHaveBeenCalledTimes(1));
    expect(screen.queryByText(/is thinking/i)).toBeNull();
  });

  it("clears the indicator when the addressed persona's reply arrives on a poll", async () => {
    vi.useFakeTimers();
    render(ChannelTimeline, { props: { userId: "local" } });
    await vi.waitFor(() =>
      expect(screen.getByRole("option", { name: "General" })).toBeTruthy(),
    );

    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "@ember-owl your read?" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /post/i }));
    await vi.waitFor(() =>
      expect(screen.getByText(/ember owl is thinking/i)).toBeTruthy(),
    );

    // The next poll tick surfaces Ember Owl's reply — the optimistic indicator
    // clears (pruneThinking), and the brief "Waiting for you" hint flashes.
    getChannelHistory.mockResolvedValue(
      historyOf({
        id: "r1",
        channel_id: "general",
        sender_id: "ember-owl",
        content: "my read is…",
        timestamp: "2026-06-04T10:00:05Z",
        mentions: [],
      }),
    );
    await vi.advanceTimersByTimeAsync(3000);

    expect(screen.queryByText(/ember owl is thinking/i)).toBeNull();
    expect(screen.getByText(/waiting for you/i)).toBeTruthy();
  });
});
