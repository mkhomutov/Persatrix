import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/svelte";
import ChannelTimeline from "./ChannelTimeline.svelte";

// The catch-up for more than one page of messages between two polls
// (ISSUE-0222; the paging itself is in ChannelTimeline.catchup.test.js), seen
// from two sides it must not disturb: a channel switched to while a catch-up
// is still reading, and the place of a person reading the timeline. The
// history endpoint is the same stand-in, answering as the store does.
vi.mock("../lib/api.js", () => ({
  ApiError: class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.name = "ApiError";
      this.status = status;
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
} from "../lib/api.js";
import { selection } from "../lib/selection.svelte.js";
import { fakeHistory, rows } from "../test-history.js";

const CHANNELS = [
  { id: "general", name: "General", channel_type: "group" },
  { id: "ops", name: "Ops", channel_type: "group" },
];

const POLL_MS = 3000;
// The new rows end at NEW; the old ones the day before.
const OLD = "2026-06-01T09:00:00Z";
const NEW = "2026-06-02T12:00:00Z";

let server;

// The rendered rows, oldest first, by their text ("<prefix> <i>").
function shownIds() {
  return screen
    .queryAllByRole("listitem")
    .map((li) => li.querySelector(".content").textContent);
}

// What the stand-in holds for a channel, oldest first: what a complete
// timeline shows.
function storedIds(channel = "general") {
  return server
    .all(channel)
    .map((m) => m.content)
    .reverse();
}

const marker = () =>
  screen.queryByRole("button", { name: "Load missing messages" });

// The history requests that carried a cursor: the catch-up's own.
const pagedCalls = () => server.calls.filter((c) => c.before);

async function mount(initial) {
  server = fakeHistory(initial);
  getChannelHistory.mockImplementation(server.read);
  render(ChannelTimeline, { props: { userId: "local" } });
  // Flush the mount's reads (channels, agents, the first history page).
  await vi.advanceTimersByTimeAsync(0);
  expect(shownIds()).toEqual(storedIds());
}

beforeEach(() => {
  vi.useFakeTimers();
  listAgents.mockResolvedValue([]);
  listChannels.mockResolvedValue({ channels: CHANNELS });
  getChannelActivity.mockImplementation(() => Promise.resolve({ thinking: [] }));
  selection.dmAgent = "";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("Channels panel — a channel switch during a catch-up", () => {
  it("does not put a catch-up page into the channel switched to meanwhile", async () => {
    await mount([...rows("old", 3, OLD), ...rows("ops", 2, OLD, "ops")]);
    server.add(rows("new", 120, NEW));
    // Hold the catch-up page (the first read with a cursor) in flight.
    let release;
    server.hold = (call) =>
      call.before ? new Promise((resolve) => (release = resolve)) : null;

    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(shownIds()).toHaveLength(53); // the head page landed, the page hangs

    server.hold = null;
    await fireEvent.click(screen.getByRole("option", { name: "Ops" }));
    await vi.advanceTimersByTimeAsync(0);
    expect(shownIds()).toEqual(storedIds("ops"));
    const opsReads = () => server.calls.filter((c) => c.id === "ops");
    expect(opsReads()).toHaveLength(1); // its first page

    // The old channel's page answers two seconds into the new one's first
    // poll interval.
    await vi.advanceTimersByTimeAsync(2000);
    release();
    await vi.advanceTimersByTimeAsync(0);

    expect(shownIds()).toEqual(storedIds("ops"));
    expect(marker()).toBeNull();
    // The stale tick neither pages on nor reads the new channel with the old
    // channel's cursor.
    expect(pagedCalls()).toHaveLength(1);
    expect(opsReads().filter((c) => c.before)).toHaveLength(0);
    // Nor does it restart the new channel's poll timer: that poll still comes
    // three seconds after the channel opened.
    await vi.advanceTimersByTimeAsync(1000);
    expect(opsReads()).toHaveLength(2);
  });

  it("does not let a tick that outlived a switch start a second read beside the new channel's", async () => {
    await mount([...rows("old", 3, OLD), ...rows("ops", 2, OLD, "ops")]);
    server.add(rows("new", 120, NEW));
    // Held: general's catch-up page, and ops's first poll (its second read).
    const held = [];
    const opsReads = () => server.calls.filter((c) => c.id === "ops").length;
    server.hold = (call) =>
      call.before || (call.id === "ops" && opsReads() === 2)
        ? new Promise((resolve) => held.push(resolve))
        : null;

    await vi.advanceTimersByTimeAsync(POLL_MS); // general's catch-up page hangs
    await fireEvent.click(screen.getByRole("option", { name: "Ops" }));
    await vi.advanceTimersByTimeAsync(0);
    expect(shownIds()).toEqual(storedIds("ops"));
    await vi.advanceTimersByTimeAsync(POLL_MS); // ops's first poll hangs too
    expect(held).toHaveLength(2);
    const reads = server.calls.length;

    held[0](); // the old channel's page answers while ops's poll is out
    await vi.advanceTimersByTimeAsync(0);
    document.dispatchEvent(new Event("visibilitychange")); // asks for a poll

    expect(server.calls).toHaveLength(reads);
    held[1]();
    await vi.advanceTimersByTimeAsync(0);
    expect(shownIds()).toEqual(storedIds("ops"));
  });

  it("opens no hole in the channel switched to while the tick's activity read is out", async () => {
    // The head page is merged before the tick's activity read answers; a
    // switch in between must end the tick there.
    const answers = [];
    getChannelActivity.mockImplementation(
      () => new Promise((resolve) => answers.push(resolve)),
    );
    const answerAll = () => answers.splice(0).forEach((a) => a({ thinking: [] }));
    await mount([...rows("old", 3, OLD), ...rows("ops", 2, OLD, "ops")]);
    answerAll(); // the read made on opening the channel
    server.add(rows("new", 120, NEW));

    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(shownIds()).toHaveLength(53); // the head page landed
    await fireEvent.click(screen.getByRole("option", { name: "Ops" }));
    await vi.advanceTimersByTimeAsync(0);
    answerAll();
    await vi.advanceTimersByTimeAsync(0);

    expect(shownIds()).toEqual(storedIds("ops"));
    expect(marker()).toBeNull();
    expect(pagedCalls()).toHaveLength(0);
  });
});

describe("Channels panel — the reader's place during a catch-up", () => {
  // jsdom lays nothing out, so the scroll box's measurements are stand-ins:
  // each row is 20px tall and the box shows 200px.
  function measure(el, state) {
    Object.defineProperty(el, "clientHeight", { configurable: true, get: () => 200 });
    Object.defineProperty(el, "scrollHeight", {
      configurable: true,
      get: () => el.querySelectorAll("li").length * 20,
    });
    Object.defineProperty(el, "scrollTop", {
      configurable: true,
      get: () => state.top,
      set: (v) => (state.top = v),
    });
  }

  it("leaves a reader who scrolled up where they are, and counts what arrived", async () => {
    await mount(rows("old", 40, OLD));
    const box = document.querySelector(".timeline-scroll");
    const state = { top: 0 };
    measure(box, state);
    state.top = 100; // reading near the top
    await fireEvent.scroll(box);

    server.add(rows("new", 120, NEW));
    await vi.advanceTimersByTimeAsync(POLL_MS);

    expect(shownIds()).toHaveLength(160);
    expect(state.top).toBe(100);
    // Head rows and caught-up rows alike count for the way back down.
    expect(screen.getByRole("button", { name: "120 new messages" })).toBeTruthy();
  });

  it("keeps a reader at the newest message there", async () => {
    await mount(rows("old", 40, OLD));
    const box = document.querySelector(".timeline-scroll");
    const state = { top: 0 };
    measure(box, state);
    state.top = 40 * 20 - 200; // at the bottom
    await fireEvent.scroll(box);

    server.add(rows("new", 120, NEW));
    await vi.advanceTimersByTimeAsync(POLL_MS);

    expect(state.top).toBe(160 * 20);
    expect(screen.queryByRole("button", { name: /new message|jump to latest/i })).toBeNull();
  });

  it("keeps the rows on screen where they are when an older page loads above them", async () => {
    await mount(rows("old", 80, OLD).slice(0, 50));
    server.add(rows("old", 80, OLD).slice(50));
    const box = document.querySelector(".timeline-scroll");
    const state = { top: 0 };
    measure(box, state);
    state.top = 40; // near the top, by the older-messages control
    await fireEvent.scroll(box);

    await fireEvent.click(screen.getByRole("button", { name: /show older messages/i }));
    await vi.advanceTimersByTimeAsync(0);

    expect(shownIds()).toHaveLength(80);
    // Thirty rows of 20px went in above: the view moved down by as much.
    expect(state.top).toBe(40 + 30 * 20);
  });

  it("holds the view on a marker click, so the loaded rows run on from the marker", async () => {
    await mount(rows("old", 3, OLD));
    server.add(rows("new", 690, NEW));
    await vi.advanceTimersByTimeAsync(POLL_MS);
    const box = document.querySelector(".timeline-scroll");
    const state = { top: 0 };
    measure(box, state);
    // At the newest message, where a new row would pull the view down with it.
    const top = 650 * 20 - 200;
    state.top = top;
    await fireEvent.scroll(box);
    expect(screen.queryByRole("button", { name: "Jump to latest" })).toBeNull();

    await fireEvent.click(marker());
    await vi.advanceTimersByTimeAsync(0);

    expect(shownIds()).toHaveLength(693);
    // The view did not move, so it is now 43 rows above the newest message.
    expect(state.top).toBe(top);
    expect(screen.getByRole("button", { name: "Jump to latest" })).toBeTruthy();
  });
});
