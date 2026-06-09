import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  within,
  cleanup,
  fireEvent,
} from "@testing-library/svelte";
import ChannelTimeline from "./ChannelTimeline.svelte";

// Console @-mention wiring (RFC 0011 over RFC 0048): the Channels panel feeds the
// watched channel's members into the composer's typeahead and lifts the `@id`
// tokens that resolve to a member into the publish `mentions` array. The backend
// client is mocked so the panel's wiring is exercised without a running
// orchestrator. Split from ChannelTimeline.test.js to keep each spec under the
// review-size cap, mirroring the create/dm/crosspanel splits.
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
}));

import {
  listAgents,
  listChannels,
  getChannelHistory,
  getChatHistory,
  publishMessage,
} from "../lib/api.js";
import { selection } from "../lib/selection.svelte.js";

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
  listAgents.mockResolvedValue([]);
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
});

describe("Channel timeline @-mentions", () => {
  it("lifts @mentions of channel members into the publish payload", async () => {
    // An `@id` that resolves to a channel member is lifted into the publish
    // `mentions` array (driving the fan-out gate); the prose posts verbatim.
    listChannels.mockResolvedValue({
      channels: [channelWithMembers("local", "ember-owl")],
    });

    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });

    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "@ember-owl your read?" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /post/i }));

    await waitFor(() => {
      expect(publishMessage).toHaveBeenCalledWith("general", {
        senderId: "local",
        content: "@ember-owl your read?",
        mentions: ["ember-owl"],
      });
    });
  });

  it("does not attach a mentions array for a plain publish", async () => {
    // Keep the no-mention call shape identical to the pre-feature wire.
    listChannels.mockResolvedValue({
      channels: [channelWithMembers("local", "ember-owl")],
    });

    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });

    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "just a status update" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /post/i }));

    await waitFor(() => {
      expect(publishMessage).toHaveBeenCalledWith("general", {
        senderId: "local",
        content: "just a status update",
      });
    });
  });

  it("offers the channel's members as @-mention candidates, excluding the operator", async () => {
    listChannels.mockResolvedValue({
      channels: [channelWithMembers("local", "ember-owl", "iron-fox")],
    });

    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });

    const textarea = screen.getByRole("textbox", { name: /message/i });
    textarea.value = "@";
    textarea.selectionStart = 1;
    textarea.selectionEnd = 1;
    await fireEvent.input(textarea);

    const menu = await screen.findByRole("listbox", {
      name: /channel members/i,
    });
    const labels = within(menu)
      .getAllByRole("option")
      .map((o) => o.textContent)
      .join(" ");
    expect(labels).toContain("@ember-owl");
    expect(labels).toContain("@iron-fox");
    expect(labels).not.toContain("@local");
  });
});
