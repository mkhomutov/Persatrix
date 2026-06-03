import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/svelte";
import ChannelTimeline from "./ChannelTimeline.svelte";

// RFC 0048 chat-panel-retirement amendment §C/§D — the consolidated Channels
// panel's first-contact surface. With one conversation panel there is no longer a
// cross-panel hand-off (the slice1-ux §F deep-link and nav.svelte.js are gone);
// "view this DM as a channel" is just an in-panel selection now. What remains to
// guard is the MERGED onboarding (§D): a stack is a dead end only when BOTH entry
// points are empty — with either personas OR channels present the panel offers a
// way in. Split out of ChannelTimeline.test.js to keep each spec under the
// review-size cap; the core panel/polling behaviour stays there, and the DM entry
// point lives in ChannelTimeline.dm.test.js.
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
}));

import {
  listAgents,
  listChannels,
  getChannelHistory,
  getChatHistory,
} from "../lib/api.js";
import { selection } from "../lib/selection.svelte.js";

const AGENTS = [{ id: "ada", name: "Ada", role: "Researcher", status: "healthy" }];
const CHANNELS = [{ id: "general", name: "General", channel_type: "group" }];

function historyOf(...messages) {
  return { messages };
}

beforeEach(() => {
  listAgents.mockResolvedValue([]);
  listChannels.mockResolvedValue({ channels: [] });
  getChannelHistory.mockResolvedValue(historyOf());
  getChatHistory.mockResolvedValue(historyOf());
  selection.dmAgent = "";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("Channels panel — merged onboarding (§D)", () => {
  it("makes the both-empty state an on-ramp, not a dead end", async () => {
    // No personas AND no channels: the one first-contact surface names both ways
    // in and offers a no-reload re-check + the quick-start link.
    render(ChannelTimeline, { props: { userId: "local" } });

    await screen.findByText(/no personas or channels/i);
    expect(screen.getByRole("button", { name: /refresh/i })).toBeTruthy();
    expect(screen.getByRole("link", { name: /quick-start/i })).toBeTruthy();
  });

  it("is not a dead end when personas exist but no channels do", async () => {
    // A persona is an entry point on its own (start a DM), so a channel-less stack
    // is NOT the onboarding dead end — the persona picker is offered instead.
    listAgents.mockResolvedValue(AGENTS);

    render(ChannelTimeline, { props: { userId: "local" } });

    expect(
      await screen.findByRole("combobox", { name: /persona/i }),
    ).toBeTruthy();
    expect(screen.queryByText(/no personas or channels/i)).toBeNull();
  });

  it("is not a dead end when channels exist but no personas do", async () => {
    // Symmetrically, a group channel to watch is an entry point even with no
    // personas registered — the channel picker is offered, not the onboarding.
    listChannels.mockResolvedValue({ channels: CHANNELS });

    render(ChannelTimeline, { props: { userId: "local" } });

    expect(
      await screen.findByRole("combobox", { name: /channel/i }),
    ).toBeTruthy();
    expect(screen.queryByText(/no personas or channels/i)).toBeNull();
  });
});
