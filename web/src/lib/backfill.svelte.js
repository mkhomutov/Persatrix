// Reading back into a channel's history, for the conversation feed: the page
// above the oldest message shown ("Show older messages"), and the spans the
// feed missed when more than one page of messages arrived between two polls
// (ISSUE-0222). Split out of ConversationFeed.svelte, which keeps the rows,
// the poll loop, the scrolling and the markup.
//
// A `.svelte.js` module so the state the markup reads — the older-messages
// control and the gap markers — is reactive.
import { justAfter } from "./timeline.js";

// One catch-up page, and how many of them one run may read. Each page is one
// request against the console's rate budget (60 calls per 60 seconds by
// default, shared by every console window while sign-in is off), so the pages
// are large and few: three of them bring up to 597 messages (each page reads
// again the row its cursor stands on), on top of the 50 the head poll brought.
// The server honours a `limit` up to 1000
// (channelMaxLimit, internal/server/channel_handlers.go); were that cap ever
// lowered below this page size, every page would look like the channel's last.
export const CATCHUP_PAGE_LIMIT = 200;
export const MAX_CATCHUP_PAGES = 3;

// pageBack reads one missing span, newest page first, starting at the history
// cursor `before`, and hands each page's new rows to `commit` as it arrives —
// so a page that fails later loses nothing already read. It ends with:
//   "closed"  — a row the feed holds below the span came back (`joins`), or
//               the page was short (the channel's first message): the span is
//               loaded;
//   "open"    — the page budget is spent and the span is not finished;
//   "stalled" — a full page brought nothing new and no join: one timestamp
//               holds a whole page or more, and no cursor gets past it;
//   "stale"   — the feed moved to another channel meanwhile (`isCurrent`).
// A failed read rejects; the rows of earlier pages have been committed.
//
// `commit(rows, next)` gets the page's new rows, newest first, and the cursor
// the span's next page starts from — null once the span is closed.
//
// The next cursor is one nanosecond after the oldest row of the page just
// read (`justAfter`): the endpoint's bound is strict, so rows sharing that
// timestamp come back again and are dropped by id (`isHeld`), not lost. Rows
// sharing the joining row's timestamp can sort either side of it, so those
// are kept too. Every other row after the join lies below rows the feed
// already holds and belongs to whatever hole is there, not to this one.
export async function pageBack({
  fetchPage,
  before,
  isHeld,
  joins,
  isCurrent,
  commit,
  limit = CATCHUP_PAGE_LIMIT,
  maxPages = MAX_CATCHUP_PAGES,
}) {
  let cursor = before;
  for (let page = 0; page < maxPages; page++) {
    if (!isCurrent()) return "stale";
    const rows = await fetchPage(cursor, limit);
    if (!isCurrent()) return "stale";
    const joinAt = rows.findIndex((m) => joins(m.id));
    const span =
      joinAt === -1
        ? rows
        : rows.filter(
            (m, i) => i < joinAt || m.timestamp === rows[joinAt].timestamp,
          );
    const fresh = span.filter((m) => !isHeld(m.id));
    if (joinAt !== -1 || rows.length < limit) {
      await commit(fresh, null);
      return "closed";
    }
    if (fresh.length === 0) return "stalled";
    cursor = justAfter(rows.at(-1).timestamp);
    await commit(fresh, cursor);
  }
  return "open";
}

