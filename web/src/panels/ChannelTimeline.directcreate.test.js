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
// the New channel form's "Direct message" type opens a 1:1 DM with one persona by
// sending an opening message through the chat façade (GetOrCreateDM on first
// message). Post chat-panel-retirement amendment §C the form no longer lands a
// dm: row in the group-channel picker (DMs are filtered out there); instead it
// hands the persona back and the panel opens DM mode for it — the same
// consolidated conversation surface the top persona picker uses. Split from the
// group create spec to keep each file under the review-size cap.
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
  publishMessage: vi.fn(),
  createChannel: vi.fn(),
  sendChat: vi.fn(),
  listSessions: vi.fn(),
  createSession: vi.fn(),
}));

import {
  listAgents,
  listChannels,
  getChannelHistory,
  getChatHistory,
  createChannel,
  sendChat,
  listSessions,
} from "../lib/api.js";
import { selection } from "../lib/selection.svelte.js";

const CHANNELS = [{ id: "general", name: "General", channel_type: "group" }];
const DM_ID = "dm:ada:local";

const AGENTS = [
  { id: "ada", name: "Ada", role: "Researcher", status: "healthy" },
  { id: "bob", name: "Bob", role: "Writer", status: "healthy" },
];

function chanMsg(id, content, sender, channelId, ts = "2026-06-03T10:00:00Z") {
  return { id, channel_id: channelId, sender_id: sender, content, timestamp: ts, mentions: [] };
}

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
  getChannelHistory.mockImplementation((id) =>
    Promise.resolve(
      id === DM_ID
        ? historyOf(chanMsg("d1", "hello Ada", "local", DM_ID))
        : historyOf(),
    ),
  );
  // After the opening chat the DM exists; resolving it returns the dm channel id.
  getChatHistory.mockResolvedValue(
    historyOf(chanMsg("d1", "hello Ada", "local", DM_ID)),
  );
  sendChat.mockResolvedValue({ reply: "Hi!", agent_display_name: "Ada" });
  listSessions.mockResolvedValue({ sessions: [] });
  selection.dmAgent = "";
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

    // Direct mode: a single-persona select (distinctly named so it doesn't
    // collide with the panel's top persona picker) and an opening-message box.
    expect(
      await screen.findByRole("combobox", { name: /direct-message persona/i }),
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

    const picker = await screen.findByRole("combobox", {
      name: /direct-message persona/i,
    });
    // Scope the option check to the direct picker (the top picker lists personas
    // too); Runner must not be offered as a DM target there.
    expect(
      [...picker.options].some((o) => /ada/i.test(o.textContent)),
    ).toBe(true);
    expect(
      [...picker.options].some((o) => /runner/i.test(o.textContent)),
    ).toBe(false);
  });

  it("keeps start disabled until a persona and an opening message are set", async () => {
    render(ChannelTimeline, { props: { userId: "local", canCreate: true } });
    await screen.findByRole("option", { name: "General" });
    await openDirect();

    const start = screen.getByRole("button", { name: /start/i });
    expect(start.disabled).toBe(true);

    await fireEvent.change(
      screen.getByRole("combobox", { name: /direct-message persona/i }),
      { target: { value: "ada" } },
    );
    expect(start.disabled).toBe(true); // persona but no message yet

    await fireEvent.input(
      screen.getByRole("textbox", { name: /opening message/i }),
      { target: { value: "hello" } },
    );
    expect(start.disabled).toBe(false);
  });

  it("creates the DM via the chat façade and opens DM mode for the persona", async () => {
    render(ChannelTimeline, { props: { userId: "local", canCreate: true } });
    await screen.findByRole("option", { name: "General" });
    await openDirect();

    await fireEvent.change(
      screen.getByRole("combobox", { name: /direct-message persona/i }),
      { target: { value: "ada" } },
    );
    await fireEvent.input(
      screen.getByRole("textbox", { name: /opening message/i }),
      { target: { value: "hello Ada" } },
    );
    await fireEvent.click(
      screen.getByRole("button", { name: /start/i }),
    );

    // The opening message goes through the chat façade as the acting user.
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1));
    const [agentId, payload] = sendChat.mock.calls[0];
    expect(agentId).toBe("ada");
    expect(payload).toMatchObject({ message: "hello Ada", userId: "local" });
    // No group channel was created in direct mode.
    expect(createChannel).not.toHaveBeenCalled();

    // The panel opens DM mode: the form collapses, the persona header shows, and
    // the resolved dm: channel's history renders.
    await waitFor(() =>
      expect(screen.queryByRole("radio", { name: /direct/i })).toBeNull(),
    );
    expect(await screen.findByText("Ada")).toBeTruthy(); // persona header
    await waitFor(() =>
      expect(getChatHistory).toHaveBeenCalledWith("ada", { userId: "local" }),
    );
    // The top persona picker now reflects the opened DM.
    expect(screen.getByRole("combobox", { name: "Persona" }).value).toBe("ada");
  });
});
