// Shared display-formatting helpers for the console panels. These are pure
// functions (no component state) extracted so Chat and ChannelTimeline render
// timestamps and sender/channel labels identically without duplicating the
// logic — and so the formatting can be unit-tested directly.

// formatTimestamp renders a wire timestamp (RFC-3339 UTC) as a readable local
// date-time. The <time> element keeps the raw value in its machine-readable
// `datetime` attribute, so the human-facing text can be friendly without losing
// the parseable original. An unparseable value falls back to the raw string
// rather than rendering "Invalid Date".
export function formatTimestamp(ts) {
  const date = new Date(ts);
  return Number.isNaN(date.getTime()) ? ts : date.toLocaleString();
}

// channelLabel is the channel picker's display text: the channel's name,
// falling back to its id (DMs/threads have no name — channel_types.go).
export function channelLabel(channel) {
  return channel.name ? channel.name : channel.id;
}

// senderLabel turns a raw sender_id into a readable name. The operator's own
// posts read as "You" (the human/agent distinction §D asks for); an agent
// resolves to "name — role" via the best-effort agent map; anything unknown
// falls back to the raw id rather than inventing a label.
export function senderLabel(senderId, userId, agentsById) {
  if (senderId === userId) {
    return "You";
  }
  const agent = agentsById[senderId];
  if (!agent) {
    return senderId;
  }
  const name = agent.name || agent.id;
  return agent.role ? `${name} — ${agent.role}` : name;
}
