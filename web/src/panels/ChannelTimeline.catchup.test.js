import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  within,
  cleanup,
  fireEvent,
} from "@testing-library/svelte";
import ChannelTimeline from "./ChannelTimeline.svelte";

// More than one page of messages between two polls (ISSUE-0222). The head poll
// reads the newest 50 every three seconds and stops while the tab is hidden,
// so a busy channel can put more than 50 messages between two reads. The feed
// then pages backwards until it meets a message it already shows; after three
// pages it stops and draws a marker in the timeline that loads the rest on a
// click. A page starts one nanosecond after the oldest row already read, so
// it brings that row again: a page of 200 adds 199 rows. The history endpoint is replaced by a stand-in that answers as the
// store does, so the paging is exercised end to end without an orchestrator.
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
  ApiError,
} from "../lib/api.js";
import { selection } from "../lib/selection.svelte.js";
import { fakeHistory, rows } from "../test-history.js";

const CHANNELS = [
  { id: "general", name: "General", channel_type: "group" },
  { id: "ops", name: "Ops", channel_type: "group" },
];

const POLL_MS = 3000;
// The new rows end at NEW; the old ones the day before, or (NEAR) seconds
// before the oldest of 690 new rows, on one day with them in every time zone.
const OLD = "2026-06-01T09:00:00Z";
const NEW = "2026-06-02T12:00:00Z";
const NEAR = "2026-06-02T11:48:20Z";

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

