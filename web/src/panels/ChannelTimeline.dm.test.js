import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/svelte";
import ChannelTimeline from "./ChannelTimeline.svelte";

// RFC 0048 chat-panel-retirement amendment §B — the consolidated Channels panel
// absorbs the Chat panel's DM ergonomics: a persona entry point opens a DM,
// which renders and polls through the same channel timeline machinery (a chat IS
// a `dm:` channel server-side), while the composer sends via the synchronous
// chat façade (sendChat) with the scope selector + abortable turn preserved.
// Group-channel behaviour stays in ChannelTimeline.test.js; this spec pins the
// DM mode. The backend client is mocked so the wiring is exercised without a
// running orchestrator.
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
  listSessions: vi.fn(),
  createSession: vi.fn(),
}));

import {
  listAgents,
  listChannels,
  getChannelHistory,
  getChatHistory,
  sendChat,
  listSessions,
} from "../lib/api.js";
import { selection } from "../lib/selection.svelte.js";

const AGENTS = [
  { id: "ada", name: "Ada", role: "Researcher", status: "healthy" },
  { id: "runner", name: "Runner", type: "task", status: "healthy" },
];
const CHANNELS = [{ id: "general", name: "General", channel_type: "group" }];
const DM_ID = "dm:ada:local";

function chanMsg(id, content, sender, channelId, ts = "2026-06-03T10:00:00Z") {
  return {
    id,
    channel_id: channelId,
    sender_id: sender,
    content,
    timestamp: ts,
    mentions: [],
  };
}

function historyOf(...messages) {
  return { messages };
}

// Route the shared channel-history mock by id: the DM channel returns the DM
// transcript, any group channel returns the (empty) group history.
function routeHistory(dmHistory) {
  getChannelHistory.mockImplementation((id) =>
    Promise.resolve(id === DM_ID ? dmHistory : historyOf()),
  );
}

// Open a DM by selecting the persona in the panel's persona picker.
async function pickPersona(value = "ada") {
  await fireEvent.change(screen.getByRole("combobox", { name: /persona/i }), {
    target: { value },
  });
}

