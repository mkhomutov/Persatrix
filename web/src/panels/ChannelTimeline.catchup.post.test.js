import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/svelte";
import ChannelTimeline from "./ChannelTimeline.svelte";

// The operator's own post and the catch-up (ISSUE-0222). A message the
// operator posts is drawn the moment the server stores it, without a read.
// So it says nothing about the messages under it: another member may have
// written between the feed's last read and the post. A row like that must not
// pass for "a message already shown" when the feed decides whether a head
// page left a hole, or where a hole ends — or the messages written just
// before the post are lost with nothing on screen to say so.
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
  publishMessage,
  ApiError,
} from "../lib/api.js";
import { selection } from "../lib/selection.svelte.js";
import { fakeHistory, rows } from "../test-history.js";

const POLL_MS = 3000;
const OLD = "2026-06-01T09:00:00Z";

let server;

// The rendered rows, oldest first, by their text.
const shownIds = () =>
  screen
    .queryAllByRole("listitem")
    .map((li) => li.querySelector(".content").textContent);

// What the stand-in holds, oldest first: what a complete timeline shows.
const storedIds = () =>
  server
    .all("general")
    .map((m) => m.content)
    .reverse();

const marker = () =>
  screen.queryByRole("button", { name: "Load missing messages" });
const older = () =>
  screen.queryByRole("button", { name: /show older messages/i });
const pagedCalls = () => server.calls.filter((c) => c.before);

async function mount(initial) {
  server = fakeHistory(initial);
  getChannelHistory.mockImplementation(server.read);
  render(ChannelTimeline, { props: { userId: "local" } });
  await vi.advanceTimersByTimeAsync(0);
  expect(shownIds()).toEqual(storedIds());
}

// The operator posts `content`: the stand-in stores the row and answers with
// it, as POST /api/v1/channels/{id}/messages does, and the panel draws it.
async function post(at, content = "mine") {
  publishMessage.mockImplementation(async (channel) => {
    const row = {
      id: `post-${content}`,
      channel_id: channel,
      sender_id: "local",
      content,
      timestamp: at,
      mentions: [],
    };
    server.add([row]);
    return row;
  });
  await fireEvent.input(screen.getByRole("textbox", { name: /message/i }), {
    target: { value: content },
  });
  await fireEvent.click(screen.getByRole("button", { name: /post/i }));
  await vi.advanceTimersByTimeAsync(0);
  expect(shownIds().at(-1)).toBe(content);
}

