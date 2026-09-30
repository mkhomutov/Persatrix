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

// The conversation sidebar: group channels and personas as two keyboard-driven
// lists, narrowed by one "Jump to…" box that Ctrl/⌘+K focuses from anywhere.
// It has to stay usable with hundreds of rows, so it sorts by name, filters by
// every typed word, and opens the first match on Enter.
vi.mock("../lib/api.js", () => ({
  ApiError: class ApiError extends Error {},
  listAgents: vi.fn(),
  listChannels: vi.fn(),
  getChannelHistory: vi.fn(),
  getChatHistory: vi.fn(),
  sendChat: vi.fn(),
  publishMessage: vi.fn(),
  getClosedInteractions: vi.fn(() => Promise.resolve({ interactions: [] })),
  listSessions: vi.fn(() => Promise.resolve({ sessions: [] })),
  createSession: vi.fn(),
}));

import {
  listAgents,
  listChannels,
  getChannelHistory,
  getChatHistory,
} from "../lib/api.js";
import { selection } from "../lib/selection.svelte.js";

const AGENTS = [
  { id: "zed", name: "Zed", role: "Critic", status: "healthy" },
  { id: "runner", name: "Runner", type: "task", status: "healthy" },
  { id: "ada", name: "Ada", role: "Researcher", status: "healthy" },
  { id: "bob", name: "Bob", status: "degraded" },
];
const CHANNELS = [
  { id: "ops", name: "Ops", channel_type: "group", description: "On-call handoffs" },
  { id: "general", name: "General", channel_type: "group" },
  { id: "dm:ada:local", channel_type: "dm" },
];

function optionNames(listName) {
  const list = screen.getByRole("listbox", { name: listName });
  return within(list)
    .getAllByRole("option")
    .map((o) => o.getAttribute("aria-label"));
}