beforeEach(() => {
  listAgents.mockResolvedValue(AGENTS);
  listChannels.mockResolvedValue({ channels: CHANNELS });
  getChannelHistory.mockResolvedValue(historyOf());
  // The persona has an existing DM: resolving it returns a message carrying the
  // canonical dm channel id. Tests that exercise a fresh DM override this.
  getChatHistory.mockResolvedValue(
    historyOf(chanMsg("d1", "earlier", "local", DM_ID)),
  );
  sendChat.mockResolvedValue({ reply: "Hi!", agent_display_name: "Ada" });
  // The DM composer mounts the ScopeSelector, which loads the session list; an
  // empty list keeps the dropdown present without seeding any fixture sessions.
  listSessions.mockResolvedValue({ sessions: [] });
  selection.dmAgent = "";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("Channels panel — DM mode (§B)", () => {
  it("opens a DM from the persona picker and shows the persona header + history", async () => {
    routeHistory(historyOf(chanMsg("d1", "earlier", "local", DM_ID)));

    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });

    await pickPersona("ada");

    // The DM is resolved read-only via the chat-history endpoint (no create),
    // and its persisted transcript renders through the channel timeline.
    await waitFor(() =>
      expect(getChatHistory).toHaveBeenCalledWith("ada", { userId: "local" }),
    );
    expect(await screen.findByText("Ada")).toBeTruthy(); // persona header
    expect(await screen.findByText(/earlier/)).toBeTruthy();
    await waitFor(() =>
      expect(getChannelHistory).toHaveBeenCalledWith(DM_ID, expect.anything()),
    );
  });

  it("sends a DM turn through the chat façade with a thinking state", async () => {
    routeHistory(historyOf(chanMsg("d1", "earlier", "local", DM_ID)));
    let resolveSend;
    sendChat.mockReturnValue(
      new Promise((resolve) => {
        resolveSend = resolve;
      }),
    );

    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });
    await pickPersona("ada");
    await screen.findByText(/earlier/);

    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "hi Ada" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));

    // The turn rides the chat façade as the acting user (never publishMessage).
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1));
    const [agentId, payload] = sendChat.mock.calls[0];
    expect(agentId).toBe("ada");
    expect(payload).toMatchObject({ message: "hi Ada", userId: "local" });
    // The synchronous round-trip shows a cancellable "Waiting…" status.
    expect(screen.getByText(/waiting for a reply/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /cancel/i })).toBeTruthy();

    resolveSend({ reply: "Hi!", agent_display_name: "Ada" });
    await waitFor(() =>
      expect(screen.queryByText(/waiting for a reply/i)).toBeNull(),
    );
  });

  it("passes the scope-selector overrides on a DM send", async () => {
    routeHistory(historyOf(chanMsg("d1", "earlier", "local", DM_ID)));

    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });
    await pickPersona("ada");
    await screen.findByText(/earlier/);

    // Set an epoch override (free-text) and send — it must ride the request so the
    // v0.3.5 isolation story survives the consolidation (§B).
    await fireEvent.input(screen.getByRole("textbox", { name: /epoch/i }), {
      target: { value: "ep-1" },
    });
    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "scoped hi" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1));
    expect(sendChat.mock.calls[0][1]).toMatchObject({ epochId: "ep-1" });
  });

  it("opens an empty DM for a never-messaged persona and resolves it on first send", async () => {
    // No prior DM: the resolve returns empty (200, not 404), so the conversation
    // opens empty with a usable composer. The first send creates the DM
    // server-side; re-resolving then lights up the channel + its history.
    getChatHistory
      .mockResolvedValueOnce(historyOf())
      .mockResolvedValue(historyOf(chanMsg("d1", "hi Ada", "local", DM_ID)));
    routeHistory(historyOf(chanMsg("d1", "hi Ada", "local", DM_ID)));

    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });
    await pickPersona("ada");

    expect(await screen.findByText(/no messages yet/i)).toBeTruthy();

    await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
      target: { value: "hi Ada" },
    });
    await fireEvent.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1));
    // The re-resolve picks up the freshly-created DM channel and loads its history.
    await waitFor(() =>
      expect(getChannelHistory).toHaveBeenCalledWith(DM_ID, expect.anything()),
    );
    expect(await screen.findByText(/hi Ada/)).toBeTruthy();
  });

  it("disables a task agent in the persona picker", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });

    // Runner is a task agent — present in the picker but not selectable for a DM.
    const runnerOption = screen.getByRole("option", { name: /runner/i });
    expect(runnerOption.disabled).toBe(true);
  });

  it("exits the DM back to the group-channel view", async () => {
    routeHistory(historyOf(chanMsg("d1", "earlier", "local", DM_ID)));

    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });
    await pickPersona("ada");
    await screen.findByText("Ada"); // persona header present

    await fireEvent.click(screen.getByRole("button", { name: /exit/i }));

    // Back in group mode: the persona header is gone and the publish composer
    // returns over the selected group channel.
    await waitFor(() => expect(screen.queryByText("Ada")).toBeNull());
    expect(screen.getByRole("button", { name: /post/i })).toBeTruthy();
  });

  it("never shows the standalone-Chat lobby prompt — the timeline fills the body (§B)", async () => {
    // The consolidated panel has two entry points, so an unselected persona is
    // NOT a dead end: the group timeline shows, not the Chat panel's "pick a
    // persona" lobby. Guards the showLobby removal — this state must stay
    // lobby-free however the picker is built.
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });

    // Persona picker present (an entry point), but no lobby prompt over it.
    expect(screen.getByRole("combobox", { name: /persona/i })).toBeTruthy();
    expect(
      screen.queryByText(/select a persona to start a conversation/i),
    ).toBeNull();
  });

  it("resumes a remembered DM across a remount (sticky selection §B)", async () => {
    // The rehomed sticky selection re-opens a deliberately-chosen DM after the
    // unmount a tab switch causes, rather than snapping back to the group view.
    selection.dmAgent = "ada";
    routeHistory(historyOf(chanMsg("d1", "earlier", "local", DM_ID)));

    render(ChannelTimeline, { props: { userId: "local" } });

    await waitFor(() =>
      expect(getChatHistory).toHaveBeenCalledWith("ada", { userId: "local" }),
    );
    expect(await screen.findByText("Ada")).toBeTruthy();
    expect(await screen.findByText(/earlier/)).toBeTruthy();
  });
});
