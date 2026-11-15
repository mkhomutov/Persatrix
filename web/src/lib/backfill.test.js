import { describe, it, expect, vi } from "vitest";
import {
  pageBack,
  createBackfill,
  CATCHUP_PAGE_LIMIT,
  MAX_CATCHUP_PAGES,
} from "./backfill.svelte.js";
import { justAfter } from "./timeline.js";
import { fakeHistory, fakeFeed, rows } from "../test-history.js";

// The paging behind the conversation feed's catch-up (ISSUE-0222), without a
// component: pageBack is the loop that reads one missing span, createBackfill
// the controller that holds the holes and the older-messages control. What
// the controller makes of a head page, and the older-messages page, are in
// backfill.head.test.js; the panel-level cases in
// panels/ChannelTimeline.catchup*.test.js.

const T = "2026-06-02T12:00:00Z";

function row(id, timestamp = T) {
  return { id, timestamp, channel_id: "general", sender_id: "alice", content: id };
}

// run drives pageBack over a list of prepared pages and records what it did.
async function run(pages, { held = [], below = [], current = () => true, ...opts } = {}) {
  const fetched = [];
  const committed = [];
  const heldIds = new Set([...held, ...below]);
  const end = await pageBack({
    before: "cursor-0",
    fetchPage: (before, limit) => {
      fetched.push({ before, limit });
      const page = pages[fetched.length - 1];
      return page instanceof Error ? Promise.reject(page) : Promise.resolve(page);
    },
    isHeld: (id) => heldIds.has(id),
    joins: (id) => below.includes(id),
    isCurrent: current,
    commit: (list, next) => {
      list.forEach((m) => heldIds.add(m.id));
      committed.push({ ids: list.map((m) => m.id), next });
    },
    ...opts,
  });
  return { end, fetched, committed };
}

// A full page of `limit` rows one second apart, newest first.
function fullPage(prefix, limit, end) {
  return rows(prefix, limit, end);
}

