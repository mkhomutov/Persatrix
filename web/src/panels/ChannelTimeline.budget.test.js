import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/svelte";
import ChannelTimeline from "./ChannelTimeline.svelte";

// What one console window costs while it shows a group channel (ISSUE-0224).
// Every data request counts toward one budget: 60 calls per 60 seconds by
// default, shared by every console window while sign-in is off. Watching the
// shipped `planning` channel, with its three personas, must stay inside it
// with room for a person's own clicks. This case counts every request the
// panel makes in one minute of watching, by endpoint, so a change that adds a
// read to a tick anywhere in the panel fails here.
vi.mock("../lib/api.js", () => ({
  ApiError: class ApiError extends Error {},
  listAgents: vi.fn(),
  listChannels: vi.fn(),
  getChannelHistory: vi.fn(),
  getChannelActivity: vi.fn(),
  getChatHistory: vi.fn(),
  sendChat: vi.fn(),
  publishMessage: vi.fn(),
  getClosedInteractions: vi.fn(),
  getChannelConfig: vi.fn(),
  listSessions: vi.fn(),
  createSession: vi.fn(),
}));

import * as api from "../lib/api.js";
import { selection } from "../lib/selection.svelte.js";

const PERSONAS = ["ember-owl", "iron-fox", "nova-sparrow"];
const BUDGET = 60;

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  api.listAgents.mockResolvedValue(
    PERSONAS.map((id) => ({ id, name: id, role: "Persona", status: "healthy" })),
  );
  api.listChannels.mockResolvedValue({
    channels: [
      {
        id: "group:planning",
        name: "planning",
        channel_type: "group",
        members: PERSONAS.map((id) => ({ id, respond: "auto" })),
      },
    ],
  });
  api.getChannelHistory.mockResolvedValue({
    messages: [
      {
        id: "m1",
        channel_id: "group:planning",
        sender_id: "ember-owl",
        content: "a first message",
        timestamp: "2026-06-02T10:00:00Z",
        mentions: [],
      },
    ],
  });
  api.getChannelActivity.mockResolvedValue({ thinking: [] });
  api.getClosedInteractions.mockResolvedValue({ interactions: [] });
  selection.dmAgent = "";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("Channels panel — requests in one minute of watching a group channel", () => {
  it("makes 46 requests, 14 inside the default budget of 60", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await vi.advanceTimersByTimeAsync(0);
    expect(screen.getByText("a first message")).toBeTruthy();
    // Opening the channel: the two lists, its first page, its activity, and
    // one summary read per persona.
    const count = () =>
      Object.fromEntries(
        Object.entries(api)
          .filter(([, fn]) => vi.isMockFunction(fn) && fn.mock.calls.length > 0)
          .map(([name, fn]) => [name, fn.mock.calls.length]),
      );
    expect(count()).toEqual({
      listAgents: 1,
      listChannels: 1,
      getChannelHistory: 1,
      getChannelActivity: 1,
      getClosedInteractions: 3,
    });
    vi.clearAllMocks();

    await vi.advanceTimersByTimeAsync(60000);

    // The feed: a history read and an activity read every three seconds.
    // The summary: each of the three personas twice.
    expect(count()).toEqual({
      getChannelHistory: 20,
      getChannelActivity: 20,
      getClosedInteractions: 6,
    });
    const total = Object.values(count()).reduce((a, b) => a + b, 0);
    expect(total).toBe(46);
    expect(BUDGET - total).toBe(14);
  });
});