async function mount(initial, options) {
  server = fakeHistory(initial, options);
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

describe("Channels panel — more than one page between two polls", () => {
  it("shows every message when a poll finds a whole page of new ones", async () => {
    await mount(rows("old", 3, OLD));
    server.add(rows("new", 120, NEW));

    await vi.advanceTimersByTimeAsync(POLL_MS);

    // All 123, oldest first, each once — and nothing left to load.
    expect(shownIds()).toEqual(storedIds());
    expect(shownIds()).toHaveLength(123);
    expect(marker()).toBeNull();
    // One page was enough: the head's 50, then 70 new rows and the join.
    expect(pagedCalls()).toHaveLength(1);
  });

  it("catches up when a hidden tab is shown again", async () => {
    const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(false);
    await mount(rows("old", 3, OLD));

    hidden.mockReturnValue(true);
    document.dispatchEvent(new Event("visibilitychange"));
    server.add(rows("new", 80, NEW));
    await vi.advanceTimersByTimeAsync(10 * POLL_MS);
    expect(shownIds()).toHaveLength(3); // nothing is read while hidden

    hidden.mockReturnValue(false);
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);

    expect(shownIds()).toEqual(storedIds());
    expect(shownIds()).toHaveLength(83);
  });

  it("reads nothing back when a full head page holds messages already shown", async () => {
    // A busy channel's every poll is a full page; only one with nothing
    // known on it means messages were missed.
    await mount(rows("old", 50, OLD));
    server.add(rows("new", 5, NEW));

    await vi.advanceTimersByTimeAsync(POLL_MS);

    expect(shownIds()).toEqual(storedIds());
    expect(shownIds()).toHaveLength(55);
    expect(pagedCalls()).toHaveLength(0);
    expect(marker()).toBeNull();
  });

  it("reads nothing back when a full head page holds one message already shown, and only one", async () => {
    await mount(rows("old", 3, OLD));
    // 49 arrive: the page's oldest row is the newest message on screen, so
    // the page reaches what is loaded and nothing lies between.
    server.add(rows("new", 49, NEW));

    await vi.advanceTimersByTimeAsync(POLL_MS);

    expect(shownIds()).toEqual(storedIds());
    expect(shownIds()).toHaveLength(52);
    expect(pagedCalls()).toHaveLength(0);
    expect(marker()).toBeNull();
  });

  it("stops after three pages, marks the hole, and loads the rest on a click", async () => {
    await mount(rows("old", 3, NEAR));
    // 50 on the head page, 597 on three catch-up pages, 43 beyond the bound.
    server.add(rows("new", 690, NEW));
    const before = server.calls.length;

    await vi.advanceTimersByTimeAsync(POLL_MS);

    // The tick cost four history reads: the head and three pages of 200.
    const tick = server.calls.slice(before);
    expect(tick.map((c) => c.limit)).toEqual([50, 200, 200, 200]);
    expect(shownIds()).toHaveLength(3 + 647);
    // The marker sits on the row just below the hole, inside that row's list
    // item: the timeline's items stay one per message.
    const items = screen.getAllByRole("listitem");
    const below = items[3];
    expect(within(below).getByRole("button", { name: "Load missing messages" })).toBeTruthy();
    // Beside the button, a line that says what the hole is.
    expect(within(below).getByText("Some messages here are not loaded yet.")).toBeTruthy();
    expect(below.querySelector(".content").textContent).toBe("new 646");
    // That row shows its sender although the row above it has the same one
    // within five minutes: across a hole a row is never a continuation.
    expect(below.classList.contains("compact")).toBe(false);
    expect(items[5].classList.contains("compact")).toBe(true);

    // Later polls do not page again on their own: the hole waits for a click.
    await vi.advanceTimersByTimeAsync(5 * POLL_MS);
    expect(pagedCalls()).toHaveLength(3);
    expect(shownIds()).toHaveLength(3 + 647);

    await fireEvent.click(marker());
    await vi.advanceTimersByTimeAsync(0);

    expect(pagedCalls()).toHaveLength(4);
    expect(shownIds()).toEqual(storedIds());
    expect(shownIds()).toHaveLength(693);
    expect(marker()).toBeNull();
  });

  // Tied rows in the order the store lists them, and the other way round:
  // the store's query does not promise an order among them.
  it.each([
    ["the first written listed first, as the store lists them", false],
    ["the last written listed first", true],
  ])("keeps messages that share a timestamp across a page boundary, once each (%s)", async (_, tiesLastWrittenFirst) => {
    // Rows either side of the head page's edge share one instant, and so do
    // the newest old row and the two oldest new ones (the join).
    const old = rows("old", 3, OLD);
    await mount(old, { tiesLastWrittenFirst });
    const fresh = rows("new", 120, NEW);
    // The head page ends after row 49: rows 48 to 51 share one instant, so
    // two of them are on the head page and two are cut off behind it.
    fresh[48].timestamp = fresh[50].timestamp = fresh[51].timestamp = fresh[49].timestamp;
    fresh[118].timestamp = fresh[119].timestamp = old[0].timestamp; // the join
    server.add(fresh);

    await vi.advanceTimersByTimeAsync(POLL_MS);

    const shown = shownIds();
    expect(new Set(shown).size).toBe(shown.length); // none twice
    expect([...shown].sort()).toEqual([...storedIds()].sort()); // none lost
    expect(shown).toHaveLength(123);
    expect(marker()).toBeNull();
  });

  it("keeps what it read when a page fails, marks the hole, and backs the poll off", async () => {
    await mount(rows("old", 3, OLD));
    server.add(rows("new", 350, NEW)); // head 50, a page of 199, then the rest
    // The second catch-up page is refused, as the rate limiter refuses it.
    // The text is the one the client makes of that refusal today: it cannot
    // read the limiter's nested error body, so it names the request.
    let refused;
    server.fail = (call) => {
      if (!call.before || pagedCalls().length !== 2) return null;
      const query = new URLSearchParams({ limit: call.limit, before: call.before });
      refused = `/api/v1/channels/general/messages?${query} responded 429`;
      return new ApiError(refused, 429);
    };

    await vi.advanceTimersByTimeAsync(POLL_MS);

    expect(shownIds()).toHaveLength(3 + 249);
    expect(marker()).not.toBeNull();
    expect(screen.getByText(`Live updates paused: ${refused}`)).toBeTruthy();

    // Backed off as a failed poll is: nothing at the base interval, the next
    // read at twice it — and that read is a head read, not a retried page.
    server.fail = null;
    const after = server.calls.length;
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(server.calls).toHaveLength(after);
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(server.calls).toHaveLength(after + 1);
    expect(server.calls.at(-1).before).toBeUndefined();
    expect(screen.queryByText(/Live updates paused/)).toBeNull();
    expect(shownIds()).toHaveLength(3 + 249);

    await fireEvent.click(marker());
    await vi.advanceTimersByTimeAsync(0);

    expect(shownIds()).toEqual(storedIds());
    expect(marker()).toBeNull();
  });

  it("shows why a click could not load the hole, and loads it on the next click", async () => {
    await mount(rows("old", 3, NEAR));
    server.add(rows("new", 690, NEW));
    await vi.advanceTimersByTimeAsync(POLL_MS);

    server.fail = (call) => (call.before ? new ApiError("backend down", 503) : null);
    await fireEvent.click(marker());
    await vi.advanceTimersByTimeAsync(0);

    // Announced to a screen reader as the poll's own notice is.
    const failure = screen.getByText("Could not load the missing messages: backend down");
    expect(screen.getAllByRole("status")).toContain(failure);
    // A failed click is not a failed poll: live updates carry on.
    expect(screen.queryByText(/Live updates paused/)).toBeNull();
    expect(shownIds()).toHaveLength(3 + 647);

    // The next click's read is held for a moment: meanwhile the button says
    // so and cannot be pressed, and the failure's text is gone.
    server.fail = null;
    let answer;
    server.hold = (call) =>
      call.before ? new Promise((resolve) => (answer = resolve)) : null;
    await fireEvent.click(marker());
    const busy = screen.getByRole("button", { name: "Loading missing messages…" });
    expect(busy.disabled).toBe(true);
    expect(marker()).toBeNull();
    expect(screen.queryByText(/Could not load the missing messages/)).toBeNull();

    answer();
    await vi.advanceTimersByTimeAsync(0);

    expect(shownIds()).toEqual(storedIds());
    expect(screen.queryByRole("button", { name: /missing messages/ })).toBeNull();
  });

  it("ends without a marker when the catch-up reaches the channel's first message", async () => {
    // The channel keeps only its newest rows: the three the feed shows are
    // gone from the store by the time it pages back, so no page can join.
    await mount(rows("old", 3, OLD));
    server.replace(rows("new", 120, NEW));

    await vi.advanceTimersByTimeAsync(POLL_MS);

    expect(shownIds()).toEqual([
      ...rows("old", 3, OLD).map((m) => m.content).reverse(),
      ...storedIds(),
    ]);
    expect(marker()).toBeNull();
    expect(pagedCalls()).toHaveLength(1); // the short page ended it
    await vi.advanceTimersByTimeAsync(5 * POLL_MS);
    expect(pagedCalls()).toHaveLength(1);
  });

  it("offers older messages, with no extra read, when an empty conversation fills past one page", async () => {
    await mount([]);
    server.add(rows("new", 80, NEW));

    await vi.advanceTimersByTimeAsync(POLL_MS);

    // Nothing older is shown, so there is nothing to join: the newest 50 are
    // on screen and the control at the top pages back, as after a first load.
    expect(shownIds()).toHaveLength(50);
    expect(pagedCalls()).toHaveLength(0);
    expect(marker()).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: /show older messages/i }));
    await vi.advanceTimersByTimeAsync(0);

    expect(shownIds()).toEqual(storedIds());
  });

  it("drops that offer when another channel is opened", async () => {
    await mount(rows("ops", 2, OLD, "ops"));
    server.add(rows("new", 80, NEW));
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(screen.getByRole("button", { name: /show older messages/i })).toBeTruthy();

    await fireEvent.click(screen.getByRole("option", { name: "Ops" }));
    await vi.advanceTimersByTimeAsync(0);

    expect(shownIds()).toEqual(storedIds("ops"));
    expect(screen.queryByRole("button", { name: /show older messages/i })).toBeNull();
  });

  it("opens no hole and offers nothing for a short page of new messages", async () => {
    // Fewer than a page: the read holds everything the channel has.
    await mount([]);
    server.add(rows("new", 3, NEW));
    await vi.advanceTimersByTimeAsync(POLL_MS);

    expect(shownIds()).toEqual(storedIds());
    expect(screen.queryByRole("button", { name: /show older messages/i })).toBeNull();

    // The same with rows on screen that the channel no longer holds.
    server.replace(rows("later", 10, NEW));
    await vi.advanceTimersByTimeAsync(POLL_MS);

    expect(shownIds()).toHaveLength(13);
    expect(marker()).toBeNull();
    expect(pagedCalls()).toHaveLength(0);
  });
});
