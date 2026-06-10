import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/svelte";
import ChannelTimeline from "./ChannelTimeline.svelte";

// Live presence wiring (RFC 0048 console): a group lights the "thinking"
// indicator from two sources — an optimistic add for the console's own
// @-addressed publish (instant feedback) and the authoritative /activity poll
// (Tier 1), which also surfaces turns this console did NOT trigger and is the
// source of truth for clearing. The DM lifecycle — thinking-while-sending,
// cleared on reply, no /activity poll — is pinned in ChannelTimeline.dm.test.js.
// The backend client is mocked so the wiring runs without an orchestrator.
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
  getChannelActivity: vi.fn(),
  getChatHistory: vi.fn(),
  sendChat: vi.fn(),
  publishMessage: vi.fn(),
  getClosedInteractions: vi.fn(() => Promise.resolve({ interactions: [] })),
}));

import {
  listAgents,
  listChannels,
  getChannelHistory,
  getChannelActivity,
  getChatHistory,
  publishMessage,
} from "../lib/api.js";
import { selection } from "../lib/selection.svelte.js";
import { SLOW_AFTER_MS } from "../lib/presence.js";

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
  getChannelActivity.mockResolvedValue({ thinking: [] });
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

describe("Channel timeline — live presence", () => {
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

  it("drops the optimistic indicator when the operator switches conversations", async () => {
    // The optimistic signal belongs to the channel that triggered it. Switching
    // to another conversation must NOT carry a prior channel's "thinking" line
    // across — there is no turn in flight here, and neither the new channel's
    // /activity poll nor a reply would ever clear it, leaving it stranded
    // until the grace fade.
    listChannels.mockResolvedValue({
      channels: [
        channelWithMembers("local", "ember-owl"),
        {
          id: "random",
          name: "Random",
          channel_type: "group",
          members: [{ id: "local", respond: "when_mentioned" }],
        },
      ],
    });

    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });

    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "@ember-owl your read?" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /post/i }));
    expect(await screen.findByText(/ember owl is thinking/i)).toBeTruthy();

    await fireEvent.change(screen.getByRole("combobox", { name: /channel/i }), {
      target: { value: "random" },
    });

    await waitFor(() =>
      expect(screen.queryByText(/ember owl is thinking/i)).toBeNull(),
    );
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
    // clears (pruneFrom), and the brief "Waiting for you" hint flashes.
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

  it("softens past the slow threshold while the server keeps confirming the turn", async () => {
    // A turn the /activity poll keeps confirming softens to "taking a while…" so
    // a slow reply doesn't read as a stall; when the server drops it the bar
    // clears and hands back to the operator. (Server-confirmed, so it outlives
    // the optimistic grace — unlike a wrong guess the server never confirms.)
    vi.useFakeTimers();
    getChannelActivity.mockResolvedValue({ thinking: ["ember-owl"] });
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

    await vi.advanceTimersByTimeAsync(SLOW_AFTER_MS);
    expect(screen.getByText(/ember owl is taking a while/i)).toBeTruthy();

    // The server drops the turn — the next poll clears the bar.
    getChannelActivity.mockResolvedValue({ thinking: [] });
    await vi.advanceTimersByTimeAsync(3000);
    expect(screen.queryByText(/ember owl is/i)).toBeNull();
  });

  it("shows a turn the console did NOT trigger, from the /activity poll (Tier 1)", async () => {
    // The whole point of Tier 1: an agent dispatched by another participant (or
    // an autonomous reply) surfaces from the authoritative server set, with no
    // optimistic add from this console. It clears when the server drops it.
    vi.useFakeTimers();
    getChannelActivity.mockResolvedValue({ thinking: ["ember-owl"] });
    render(ChannelTimeline, { props: { userId: "local" } });
    await vi.waitFor(() =>
      expect(screen.getByRole("option", { name: "General" })).toBeTruthy(),
    );

    // No publish from this console — the indicator comes purely from the poll.
    await vi.advanceTimersByTimeAsync(3000);
    expect(screen.getByText(/ember owl is thinking/i)).toBeTruthy();

    getChannelActivity.mockResolvedValue({ thinking: [] });
    await vi.advanceTimersByTimeAsync(3000);
    expect(screen.queryByText(/ember owl is thinking/i)).toBeNull();
  });

  it("never shows the operator themselves from the /activity set", async () => {
    // The server marks every candidate responder (orderResponders), and a
    // human channel member is a candidate like any other — an agent
    // @-mentioning the console user puts the USER's id in /activity. The
    // optimistic path already filters `id !== userId`; the authoritative path
    // must too, or the bar tells the operator "local is thinking…" about
    // themselves (and inflates the "N agents" tally).
    vi.useFakeTimers();
    getChannelActivity.mockResolvedValue({ thinking: ["ember-owl", "local"] });
    render(ChannelTimeline, { props: { userId: "local" } });
    await vi.waitFor(() =>
      expect(screen.getByRole("option", { name: "General" })).toBeTruthy(),
    );

    await vi.advanceTimersByTimeAsync(3000);
    expect(screen.getByText(/ember owl is thinking/i)).toBeTruthy();
    expect(screen.queryByText(/local is/i)).toBeNull();
    expect(screen.queryByText(/2 agents/i)).toBeNull();
  });

  it("drops a stale /activity read that resolves after a newer one", async () => {
    // The on-open read (loadHistory) is fire-and-forget and races the first
    // poll tick's read under the same loadToken. A slow early response must
    // not overwrite the fresher set a later read already installed — here the
    // newest truth is "idle", and the late straggler claims a turn in flight.
    vi.useFakeTimers();
    let resolveStale;
    getChannelActivity
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveStale = resolve;
          }),
      )
      .mockResolvedValue({ thinking: [] });
    render(ChannelTimeline, { props: { userId: "local" } });
    await vi.waitFor(() =>
      expect(screen.getByRole("option", { name: "General" })).toBeTruthy(),
    );

    // First poll tick: the newer read resolves empty (idle).
    await vi.advanceTimersByTimeAsync(3000);
    expect(screen.queryByText(/is thinking/i)).toBeNull();

    // The hung on-open read finally resolves with its out-of-date set.
    resolveStale({ thinking: ["ember-owl"] });
    await vi.advanceTimersByTimeAsync(0);
    expect(screen.queryByText(/ember owl is thinking/i)).toBeNull();
  });

  it("issues the /activity read concurrently with the head fetch on a poll tick", async () => {
    // The two reads are independent; serializing them (activity only after the
    // head response lands) stretches every tick by a full round-trip and
    // drifts the 3s cadence. Only the INSTALL order matters — set() before
    // pruneFrom, pinned by the reply-clear case above — not the order the
    // requests are issued. Pin: the activity read is already in flight while
    // the head fetch is still pending.
    vi.useFakeTimers();
    render(ChannelTimeline, { props: { userId: "local" } });
    await vi.waitFor(() =>
      expect(screen.getByRole("option", { name: "General" })).toBeTruthy(),
    );

    let resolveHead;
    getChannelHistory.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveHead = resolve;
        }),
    );
    const activityReadsBefore = getChannelActivity.mock.calls.length;
    await vi.advanceTimersByTimeAsync(3000); // the tick fires; the head fetch hangs
    expect(getChannelActivity.mock.calls.length).toBe(activityReadsBefore + 1);

    resolveHead(historyOf());
    await vi.advanceTimersByTimeAsync(0);
  });
});
