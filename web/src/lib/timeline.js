// The conversation timeline's grouping rules, as pure functions over the
// messages in display order (oldest first) — split out of ConversationFeed so
// the feed keeps the loading, polling and scrolling, and these stay testable
// on their own.
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