describe("pageBack — reading one missing span", () => {
  it("asks for large pages and a small number of them", () => {
    // Each page is a request against the console's rate budget; these two
    // numbers are what a return to a hidden tab can cost on top of its poll.
    expect(CATCHUP_PAGE_LIMIT).toBe(200);
    expect(MAX_CATCHUP_PAGES).toBe(3);
  });

  it("closes the span when a row held below it comes back", async () => {
    const { end, fetched, committed } = await run(
      [[row("n1"), row("n2"), row("old1"), row("old2")]],
      { below: ["old1", "old2"], limit: 4 },
    );

    expect(end).toBe("closed");
    expect(fetched).toEqual([{ before: "cursor-0", limit: 4 }]);
    expect(committed).toEqual([{ ids: ["n1", "n2"], next: null }]);
  });

  it("uses the default page size and budget when given none", async () => {
    const pages = [
      fullPage("a", 200, "2026-06-02T11:00:00Z"),
      fullPage("b", 200, "2026-06-02T10:00:00Z"),
      fullPage("c", 200, "2026-06-02T09:00:00Z"),
      fullPage("d", 200, "2026-06-02T08:00:00Z"),
    ];
    const { end, fetched } = await run(pages);

    expect(end).toBe("open");
    expect(fetched.map((f) => f.limit)).toEqual([200, 200, 200]);
  });

  it("keeps a new row that shares the joining row's timestamp but sorts after it", async () => {
    // The store orders rows of one instant either way round.
    const { committed } = await run(
      [[row("n1", "2026-06-02T12:00:02Z"), row("old1"), row("tie"), row("old2", "2026-06-02T11:59:00Z")]],
      { below: ["old1", "old2"], limit: 4 },
    );

    expect(committed).toEqual([{ ids: ["n1", "tie"], next: null }]);
  });

  it("leaves a new row below the join alone: it belongs to a hole further down", async () => {
    const { committed } = await run(
      [[row("n1", "2026-06-02T12:00:02Z"), row("old1"), row("deeper", "2026-06-02T11:59:30Z"), row("old2", "2026-06-02T11:59:00Z")]],
      { below: ["old1", "old2"], limit: 4 },
    );

    expect(committed).toEqual([{ ids: ["n1"], next: null }]);
  });

  it("closes the span on a short page: the channel's first message was reached", async () => {
    const { end, fetched, committed } = await run([[row("n1"), row("n2")]], {
      limit: 3,
    });

    expect(end).toBe("closed");
    expect(fetched).toHaveLength(1);
    expect(committed).toEqual([{ ids: ["n1", "n2"], next: null }]);
  });

  it("pages on from one nanosecond after the oldest row read, and stops at the budget", async () => {
    const p1 = [row("a1", "2026-06-02T12:00:09Z"), row("a2", "2026-06-02T12:00:08Z")];
    const p2 = [row("b1", "2026-06-02T12:00:07Z"), row("b2", "2026-06-02T12:00:06Z")];
    const { end, fetched, committed } = await run([p1, p2, [row("never")]], {
      limit: 2,
      maxPages: 2,
    });

    expect(end).toBe("open");
    expect(fetched).toEqual([
      { before: "cursor-0", limit: 2 },
      { before: justAfter("2026-06-02T12:00:08Z"), limit: 2 },
    ]);
    // Each page is handed over as it arrives, with the cursor for the next.
    expect(committed).toEqual([
      { ids: ["a1", "a2"], next: justAfter("2026-06-02T12:00:08Z") },
      { ids: ["b1", "b2"], next: justAfter("2026-06-02T12:00:06Z") },
    ]);
  });

  it("drops rows the feed already holds above the span, and takes their siblings once", async () => {
    // The cursor sits just after `edge`, so the page starts with it again;
    // `sib` shares its timestamp and was cut off by the page before.
    const { committed } = await run(
      [[row("edge"), row("sib"), row("n3", "2026-06-02T11:59:59Z")], [row("n3", "2026-06-02T11:59:59Z"), row("n4", "2026-06-02T11:59:58Z")]],
      { held: ["edge"], limit: 3 },
    );

    expect(committed.map((c) => c.ids)).toEqual([["sib", "n3"], ["n4"]]);
  });

  it("stops when a full page brings nothing new and no join", async () => {
    // A whole page of rows sharing one timestamp, all shown already: the
    // cursor cannot get past them, and asking again would not either.
    const page = [row("s1"), row("s2"), row("s3")];
    const { end, fetched, committed } = await run([page, page, page], {
      held: ["s1", "s2", "s3"],
      limit: 3,
    });

    expect(end).toBe("stalled");
    expect(fetched).toHaveLength(1);
    expect(committed).toEqual([]);
  });

  it("rejects on a failed page after handing over the pages before it", async () => {
    const boom = new Error("rate limit exceeded");
    const fetched = [];
    const committed = [];
    const pending = pageBack({
      before: "cursor-0",
      fetchPage: (before) => {
        fetched.push(before);
        return fetched.length === 1
          ? Promise.resolve([row("a1", "2026-06-02T12:00:09Z"), row("a2", "2026-06-02T12:00:08Z")])
          : Promise.reject(boom);
      },
      isHeld: () => false,
      joins: () => false,
      isCurrent: () => true,
      commit: (list) => committed.push(list.map((m) => m.id)),
      limit: 2,
    });

    await expect(pending).rejects.toBe(boom);
    expect(committed).toEqual([["a1", "a2"]]);
    expect(fetched).toHaveLength(2); // and it did not try a third time
  });

  it("hands nothing over when the feed moved on while a page was out", async () => {
    let current = true;
    const committed = [];
    const end = await pageBack({
      before: "cursor-0",
      fetchPage: () => {
        current = false; // the channel changes while this read is in flight
        return Promise.resolve([row("n1")]);
      },
      isHeld: () => false,
      joins: () => false,
      isCurrent: () => current,
      commit: (list) => committed.push(list),
      limit: 1,
    });

    expect(end).toBe("stale");
    expect(committed).toEqual([]);
  });

  it("reads no further page once the feed has moved on", async () => {
    let current = true;
    const fetchPage = vi.fn(() => Promise.resolve([row(`n${fetchPage.mock.calls.length}`, `2026-06-02T12:00:0${9 - fetchPage.mock.calls.length}Z`)]));
    const end = await pageBack({
      before: "cursor-0",
      fetchPage,
      isHeld: () => false,
      joins: () => false,
      isCurrent: () => current,
      commit: () => {
        current = false; // the channel changes while the rows are being drawn
      },
      limit: 1,
    });

    expect(end).toBe("stale");
    expect(fetchPage).toHaveBeenCalledTimes(1);
  });
});

