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

// isDMChannel marks a `dm:` channel so the consolidated Channels panel can keep
// DMs OUT of the group-channel picker — a DM is reached through the persona
// entry point (which resolves it as a conversation), never as a raw
// `dm:user:agent` row (RFC 0048 chat-panel-retirement amendment §B). The server
// DTO carries `channel_type` ("group" | "dm" | "thread"); the id-prefix check is
// a belt-and-suspenders fallback for a payload that omits the type, since the
// canonical DM id always starts with `dm:` (channels.CanonicalDMID).
export function isDMChannel(channel) {
  return channel?.channel_type === "dm" || (channel?.id ?? "").startsWith("dm:");
}

// hueForId hashes an id (djb2) onto a stable 0–359 hue, so a participant keeps
// one avatar colour across messages, conversations, and reloads. Pure display
// decoration — no meaning is attached to the colour.
export function hueForId(id) {
  let h = 5381;
  const s = id ?? "";
  for (let i = 0; i < s.length; i++) {
    h = (h * 33 + s.charCodeAt(i)) >>> 0;
  }
  return h % 360;
}

// initialsFor reduces a display label to at most two avatar initials, using
// the name part before any " — role" suffix ("Grace — Systems engineer" → "G",
// "Ember Owl" → "EO"). Falls back to "?" for an empty label.
export function initialsFor(label) {
  const name = (label ?? "").split("—")[0].trim();
  const words = name.split(/\s+/).filter(Boolean);
  if (words.length === 0) {
    return "?";
  }
  return words
    .slice(0, 2)
    .map((w) => w[0].toUpperCase())
    .join("");
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
