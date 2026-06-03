import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/svelte";
import ChannelTimeline from "./ChannelTimeline.svelte";

// Direct-message creation (RFC 0048 channel-creation amendment §B, direct mode):
// the New channel form's "Direct message" type opens a 1:1 DM with one persona.
// A DM is born by chatting (GetOrCreateDM on first message), so direct mode picks
// a persona + an opening message, sends it via the chat façade, and lands the
// operator in the resolved dm: channel on the timeline. Split from the group
// create spec to keep each file under the review-size cap.
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
  publishMessage: vi.fn(),
  createChannel: vi.fn(),
  sendChat: vi.fn(),
}));

import {
  listAgents,
  listChannels,
  getChannelHistory,
  createChannel,
  sendChat,
} from "../lib/api.js";
import { nav } from "../lib/nav.svelte.js";

const CHANNELS = [{ id: "general", name: "General", channel_type: "group" }];
const DM = { id: "dm:ada:local", name: "Ada", channel_type: "dm" };

const AGENTS = [
  { id: "ada", name: "Ada", role: "Researcher", status: "healthy" },
  { id: "bob", name: "Bob", role: "Writer", status: "healthy" },
];

function historyOf(...messages) {
  return { messages };
}

// Open the form and switch it to direct mode.
async function openDirect() {
  await fireEvent.click(screen.getByRole("button", { name: /new channel/i }));
  await fireEvent.click(await screen.findByRole("radio", { name: /direct/i }));
}

beforeEach(() => {
  listAgents.mockResolvedValue(AGENTS);
  listChannels.mockResolvedValue({ channels: CHANNELS });
  getChannelHistory.mockResolvedValue(historyOf());
  // The chat façade creates the DM and returns its resolved channel id.
  sendChat.mockResolvedValue({
    reply: "Hi!",
    agent_id: "ada",
    channel_id: "dm:ada:local",
  });
  nav.targetChannel = "";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("Direct-channel creation", () => {
  it("offers a channel-type choice with group as the default", async () => {
    render(ChannelTimeline, { props: { userId: "local", canCreate: true } });
    await screen.findByRole("option", { name: "General" });
    await fireEvent.click(screen.getByRole("button", { name: /new channel/i }));

    const group = await screen.findByRole("radio", { name: /group/i });
    const direct = screen.getByRole("radio", { name: /direct/i });
    expect(group.checked).toBe(true);
    expect(direct.checked).toBe(false);
  });

  it("direct mode shows a persona picker + opening message and hides the group fields", async () => {
    render(ChannelTimeline, { props: { userId: "local", canCreate: true } });
    await screen.findByRole("option", { name: "General" });
    await openDirect();

    // Direct mode: a single-persona select and an opening-message box.
    expect(
      await screen.findByRole("combobox", { name: /persona/i }),
    ).toBeTruthy();
    expect(
      screen.getByRole("textbox", { name: /opening message/i }),
    ).toBeTruthy();
    // The group-only fields are gone (no channel name, no member checkboxes).
    expect(
      screen.queryByRole("textbox", { name: /channel name/i }),
    ).toBeNull();
    expect(screen.queryByRole("checkbox", { name: /ada/i })).toBeNull();
  });

  it("lists only persona agents in the direct picker", async () => {
    listAgents.mockResolvedValue([
      ...AGENTS,
      { id: "runner", name: "Runner", type: "task", status: "healthy" },
    ]);
    render(ChannelTimeline, { props: { userId: "local", canCreate: true } });
    await screen.findByRole("option", { name: "General" });
    await openDirect();

    await screen.findByRole("combobox", { name: /persona/i });
    expect(screen.getByRole("option", { name: "Ada" })).toBeTruthy();
    expect(screen.queryByRole("option", { name: "Runner" })).toBeNull();
  });

  it("keeps start disabled until a persona and an opening message are set", async () => {
    render(ChannelTimeline, { props: { userId: "local", canCreate: true } });
    await screen.findByRole("option", { name: "General" });
    await openDirect();

    const start = screen.getByRole("button", { name: /start/i });
    expect(start.disabled).toBe(true);

    await fireEvent.change(screen.getByRole("combobox", { name: /persona/i }), {
      target: { value: "ada" },
    });
    expect(start.disabled).toBe(true); // persona but no message yet

    await fireEvent.input(
      screen.getByRole("textbox", { name: /opening message/i }),
      { target: { value: "hello" } },
    );
    expect(start.disabled).toBe(false);
  });

  it("creates the DM via the chat façade and lands in the resolved dm channel", async () => {
    // After the chat, the channel list includes the new DM so the picker can
    // select it (loadChannels honours nav.targetChannel).
    listChannels
      .mockResolvedValueOnce({ channels: CHANNELS })
      .mockResolvedValue({ channels: [...CHANNELS, DM] });

    render(ChannelTimeline, { props: { userId: "local", canCreate: true } });
    await screen.findByRole("option", { name: "General" });
    await openDirect();

    await fireEvent.change(screen.getByRole("combobox", { name: /persona/i }), {
      target: { value: "ada" },
    });
    await fireEvent.input(
      screen.getByRole("textbox", { name: /opening message/i }),
      { target: { value: "hello Ada" } },
    );
    await fireEvent.click(screen.getByRole("button", { name: /start/i }));

    // The opening message goes through the chat façade as the acting user.
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1));
    const [agentId, payload] = sendChat.mock.calls[0];
    expect(agentId).toBe("ada");
    expect(payload).toMatchObject({ message: "hello Ada", userId: "local" });
    // No group channel was created in direct mode.
    expect(createChannel).not.toHaveBeenCalled();

    // The operator lands in the resolved dm channel, and the form collapses.
    await waitFor(() => {
      const picker = screen.getByRole("combobox", { name: /channel/i });
      expect(picker.value).toBe("dm:ada:local");
    });
    expect(screen.queryByRole("radio", { name: /direct/i })).toBeNull();
  });
});
