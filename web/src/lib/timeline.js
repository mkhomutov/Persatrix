// The conversation timeline's grouping rules, as pure functions over the
// messages in display order (oldest first), and its paging cursor — split out
// of ConversationFeed so the feed keeps the loading, polling and scrolling,
// and these stay testable on their own.
import { dayKey, formatDayLabel } from "./format.js";

// dayLabelAt is the day divider's label for row i: set on the first message of
// each local calendar day, "" otherwise. An unparseable timestamp never starts
// a day.
export function dayLabelAt(list, i, now = new Date()) {
  const key = dayKey(list[i].timestamp);
  if (!key || (i > 0 && dayKey(list[i - 1].timestamp) === key)) return "";
  return formatDayLabel(list[i].timestamp, now);
}

// A consecutive same-sender message inside this window renders compact (no
// avatar/head) so a run of turns reads as one visual block. Unparseable
// timestamps disable grouping for that pair rather than guessing, and a new
// day always starts a full row under its divider.
const COMPACT_WINDOW_MS = 5 * 60 * 1000;

export function isCompact(list, i) {
  if (i === 0) return false;
  const prev = list[i - 1];
  const cur = list[i];
  if (prev.sender_id !== cur.sender_id) return false;
  const a = new Date(prev.timestamp).getTime();
  const b = new Date(cur.timestamp).getTime();
  if (Number.isNaN(a) || Number.isNaN(b)) return false;
  if (dayKey(prev.timestamp) !== dayKey(cur.timestamp)) return false;
  return b - a < COMPACT_WINDOW_MS;
}

// justAfter is the history cursor for paging back from the oldest shown
// message: the instant one nanosecond after its timestamp. The endpoint's
// `before` bound is strict (`timestamp < before`), so a cursor AT the oldest
// row would skip any row sharing its timestamp that the previous page cut off;
// one nanosecond later returns those rows too, and the feed drops the ones it
// already shows. The offset is kept; an unparseable value comes back as it was.
const RFC3339 = /^(.*T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$/;

export function justAfter(ts) {
  const m = RFC3339.exec(ts ?? "");
  if (!m) return ts;
  const [, seconds, fraction = "", offset] = m;
  const nanos = Number(fraction.padEnd(9, "0")) + 1;
  if (nanos < 1e9) {
    return `${seconds}.${String(nanos).padStart(9, "0")}${offset}`;
  }
  // .999999999 rolls over into the next whole second.
  return new Date(Date.parse(`${seconds}${offset}`) + 1000).toISOString();
}