// createBackfill builds the controller one feed owns. `feed` is what it needs
// from the component:
//   fetchPage(before, limit) — one history page of the channel now shown,
//       newest first; rejects as the API client does;
//   rows()     — the rows the feed holds, newest first;
//   isHeld(id) — whether the feed already shows that row;
//   drop(ids)  — takes rows off the screen and forgets them, so that a read
//       can bring them back in their place;
//   token()    — a value that changes when the feed moves to another channel;
//   insert(rows, { afterId, keep }) — puts rows read back from history in
//       place and resolves once they are drawn (ConversationFeed's `insert`).
// `pageLimit` is the size of an older-messages page.
export function createBackfill(feed, { pageLimit }) {
  // Paging backwards: a full first page means older rows may exist; a short
  // page (first or older) means the channel's start has been reached.
  let hasOlder = $state(false);
  let loadingOlder = $state(false);
  let olderError = $state("");
  // The holes in the timeline. Each hangs on the row just above it in wire
  // order — `rowId`, the oldest row of the newer block — and the feed draws
  // its marker over that row. `before` is the cursor its next page starts at.
  let gaps = $state([]);
  const gapByRow = $derived(new Map(gaps.map((g) => [g.rowId, g])));
  // Rows on screen that no read has returned: the operator's own posts, drawn
  // the moment the server stores them. Such a row says nothing about the rows
  // under it — another member may have written between the feed's last read
  // and the post — so it never passes for a row that ties a page to what is
  // already loaded (headRead below).
  let unread = new Set();

  // loadOlder extends the timeline backwards one page, from just after the
  // oldest shown message's timestamp (justAfter — so rows sharing it are not
  // skipped; the ones already shown are dropped by id), and keeps the reader's
  // place: the rows already on screen stay where they were. A full page that
  // brings nothing new (a run of rows sharing one instant) ends the paging
  // rather than offering the same page forever.
  async function loadOlder() {
    const token = feed.token();
    const oldest = feed.rows().at(-1);
    if (!oldest || loadingOlder) {
      return;
    }
    loadingOlder = true;
    olderError = "";
    try {
      const older = await feed.fetchPage(justAfter(oldest.timestamp), pageLimit);
      if (token !== feed.token()) return;
      const unseenOlder = older.filter((m) => !feed.isHeld(m.id));
      hasOlder = older.length >= pageLimit && unseenOlder.length > 0;
      await feed.insert(unseenOlder, { keep: "rows" });
    } catch (err) {
      if (token !== feed.token()) return;
      olderError = `Could not load older messages: ${err.message}`;
    } finally {
      if (token === feed.token()) loadingOlder = false;
    }
  }

  // fill reads one hole (pageBack above), moving its marker up as each page
  // lands and removing it once the span is loaded. The poll calls it for a
  // hole it has just opened and gets a failed read back as a rejection, so
  // the loop backs off exactly as for a failed head read; the hole keeps its
  // marker and is not read again until someone clicks it. The marker's button
  // calls it with `byClick`: the reader's place is kept, and a failure is
  // shown beside the marker and leaves the poll loop alone.
  async function fill(gap, { byClick = false } = {}) {
    if (gap.loading) {
      return;
    }
    const token = feed.token();
    const held = feed.rows();
    const below = new Set(
      held.slice(held.findIndex((m) => m.id === gap.rowId) + 1).map((m) => m.id),
    );
    gap.loading = true;
    gap.error = "";
    try {
      const end = await pageBack({
        before: gap.before,
        fetchPage: feed.fetchPage,
        isHeld: feed.isHeld,
        joins: (id) => below.has(id),
        isCurrent: () => token === feed.token(),
        commit: (rows, next) => {
          const afterId = gap.rowId;
          if (rows.length > 0) gap.rowId = rows.at(-1).id;
          if (next) gap.before = next;
          else gaps = gaps.filter((g) => g.rowId !== gap.rowId);
          return feed.insert(rows, { afterId, keep: byClick ? "top" : "" });
        },
      });
      if (end === "stalled") {
        gap.error =
          "Could not load the missing messages: the server returned only messages already shown.";
      }
    } catch (err) {
      if (token !== feed.token()) return;
      if (!byClick) throw err;
      gap.error = `Could not load the missing messages: ${err.message}`;
    } finally {
      gap.loading = false;
    }
  }

  return {
    get hasOlder() {
      return hasOlder;
    },
    get loadingOlder() {
      return loadingOlder;
    },
    get olderError() {
      return olderError;
    },
    // reset starts a conversation over: no holes, and no older rows on offer
    // until its first page turns out to be full.
    reset() {
      hasOlder = false;
      loadingOlder = false;
      olderError = "";
      gaps = [];
      unread = new Set();
    },
    // offerOlder: rows older than the oldest one shown may exist — a first
    // page was full.
    offerOlder() {
      hasOlder = true;
    },
    // shownUnread: the feed drew this row without reading it (its `echo`).
    shownUnread(id) {
      unread.add(id);
    },
    // headRead looks at one head page of `limit` rows before the feed sorts
    // it into new and known rows, and returns the hole the page leaves under
    // it, for fill() — or null. A full page with no row a read has returned
    // means more than a page arrived since the last read (ISSUE-0222). The
    // operator's unread posts are then taken off the screen: they prove
    // nothing about the rows around them, and the page or the reads below it
    // return them in their place. With rows on screen that leaves a hole
    // under the page; with none, older rows exist after all, and the control
    // at the top pages back to them. Any other page reaches a row a read
    // returned, or the channel's first message, so a post on it has been
    // read with everything around it.
    headRead(head, limit) {
      const read = (m) => feed.isHeld(m.id) && !unread.has(m.id);
      if (head.length < limit || head.some(read)) {
        head.forEach((m) => unread.delete(m.id));
        return null;
      }
      const shown = feed.rows().length > 0;
      if (unread.size > 0) feed.drop(unread);
      unread = new Set();
      if (!shown) {
        hasOlder = true;
        return null;
      }
      const row = head.at(-1);
      gaps.push({
        rowId: row.id,
        before: justAfter(row.timestamp),
        loading: false,
        error: "",
      });
      return gaps[gaps.length - 1];
    },
    // gapAbove is the hole drawn over a row, if there is one.
    gapAbove: (rowId) => gapByRow.get(rowId),
    loadOlder,
    fill,
  };
}
