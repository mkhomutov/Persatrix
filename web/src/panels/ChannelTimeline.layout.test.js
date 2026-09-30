import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/svelte";
import ChannelTimeline from "./ChannelTimeline.svelte";

// Workspace layout controls: the channel-management rail can be folded away
// (and remembers that per browser), and the conversation list can be opened as
// a drawer on narrow screens. The rail stays mounted while folded so unsaved
// settings survive a peek at the conversation.
vi.mock("../lib/api.js", () => ({
  ApiError: class ApiError extends Error {},
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
  listSessions: vi.fn(() => Promise.resolve({ sessions: [] })),
}));

import {
  listAgents,
  listChannels,
  getChannelHistory,
  getChannelConfig,
} from "../lib/api.js";
import { selection } from "../lib/selection.svelte.js";

const CHANNELS = [
  {
    id: "general",
    name: "General",
    channel_type: "group",
    members: [
      { id: "local", respond: "never" },
      { id: "ada", respond: "always" },
    ],
  },
];

beforeEach(() => {
  localStorage.clear();
  listAgents.mockResolvedValue([{ id: "ada", name: "Ada", status: "healthy" }]);
  listChannels.mockResolvedValue({ channels: CHANNELS });
  getChannelHistory.mockResolvedValue({ messages: [] });
  getChannelConfig.mockResolvedValue({ revision: 0 });
  selection.dmAgent = "";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  localStorage.clear();
});

describe("Channels panel — workspace layout", () => {
  it("folds and unfolds the channel-management rail from the header", async () => {
    render(ChannelTimeline, {
      props: { userId: "local", canCreate: true, canConfigEdit: true },
    });
    await screen.findByRole("option", { name: "General" });

    const toggle = screen.getByRole("button", { name: /channel details/i });
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("complementary", { name: /channel management/i })).toBeTruthy();

    await fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("complementary", { name: /channel management/i })).toBeNull();

    await fireEvent.click(toggle);
    expect(screen.getByRole("complementary", { name: /channel management/i })).toBeTruthy();
  });

  it("remembers a folded rail across a remount", async () => {
    const first = render(ChannelTimeline, {
      props: { userId: "local", canCreate: true, canConfigEdit: true },
    });
    await screen.findByRole("option", { name: "General" });
    await fireEvent.click(screen.getByRole("button", { name: /channel details/i }));
    first.unmount();

    render(ChannelTimeline, {
      props: { userId: "local", canCreate: true, canConfigEdit: true },
    });
    await screen.findByRole("option", { name: "General" });
    expect(
      screen.getByRole("button", { name: /channel details/i }).getAttribute("aria-expanded"),
    ).toBe("false");
  });

  it("closes the rail from its own close button", async () => {
    render(ChannelTimeline, {
      props: { userId: "local", canCreate: true, canConfigEdit: true },
    });
    await screen.findByRole("option", { name: "General" });

    await fireEvent.click(screen.getByRole("button", { name: /close details/i }));

    await waitFor(() =>
      expect(screen.queryByRole("complementary", { name: /channel management/i })).toBeNull(),
    );
  });

  it("offers no details toggle when there is nothing to manage", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });
    expect(screen.queryByRole("button", { name: /channel details/i })).toBeNull();
  });

  it("opens the drawer from Ctrl+K only where the sidebar is a drawer", async () => {
    // On a wide layout the sidebar is always visible, so the jump shortcut must
    // not flip the drawer state (its scrim would take a grid cell).
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });
    const open = screen.getByRole("button", { name: /show conversations/i });

    await fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(open.getAttribute("aria-expanded")).toBe("false");

    // A narrow layout: the same shortcut opens the drawer to reach the box.
    vi.stubGlobal("matchMedia", (query) => ({ matches: query.includes("max-width") }));
    try {
      await fireEvent.keyDown(window, { key: "k", ctrlKey: true });
      expect(open.getAttribute("aria-expanded")).toBe("true");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("keeps the error card up while a retry runs, never an empty workspace", async () => {
    let resolveRetry;
    listChannels
      .mockRejectedValueOnce(new Error("backend down"))
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveRetry = resolve;
          }),
      );
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("alert");

    await fireEvent.click(screen.getByRole("button", { name: /retry/i }));

    // In flight: still the error card (its button busy), not a channel-less
    // workspace or the both-empty onboarding.
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.getByRole("button", { name: /retry/i }).disabled).toBe(true);
    expect(screen.queryByText(/no group channels yet/i)).toBeNull();

    resolveRetry({ channels: CHANNELS });
    expect(await screen.findByRole("option", { name: "General" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("closes the drawer on Escape and moves focus into the conversation after a pick", async () => {
    vi.stubGlobal("matchMedia", (query) => ({ matches: query.includes("max-width") }));
    try {
      render(ChannelTimeline, { props: { userId: "local" } });
      await screen.findByRole("option", { name: "General" });
      const open = screen.getByRole("button", { name: /show conversations/i });

      await fireEvent.click(open);
      expect(open.getAttribute("aria-expanded")).toBe("true");
      await fireEvent.keyDown(window, { key: "Escape" });
      expect(open.getAttribute("aria-expanded")).toBe("false");

      await fireEvent.click(open);
      await fireEvent.click(screen.getByRole("option", { name: "General" }));
      const conversation = screen.getByRole("region", { name: /conversation$/i });
      await waitFor(() => expect(conversation.contains(document.activeElement)).toBe(true));
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("opens the conversation list as a drawer and closes it after a pick", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });

    const open = screen.getByRole("button", { name: /show conversations/i });
    expect(open.getAttribute("aria-expanded")).toBe("false");
    await fireEvent.click(open);
    expect(open.getAttribute("aria-expanded")).toBe("true");

    await fireEvent.click(screen.getByRole("option", { name: "General" }));
    expect(open.getAttribute("aria-expanded")).toBe("false");
  });
});
