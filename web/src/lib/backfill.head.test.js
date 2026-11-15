import { describe, it, expect } from "vitest";
import { createBackfill } from "./backfill.svelte.js";
import { justAfter } from "./timeline.js";
import { fakeHistory, fakeFeed, rows } from "../test-history.js";

// The controller behind the conversation feed's catch-up (ISSUE-0222),
// without a component: what it makes of one head page — whether the page
// left a hole, and what becomes of a message the operator posted and no read
// has returned yet — and the older-messages page. The paging loop and the
// holes themselves are in backfill.test.js.

const T = "2026-06-02T12:00:00Z";

function row(id, timestamp = T) {
  return { id, timestamp, channel_id: "general", sender_id: "alice", content: id };
}

const contents = (feed) => feed.list.map((m) => m.content);

// A feed showing `shown` old rows and a controller for it; `arrive` writes
// rows to the server and returns the head page a poll would then read.
function feedShowing(shown) {
  const old = rows("old", shown, "2026-06-01T09:00:00Z");
  const server = fakeHistory(old);
  const feed = fakeFeed(server, old);
  const backfill = createBackfill(feed, { pageLimit: 50 });
  const arrive = (list) => {
    server.add(list);
    return server.all("general").slice(0, 50);
  };
  return { server, feed, backfill, arrive };
}

const mine = (id, timestamp) => ({ ...row(id, timestamp), sender_id: "local" });

describe("createBackfill — what a head page leaves under it", () => {
  it("leaves nothing under a page that is not full", () => {
    const { feed, backfill, arrive } = feedShowing(3);

    expect(backfill.headRead(arrive(rows("new", 40, T)), 50)).toBeNull();

    expect(backfill.hasOlder).toBe(false);
    expect(feed.drops).toEqual([]);
  });

  it("leaves nothing under a full page that holds one row already read, and only one", () => {
    const { backfill, arrive } = feedShowing(3);

    // 49 new rows: the page's oldest row is the newest one on screen.
    const head = arrive(rows("new", 49, T));

    expect(head.at(-1).id).toBe("old-0");
    expect(backfill.headRead(head, 50)).toBeNull();
    expect(backfill.gapAbove("new-48")).toBeUndefined();
    expect(backfill.hasOlder).toBe(false);
  });

  it("offers older messages, and opens no hole, when nothing is on screen", () => {
    const { backfill, arrive } = feedShowing(0);

    expect(backfill.headRead(arrive(rows("new", 80, T)), 50)).toBeNull();

    expect(backfill.hasOlder).toBe(true);
    expect(backfill.gapAbove("new-49")).toBeUndefined();
  });

  it("takes the operator's unread posts off the screen when a full page holds nothing a read returned", () => {
    const { feed, backfill, arrive } = feedShowing(3);
    // Two posts: one the full page will hold, one it will have pushed out.
    feed.post(backfill, mine("early", "2026-06-02T11:00:00Z"));
    arrive([mine("early", "2026-06-02T11:00:00Z")]);
    arrive(rows("a", 30, "2026-06-02T11:30:00Z"));
    feed.post(backfill, mine("late", "2026-06-02T11:31:00Z"));
    arrive([mine("late", "2026-06-02T11:31:00Z")]);
    const head = arrive(rows("b", 30, T));
    expect(head.map((m) => m.id)).toContain("late");
    expect(head.map((m) => m.id)).not.toContain("early");

    const gap = backfill.headRead(head, 50);

    // Both are gone from the screen and may come back from a read…
    expect(feed.drops).toEqual([["early", "late"]]);
    expect(feed.list.map((m) => m.id)).toEqual(["old-0", "old-1", "old-2"]);
    expect(feed.isHeld("late")).toBe(false);
    // …and the page left a hole, hung on its oldest row.
    expect(gap.rowId).toBe(head.at(-1).id);
    expect(backfill.gapAbove(head.at(-1).id)).toBe(gap);
  });

  it("fills that hole down to a row a read returned, the posts in their place", async () => {
    const { server, feed, backfill, arrive } = feedShowing(3);
    arrive(rows("a", 30, "2026-06-02T11:30:00Z"));
    feed.post(backfill, mine("late", "2026-06-02T11:31:00Z"));
    arrive([mine("late", "2026-06-02T11:31:00Z")]);
    const head = arrive(rows("b", 60, T));
    const gap = backfill.headRead(head, 50);
    feed.takeHead(head);

    await backfill.fill(gap);

    expect(contents(feed)).toEqual(server.all("general").map((m) => m.content));
    expect(feed.list).toHaveLength(94);
  });

  it("opens the hole when an unread post was the only row on screen", () => {
    const { feed, backfill, arrive } = feedShowing(0);
    feed.post(backfill, mine("only", "2026-06-02T11:00:00Z"));
    arrive([mine("only", "2026-06-02T11:00:00Z")]);

    const head = arrive(rows("new", 80, T));
    const gap = backfill.headRead(head, 50);

    // The post was on screen, so the feed reads back for it; the control at
    // the top is for a conversation that showed nothing.
    expect(gap.rowId).toBe("new-49");
    expect(backfill.hasOlder).toBe(false);
    expect(feed.list).toEqual([]);
  });

  it("counts a post as read once a page returned it beside a row already read", () => {
    const { feed, backfill, arrive } = feedShowing(3);
    feed.post(backfill, mine("post", "2026-06-02T11:00:00Z"));
    arrive([mine("post", "2026-06-02T11:00:00Z")]);
    const first = arrive(rows("mid", 5, "2026-06-02T11:30:00Z"));
    expect(backfill.headRead(first, 50)).toBeNull(); // short, and holds the post
    feed.takeHead(first);

    const head = arrive(rows("new", 120, T));
    const gap = backfill.headRead(head, 50);

    // A miss, and the post stays where it is: it was read with all around it.
    expect(gap.rowId).toBe("new-49");
    expect(feed.drops).toEqual([]);
    expect(feed.list.map((m) => m.id)).toContain("post");
  });

  it("counts a post as read when a full page returned it with a row already read", () => {
    const { feed, backfill, arrive } = feedShowing(50);
    feed.post(backfill, mine("post", "2026-06-02T11:00:00Z"));
    arrive([mine("post", "2026-06-02T11:00:00Z")]);
    const first = arrive(rows("mid", 5, "2026-06-02T11:30:00Z"));
    expect(first).toHaveLength(50);
    expect(backfill.headRead(first, 50)).toBeNull();
    feed.takeHead(first);

    backfill.headRead(arrive(rows("new", 120, T)), 50);

    expect(feed.drops).toEqual([]);
  });

  it("forgets the unread posts when the conversation starts over", () => {
    const { feed, backfill, arrive } = feedShowing(3);
    feed.post(backfill, mine("post", "2026-06-02T11:00:00Z"));

    backfill.reset();
    backfill.headRead(arrive(rows("new", 120, T)), 50);

    expect(feed.drops).toEqual([]);
  });
});