// A feed showing three old rows, a server that has since taken `count` new
// ones, and the hole a full head page of new rows leaves.
function holeAfter(count) {
  const old = rows("old", 3, "2026-06-01T09:00:00Z");
  const server = fakeHistory(old);
  const feed = fakeFeed(server, old);
  const backfill = createBackfill(feed, { pageLimit: 50 });
  server.add(rows("new", count, T));
  const head = server.all("general").slice(0, 50);
  const gap = backfill.headRead(head, 50);
  feed.takeHead(head);
  server.calls.length = 0;
  return { server, feed, backfill, gap, head };
}

const contents = (feed) => feed.list.map((m) => m.content);

describe("createBackfill — the holes in a timeline", () => {
  it("hangs a new hole on the oldest row of the head page that opened it", () => {
    const { backfill, gap, head } = holeAfter(120);

    expect(backfill.gapAbove(head.at(-1).id)).toBe(gap);
    expect(backfill.gapAbove(head[0].id)).toBeUndefined();
    expect(gap.before).toBe(justAfter(head.at(-1).timestamp));
    expect(gap.loading).toBe(false);
    expect(gap.error).toBe("");
  });

  it("fills a hole from the newer side and removes its marker once the span is in", async () => {
    const { server, feed, backfill, gap } = holeAfter(120);

    await backfill.fill(gap);

    expect(contents(feed)).toEqual(server.all("general").map((m) => m.content));
    expect(feed.inserts).toEqual([
      { ids: rows("new", 120, T).slice(50).map((m) => m.id), afterId: "new-49", keep: "" },
    ]);
    expect(backfill.gapAbove("new-119")).toBeUndefined();
    expect(backfill.gapAbove("new-49")).toBeUndefined();
  });

  it("moves the marker up to the oldest row read when the budget runs out", async () => {
    const { server, feed, backfill, gap } = holeAfter(690);

    await backfill.fill(gap);

    expect(server.calls).toHaveLength(MAX_CATCHUP_PAGES);
    expect(feed.list).toHaveLength(3 + 50 + 597);
    expect(backfill.gapAbove("new-646")).toBe(gap);
    expect(backfill.gapAbove("new-49")).toBeUndefined();
    expect(gap.before).toBe(justAfter(server.all("general")[646].timestamp));
    expect(gap.loading).toBe(false);
    // Each page went in under the last row of the one before.
    expect(feed.inserts.map((i) => i.afterId)).toEqual(["new-49", "new-248", "new-447"]);
  });

  it("keeps the reader's place when the hole is filled by a click", async () => {
    const { feed, backfill, gap } = holeAfter(120);

    await backfill.fill(gap, { byClick: true });

    expect(feed.inserts.map((i) => i.keep)).toEqual(["top"]);
  });

  it("does not treat a held row above the hole as its far side", async () => {
    // The page starts with the row the hole hangs on (the cursor sits just
    // after it); that row is shown, but it is not below the hole.
    const { server, feed, backfill, gap } = holeAfter(300);

    await backfill.fill(gap);

    expect(server.calls).toHaveLength(2);
    expect(contents(feed)).toEqual(server.all("general").map((m) => m.content));
  });

  it("passes a failed read on to the poll, and keeps the hole where the reading got to", async () => {
    const { server, feed, backfill, gap } = holeAfter(690);
    const refused = Object.assign(new Error("rate limit exceeded"), { status: 429 });
    server.fail = () => (server.calls.length === 2 ? refused : null);

    await expect(backfill.fill(gap)).rejects.toBe(refused);

    expect(feed.list).toHaveLength(3 + 50 + 199);
    expect(backfill.gapAbove("new-248")).toBe(gap);
    expect(gap.loading).toBe(false);
    expect(gap.error).toBe(""); // the poll shows its own notice
  });

  it("shows a failed click beside the marker and does not reject", async () => {
    const { server, backfill, gap } = holeAfter(690);
    server.fail = () => new Error("backend down");

    await backfill.fill(gap, { byClick: true });

    expect(gap.error).toBe("Could not load the missing messages: backend down");
    expect(gap.loading).toBe(false);
    expect(backfill.gapAbove("new-49")).toBe(gap);

    // The next attempt starts clean.
    server.fail = null;
    await backfill.fill(gap, { byClick: true });
    expect(gap.error).toBe("");
  });

  it("reads a hole once at a time", async () => {
    const { server, backfill, gap } = holeAfter(120);
    let release;
    server.hold = () => new Promise((resolve) => (release = resolve));

    const first = backfill.fill(gap);
    expect(gap.loading).toBe(true);
    await backfill.fill(gap, { byClick: true }); // a click while it loads
    expect(server.calls).toHaveLength(1);

    release();
    await first;
    expect(server.calls).toHaveLength(1);
  });

  it.each([
    ["the first written listed first, as the store lists them", false],
    ["the last written listed first", true],
  ])("says so, and keeps the marker, when a page of one timestamp cannot be passed (%s)", async (_, tiesLastWrittenFirst) => {
    const old = rows("old", 3, "2026-06-01T09:00:00Z");
    const server = fakeHistory(old, { tiesLastWrittenFirst });
    const feed = fakeFeed(server, old);
    const backfill = createBackfill(feed, { pageLimit: 50 });
    // 250 rows written in one instant: the head page takes 50 of them, the
    // first catch-up page 200 — every one of them the same timestamp.
    const burst = rows("new", 250, T).map((m) => ({ ...m, timestamp: T }));
    server.add(burst);
    const head = server.all("general").slice(0, 50);
    const gap = backfill.headRead(head, 50);
    feed.takeHead(head);
    server.calls.length = 0;

    await backfill.fill(gap); // 150 new rows, then the same page again
    expect(server.calls).toHaveLength(2);
    expect(gap.error).toBe(
      "Could not load the missing messages: the server returned only messages already shown.",
    );
    expect(backfill.gapAbove(gap.rowId)).toBe(gap);
  });

  it("drops a read that answers after the feed moved to another channel", async () => {
    const { server, feed, backfill, gap } = holeAfter(690);
    let release;
    server.hold = () => new Promise((resolve) => (release = resolve));

    const pending = backfill.fill(gap);
    feed.version++; // the channel changed
    backfill.reset();
    release();
    await pending;

    expect(feed.inserts).toEqual([]);
    expect(server.calls).toHaveLength(1);
  });

  it("swallows a failure that answers after the feed moved to another channel", async () => {
    const { server, feed, backfill, gap } = holeAfter(690);
    let fail;
    server.fail = () => null;
    server.hold = () => new Promise((_, reject) => (fail = reject));

    const pending = backfill.fill(gap);
    feed.version++;
    backfill.reset();
    fail(new Error("backend down"));

    await expect(pending).resolves.toBeUndefined();
  });

  it("forgets every hole when the conversation starts over", () => {
    const { backfill, head } = holeAfter(120);

    backfill.offerOlder();
    backfill.reset();

    expect(backfill.gapAbove(head.at(-1).id)).toBeUndefined();
    expect(backfill.hasOlder).toBe(false);
  });
});
