// A stand-in for GET /api/v1/channels/{id}/messages, for the tests that page
// through a channel's history. It answers as the store does
// (internal/channels/sqlite_messages_read.go): newest first, at most `limit`
// rows (50 without one), and only rows strictly older than `before`.
// Timestamps are compared to the nanosecond, because the feed's cursors sit
// one nanosecond after a row's timestamp (lib/timeline.js `justAfter`).
// Rows that share a timestamp come in the order they were written, the one
// written first listed first: the store's query orders by timestamp alone,
// and its index hands equal timestamps back in insertion order. Nothing in
// the query promises that order, so the tie cases also run it reversed.
// Imported by tests only; no page of the console loads it.

const STAMP = /^(.*T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$/;

// nanos is an RFC 3339 timestamp as nanoseconds since the epoch.
export function nanos(ts) {
  const [, seconds, fraction = "", offset] = STAMP.exec(ts);
  return (
    BigInt(Date.parse(`${seconds}${offset}`)) * 1000000n +
    BigInt(fraction.padEnd(9, "0"))
  );
}

// rows builds `count` messages one second apart, newest first, the newest at
// `end`. Row i reads "<prefix> <i>", so a test can name a row by how far it is
// from the newest.
export function rows(prefix, count, end, channel = "general") {
  return Array.from({ length: count }, (_, i) => ({
    id: `${prefix}-${i}`,
    channel_id: channel,
    sender_id: "alice",
    content: `${prefix} ${i}`,
    timestamp: new Date(Date.parse(end) - i * 1000).toISOString(),
    mentions: [],
  }));
}

// fakeHistory holds messages for any number of channels and records every
// read. Among rows sharing a timestamp, the one written first is listed
// first, as the store lists them; `{ tiesLastWrittenFirst: true }` turns
// that round.
//   add(list) / replace(list) — write rows (a newest-first list, as `rows`
//       builds it, so its last row is written first); replace drops
//       everything held first.
//   all(channel)  — the channel's rows, newest first.
//   calls         — every read, as { id, limit, before }.
//   fail(call)    — when set and it returns an error, that read rejects with it.
//   hold(call)    — when set and it returns a promise, that read answers only
//       once the promise resolves (with the rows as they were when asked).
export function fakeHistory(initial = [], { tiesLastWrittenFirst = false } = {}) {
  let stored = [];
  let written = 0;
  const put = (list) => {
    for (let i = list.length - 1; i >= 0; i--) {
      stored.push({ row: list[i], order: written++ });
    }
  };
  const newestFirst = (channel) =>
    stored
      .filter((e) => e.row.channel_id === channel)
      .sort((a, b) => {
        const at = nanos(a.row.timestamp);
        const bt = nanos(b.row.timestamp);
        if (at !== bt) return at < bt ? 1 : -1;
        return tiesLastWrittenFirst ? b.order - a.order : a.order - b.order;
      })
      .map((e) => e.row);
  put(initial);

  const server = {
    calls: [],
    fail: null,
    hold: null,
    add: put,
    replace(list) {
      stored = [];
      put(list);
    },
    all: newestFirst,
    read(id, { limit = 50, before } = {}) {
      const call = { id, limit, before };
      server.calls.push(call);
      const failure = server.fail?.(call);
      if (failure) {
        return Promise.reject(failure);
      }
      const cut = before ? nanos(before) : null;
      const answer = {
        messages: newestFirst(id)
          .filter((m) => cut === null || nanos(m.timestamp) < cut)
          .slice(0, limit),
      };
      const held = server.hold?.(call);
      return held ? held.then(() => answer) : Promise.resolve(answer);
    },
  };
  return server;
}

// fakeFeed is what lib/backfill.svelte.js needs from the conversation feed,
// for the tests that drive that controller without a component: the rows
// shown (newest first), read from a fakeHistory `server`. It records what the
// controller did to it in `inserts` and `drops`.
export function fakeFeed(server, shown) {
  const feed = {
    list: [...shown],
    seen: new Set(shown.map((m) => m.id)),
    version: 0,
    inserts: [],
    drops: [],
    fetchPage: (before, limit) =>
      server.read("general", { limit, before }).then((r) => r.messages),
    rows: () => feed.list,
    isHeld: (id) => feed.seen.has(id),
    drop: (ids) => {
      feed.drops.push([...ids]);
      ids.forEach((id) => feed.seen.delete(id));
      feed.list = feed.list.filter((m) => feed.seen.has(m.id));
    },
    token: () => feed.version,
    insert: (list, { afterId = "", keep = "" } = {}) => {
      feed.inserts.push({ ids: list.map((m) => m.id), afterId, keep });
      list.forEach((m) => feed.seen.add(m.id));
      const at = afterId ? feed.list.findIndex((m) => m.id === afterId) + 1 : feed.list.length;
      feed.list = [...feed.list.slice(0, at), ...list, ...feed.list.slice(at)];
      return Promise.resolve();
    },
    // What the feed does with a head page: its new rows go on top.
    takeHead(head) {
      const fresh = head.filter((m) => !feed.seen.has(m.id));
      fresh.forEach((m) => feed.seen.add(m.id));
      feed.list = [...fresh, ...feed.list];
    },
    // What the feed does when the operator posts: the stored row is drawn at
    // once, and the controller is told that no read returned it.
    post(backfill, m) {
      feed.seen.add(m.id);
      feed.list = [m, ...feed.list];
      backfill.shownUnread(m.id);
    },
  };
  return feed;
}
