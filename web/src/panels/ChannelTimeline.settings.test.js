import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/svelte";
import ChannelTimeline from "./ChannelTimeline.svelte";

// RFC 0050 Phase 2 PR 2 mount gating: the nested ChannelSettings panel renders
// only when the config-edit capability is on AND a group channel is watched
// (not a DM) — mirroring how ChannelMembers gates on canCreate. The two
// capabilities are independent: settings is governed by canConfigEdit alone, so
// it must show even when canCreate is off. The backend client is mocked.
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
  createChannel: vi.fn(),
  getChannelConfig: vi.fn(),
  patchChannelConfig: vi.fn(),
}));

import {
  listAgents,
  listChannels,
  getChannelHistory,
  getChatHistory,
  getChannelConfig,
} from "../lib/api.js";
import { selection } from "../lib/selection.svelte.js";

const CHANNELS = [{ id: "general", name: "General", channel_type: "group" }];

function config() {
  return {
    revision: 0,
    floor_control: { value: false, source: "default" },
    salience_max_channel_members: { value: 8, source: "default" },
    max_replies_per_participant_per_interaction: { value: 4, source: "default" },
    end_vote_threshold: { value: 2, source: "default" },
    end_vote_window: { value: 600, source: "default" },
    escalation_chair_id: { value: null, source: "default" },
    interaction_idle_timeout_seconds: { value: 900, source: "default" },
    interaction_budget_tokens: { value: null, source: "default" },
  };
}

beforeEach(() => {
  listAgents.mockResolvedValue([{ id: "ada", name: "Ada", status: "healthy" }]);
  listChannels.mockResolvedValue({ channels: CHANNELS });
  getChannelHistory.mockResolvedValue({ messages: [] });
  getChatHistory.mockResolvedValue({ messages: [] });
  getChannelConfig.mockResolvedValue(config());
  selection.dmAgent = "";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ChannelSettings mount gating", () => {
  it("hides the settings panel when the config-edit capability is off", async () => {
    render(ChannelTimeline, { props: { userId: "local", canConfigEdit: false } });
    await screen.findByRole("option", { name: "General" });

    // Assert on the text the panel would actually render (its summary), not
    // role="group" — a <details> carries no such role, so that query would pass
    // whether the panel were mounted or not. The fetch guard below corroborates.
    expect(screen.queryByText(/channel settings/i)).toBeNull();
    expect(getChannelConfig).not.toHaveBeenCalled();
  });

  it("shows the settings panel for a watched group channel when the capability is on, independent of canCreate", async () => {
    render(ChannelTimeline, {
      props: { userId: "local", canCreate: false, canConfigEdit: true },
    });
    await screen.findByRole("option", { name: "General" });

    // The general group channel auto-selects, so the nested settings panel loads.
    await waitFor(() =>
      expect(screen.getByText(/channel settings/i)).toBeTruthy(),
    );
    await waitFor(() => expect(getChannelConfig).toHaveBeenCalled());
    // Members (gated on canCreate) stays hidden — the two capabilities are
    // independent, so settings must not piggyback on create.
    expect(screen.queryByText(/^Members/)).toBeNull();
  });
});