beforeEach(() => {
  vi.useFakeTimers();
  listAgents.mockResolvedValue([]);
  listChannels.mockResolvedValue({
    channels: [{ id: "general", name: "General", channel_type: "group" }],
  });
  getChannelActivity.mockImplementation(() => Promise.resolve({ thinking: [] }));
  selection.dmAgent = "";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("Channels panel — the operator's own post and the catch-up", () => {
  it("shows the message written just before a post, after a hidden tab filled past one page", async () => {
    const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(false);
    await mount(rows("old", 3, OLD));
    // Another member writes; before the next poll reads it, the operator
    // posts and switches to another tab.
    server.add(rows("just before", 1, "2026-06-02T11:59:01Z"));
    await post("2026-06-02T11:59:02Z");
    hidden.mockReturnValue(true);
    document.dispatchEvent(new Event("visibilitychange"));
    server.add(rows("new", 120, "2026-06-02T12:30:00Z"));
    await vi.advanceTimersByTimeAsync(10 * POLL_MS);

    hidden.mockReturnValue(false);
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);

    // All 125, in the store's order: the post between the message written
    // before it and the ones written after.
    expect(shownIds()).toEqual(storedIds());
    expect(shownIds()).toHaveLength(125);
    expect(marker()).toBeNull();
    expect(older()).toBeNull();
  });

  it("shows the messages under a post that sits in a full page of unread ones", async () => {
    await mount(rows("old", 3, OLD));
    // One poll interval: 30 messages, the operator's post, 30 more. The head
    // page holds the post and nothing a read has returned.
    server.add(rows("a", 30, "2026-06-02T11:59:01Z"));
    await post("2026-06-02T11:59:02Z");
    server.add(rows("b", 30, "2026-06-02T11:59:40Z"));

    await vi.advanceTimersByTimeAsync(POLL_MS);

    expect(shownIds()).toEqual(storedIds());
    expect(shownIds()).toHaveLength(64);
    expect(marker()).toBeNull();
    // One page: the 11 rows the head page cut off, and the join.
    expect(pagedCalls()).toHaveLength(1);
  });

  it("shows the messages under a post that a full page of unread ones pushed out", async () => {
    await mount(rows("old", 3, OLD));
    server.add(rows("a", 30, "2026-06-02T11:58:01Z"));
    await post("2026-06-02T11:58:02Z");
    server.add(rows("b", 60, "2026-06-02T11:59:40Z"));

    await vi.advanceTimersByTimeAsync(POLL_MS);

    expect(shownIds()).toEqual(storedIds());
    expect(shownIds()).toHaveLength(94);
    expect(marker()).toBeNull();
  });

  it("marks the hole when the page that carries the post fails, and a click loads all of it", async () => {
    await mount(rows("old", 3, OLD));
    server.add(rows("a", 30, "2026-06-02T11:58:01Z"));
    await post("2026-06-02T11:58:02Z");
    server.add(rows("b", 60, "2026-06-02T11:59:40Z"));
    server.fail = (call) => (call.before ? new ApiError("backend down", 503) : null);

    await vi.advanceTimersByTimeAsync(POLL_MS);

    // Nothing is lost without a word: what is not on screen is behind the
    // marker. The post is among it until a read returns it.
    expect(marker()).not.toBeNull();
    expect(shownIds().slice(0, 3)).toEqual(storedIds().slice(0, 3));
    expect(shownIds().slice(3)).toEqual(storedIds().slice(-50));

    server.fail = null;
    await fireEvent.click(marker());
    await vi.advanceTimersByTimeAsync(0);

    expect(shownIds()).toEqual(storedIds());
    expect(shownIds()).toHaveLength(94);
    expect(marker()).toBeNull();
  });

  it("reads back to the channel's first message when only the post was on screen", async () => {
    // An empty conversation: five messages nobody has read yet, the
    // operator's post, then more than a page.
    await mount([]);
    server.add(rows("a", 5, "2026-06-02T11:58:01Z"));
    await post("2026-06-02T11:58:02Z");
    server.add(rows("b", 60, "2026-06-02T11:59:40Z"));

    await vi.advanceTimersByTimeAsync(POLL_MS);

    expect(shownIds()).toEqual(storedIds());
    expect(shownIds()).toHaveLength(66);
    expect(marker()).toBeNull();
    expect(older()).toBeNull();
  });

  it("counts a post as read once a head page returned it with the rows around it", async () => {
    await mount(rows("old", 3, OLD));
    await post("2026-06-02T11:00:00Z");
    // The next poll's page is short and holds the post: nothing lies unread
    // under it, and from now on it is a message like any other.
    server.add(rows("mid", 5, "2026-06-02T11:30:00Z"));
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(shownIds()).toEqual(storedIds());
    expect(pagedCalls()).toHaveLength(0);

    // More than a page arrives: the catch-up stops at the newest message it
    // holds, above the post, and the post stays where it is.
    server.add(rows("new", 120, "2026-06-02T12:30:00Z"));
    await vi.advanceTimersByTimeAsync(POLL_MS);

    expect(shownIds()).toEqual(storedIds());
    expect(shownIds()).toHaveLength(129);
    expect(marker()).toBeNull();
  });

  it("leaves a post alone once a catch-up page returned it", async () => {
    await mount(rows("old", 3, OLD));
    server.add(rows("a", 30, "2026-06-02T11:58:01Z"));
    await post("2026-06-02T11:58:02Z");
    server.add(rows("b", 60, "2026-06-02T11:59:40Z"));
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(shownIds()).toEqual(storedIds());

    // A second burst: the post was read with the rows around it, so it is
    // not taken off the screen again — nothing would bring it back.
    server.add(rows("c", 120, "2026-06-02T12:30:00Z"));
    await vi.advanceTimersByTimeAsync(POLL_MS);

    expect(shownIds()).toEqual(storedIds());
    expect(shownIds()).toHaveLength(214);
    expect(marker()).toBeNull();
  });

  it("brings back a post that landed while a full page of unread messages was being read", async () => {
    await mount(rows("old", 3, OLD));
    server.add(rows("new", 60, "2026-06-02T11:59:00Z"));
    // The head read is asked before the post is stored and answers after it.
    let answer;
    server.hold = (call) =>
      call.before ? null : new Promise((resolve) => (answer = resolve));
    await vi.advanceTimersByTimeAsync(POLL_MS);
    server.hold = null;
    await post("2026-06-02T12:00:00Z");

    answer();
    await vi.advanceTimersByTimeAsync(0);
    // That page was read before the post existed, so nothing ties the post
    // to it; the next poll returns the post, at the newest end.
    await vi.advanceTimersByTimeAsync(POLL_MS);

    expect(shownIds()).toEqual(storedIds());
    expect(shownIds()).toHaveLength(64);
    expect(shownIds().at(-1)).toBe("mine");
    expect(marker()).toBeNull();
  });
});