describe("createBackfill — the older-messages page", () => {
  it("reads one page from just after the oldest row shown and puts it at the far end", async () => {
    const all = rows("m", 80, T);
    const server = fakeHistory(all);
    const feed = fakeFeed(server, all.slice(0, 50));
    const backfill = createBackfill(feed, { pageLimit: 50 });
    backfill.offerOlder();

    await backfill.loadOlder();

    expect(server.calls).toEqual([
      { id: "general", limit: 50, before: justAfter(all[49].timestamp) },
    ]);
    expect(feed.inserts).toEqual([
      { ids: all.slice(50).map((m) => m.id), afterId: "", keep: "rows" },
    ]);
    expect(backfill.hasOlder).toBe(false); // the page was short
    expect(backfill.loadingOlder).toBe(false);
  });

  it("drops an older page that answers after the feed moved to another channel", async () => {
    const all = rows("m", 200, T);
    const server = fakeHistory(all);
    const feed = fakeFeed(server, all.slice(0, 50));
    const backfill = createBackfill(feed, { pageLimit: 50 });
    backfill.offerOlder();
    let release;
    server.hold = () => new Promise((resolve) => (release = resolve));

    const pending = backfill.loadOlder();
    feed.version++; // the channel changed
    backfill.reset();
    release();
    await pending;

    // A full page of the channel left behind: none of it is handed over, and
    // it offers nothing in the conversation now shown.
    expect(feed.inserts).toEqual([]);
    expect(backfill.hasOlder).toBe(false);
    expect(backfill.loadingOlder).toBe(false);
  });

  it("keeps the offer after a full page, and reports a failure", async () => {
    const all = rows("m", 200, T);
    const server = fakeHistory(all);
    const feed = fakeFeed(server, all.slice(0, 50));
    const backfill = createBackfill(feed, { pageLimit: 50 });
    backfill.offerOlder();

    await backfill.loadOlder();
    expect(backfill.hasOlder).toBe(true);

    server.fail = () => new Error("backend down");
    await backfill.loadOlder();
    expect(backfill.olderError).toBe("Could not load older messages: backend down");
    expect(backfill.loadingOlder).toBe(false);
  });
});