beforeEach(() => {
  listAgents.mockResolvedValue(AGENTS);
  listChannels.mockResolvedValue({ channels: CHANNELS });
  getChannelHistory.mockResolvedValue({ messages: [] });
  getChatHistory.mockResolvedValue({ messages: [] });
  selection.dmAgent = "";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Channels panel — conversation sidebar", () => {
  it("lists group channels and personas as two labelled lists, sorted by name", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });

    // DMs never show as raw channel rows; they are reached through the persona.
    expect(optionNames(/channel/i)).toEqual(["General", "Ops"]);
    // Personas by name, the task agent last (shown, but not chattable).
    expect(optionNames(/persona/i)).toEqual([
      "Ada — Researcher",
      "Bob (degraded)",
      "Zed — Critic",
      "Runner (task agent — not chattable)",
    ]);
  });

  it("watches the first channel in name order by default", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    expect(
      await screen.findByRole("option", { name: "General", selected: true }),
    ).toBeTruthy();
    await waitFor(() => expect(getChannelHistory).toHaveBeenCalledWith("general", expect.anything()));
  });

  it("narrows both lists from the Jump to box", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });

    const filter = screen.getByRole("searchbox", { name: /filter conversations/i });
    await fireEvent.input(filter, { target: { value: "on-call" } });

    // Matches the description, not just the name.
    expect(optionNames(/channel/i)).toEqual(["Ops"]);
    expect(screen.queryByRole("listbox", { name: /persona/i })).toBeNull();
    expect(screen.getByText(/no personas match/i)).toBeTruthy();
  });

  it("focuses the Jump to box on Ctrl+K off a Mac", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });
    const filter = screen.getByRole("searchbox", { name: /filter conversations/i });

    await fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(document.activeElement).toBe(filter);
  });

  it("uses ⌘K on a Mac and leaves Ctrl+K to the text field", async () => {
    // macOS text fields use Ctrl+K to delete to the end of the line.
    Object.defineProperty(window.navigator, "platform", {
      value: "MacIntel",
      configurable: true,
    });
    try {
      render(ChannelTimeline, { props: { userId: "local" } });
      await screen.findByRole("option", { name: "General" });
      const filter = screen.getByRole("searchbox", { name: /filter conversations/i });

      await fireEvent.keyDown(window, { key: "k", ctrlKey: true });
      expect(document.activeElement).not.toBe(filter);

      await fireEvent.keyDown(window, { key: "k", metaKey: true });
      expect(document.activeElement).toBe(filter);
    } finally {
      delete window.navigator.platform;
    }
  });

  it("answers Ctrl+K on a non-Latin keyboard layout", async () => {
    // With a Russian layout the K key reports key "л"; the physical key
    // (code "KeyK") is what the shortcut means.
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });
    const filter = screen.getByRole("searchbox", { name: /filter conversations/i });

    await fireEvent.keyDown(window, { key: "л", code: "KeyK", ctrlKey: true });
    expect(document.activeElement).toBe(filter);
  });

  it("closes only the new-channel dialog on Escape, not the drawer under it", async () => {
    vi.stubGlobal("matchMedia", (query) => ({ matches: query.includes("max-width") }));
    try {
      render(ChannelTimeline, { props: { userId: "local", canCreate: true } });
      await screen.findByRole("option", { name: "General" });
      const menu = screen.getByRole("button", { name: /show conversations/i });
      await fireEvent.click(menu);
      expect(menu.getAttribute("aria-expanded")).toBe("true");

      await fireEvent.click(screen.getByRole("button", { name: /new channel/i }));
      await screen.findByRole("dialog");
      await fireEvent.keyDown(window, { key: "Escape" });

      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(menu.getAttribute("aria-expanded")).toBe("true");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("does nothing on Enter in an empty box — an open DM stays open", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });
    await fireEvent.click(screen.getByRole("option", { name: /^Ada/ }));
    await screen.findByRole("heading", { name: "Ada" });

    const filter = screen.getByRole("searchbox", { name: /filter conversations/i });
    await fireEvent.keyDown(filter, { key: "Enter" });

    expect(screen.getByRole("heading", { name: "Ada" })).toBeTruthy();
  });

  it("keeps sections open while filtering, without touching their saved fold", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });
    const filter = screen.getByRole("searchbox", { name: /filter conversations/i });

    await fireEvent.input(filter, { target: { value: "ops" } });
    const toggle = screen.getByRole("button", { name: /^channels/i });
    expect(toggle.disabled).toBe(true);

    await fireEvent.input(filter, { target: { value: "" } });
    expect(screen.getByRole("listbox", { name: /channel/i })).toBeTruthy();
    expect(localStorage.getItem("persatrix.console.railFolded")).toBeNull();
  });

  it("does not move focus out of an open dialog on Ctrl+K", async () => {
    render(ChannelTimeline, { props: { userId: "local", canCreate: true } });
    await screen.findByRole("option", { name: "General" });
    await fireEvent.click(screen.getByRole("button", { name: /new channel/i }));
    const dialog = await screen.findByRole("dialog");

    await fireEvent.keyDown(window, { key: "k", ctrlKey: true });

    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it("opens the first match on Enter and clears the box", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });
    const filter = screen.getByRole("searchbox", { name: /filter conversations/i });

    await fireEvent.input(filter, { target: { value: "ops" } });
    await fireEvent.keyDown(filter, { key: "Enter" });

    expect(await screen.findByRole("option", { name: "Ops", selected: true })).toBeTruthy();
    await waitFor(() => expect(getChannelHistory).toHaveBeenCalledWith("ops", expect.anything()));
    expect(filter.value).toBe("");
  });

  it("opens a persona's DM when the first match is a persona", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });
    const filter = screen.getByRole("searchbox", { name: /filter conversations/i });

    await fireEvent.input(filter, { target: { value: "researcher" } });
    await fireEvent.keyDown(filter, { key: "Enter" });

    await waitFor(() =>
      expect(getChatHistory).toHaveBeenCalledWith("ada", { userId: "local" }),
    );
    expect(await screen.findByRole("heading", { name: "Ada" })).toBeTruthy();
  });

  it("clears the box on Escape", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });
    const filter = screen.getByRole("searchbox", { name: /filter conversations/i });

    await fireEvent.input(filter, { target: { value: "zzz" } });
    await fireEvent.keyDown(filter, { key: "Escape" });

    expect(filter.value).toBe("");
    expect(optionNames(/channel/i)).toEqual(["General", "Ops"]);
  });

  it("moves from the box into the list on ArrowDown", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });
    const filter = screen.getByRole("searchbox", { name: /filter conversations/i });

    await fireEvent.keyDown(filter, { key: "ArrowDown" });

    expect(document.activeElement.getAttribute("aria-label")).toBe("General");
  });

  it("returns from a DM to a channel by picking it — even the one watched before", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General", selected: true });

    await fireEvent.click(screen.getByRole("option", { name: /^Ada/ }));
    expect(await screen.findByRole("heading", { name: "Ada" })).toBeTruthy();
    // While the DM is open, the persona is the selected conversation.
    expect(screen.queryByRole("option", { name: "General", selected: true })).toBeNull();

    await fireEvent.click(screen.getByRole("option", { name: "General" }));

    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Ada" })).toBeNull(),
    );
    expect(screen.getByRole("option", { name: "General", selected: true })).toBeTruthy();
    expect(screen.getByRole("button", { name: /post/i })).toBeTruthy();
  });

  it("refreshes personas and channels from one Refresh button", async () => {
    render(ChannelTimeline, { props: { userId: "local" } });
    await screen.findByRole("option", { name: "General" });

    await fireEvent.click(screen.getByRole("button", { name: /^refresh$/i }));

    await waitFor(() => expect(listAgents).toHaveBeenCalledTimes(2));
    expect(listChannels).toHaveBeenCalledTimes(2);
  });
});
