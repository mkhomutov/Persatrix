// Mention parsing/resolution for the channel composer (RFC 0011 mentions over
// the RFC 0048 console). The publish endpoint accepts a `mentions` array of raw
// participant ids (channel_handlers.go, capped at channelMaxMentionsPerPublish);
// the console lets the operator type `@<member-id>` in the composer and lifts
// those tokens into that array, offers a typeahead of the channel's members, and
// highlights resolved mentions in the rendered timeline. Pure functions so the
// parsing is unit-tested directly and the composer + message row render over the
// same logic without re-deriving it (the lib/agents.js + lib/format.js pattern).

// MAX_MENTIONS mirrors the server cap (channelMaxMentionsPerPublish = 10) so the
// composer stops lifting past it locally rather than eating a 400 at post time.
export const MAX_MENTIONS = 10;

// MAX_CANDIDATES bounds the typeahead so a large channel doesn't render an
// unbounded menu — the operator narrows with a few keystrokes, not by scrolling.
export const MAX_CANDIDATES = 8;

// A participant id (validateParticipantID in the store) is an ASCII
// letter/digit start then letters/digits/`-`/`_` — persona ids like `ember-owl`,
// `nova-sparrow`. The token regex anchors `@` to a boundary (start-of-string or
// whitespace) so an email's `@` or a mid-word `@` never reads as a mention. The
// boundary is captured separately (group 1) so callers can keep it as plain text.
//
// Shared across extractMentions + segmentMentions to keep the anchoring single-
// sourced. It carries the `g` flag (and thus mutable `lastIndex`), so every user
// MUST reset `lastIndex = 0` before iterating and run to completion synchronously
// — it is NOT safe to drive lazily or re-enter mid-scan.
const TOKEN_RE = /(^|\s)@([A-Za-z0-9][A-Za-z0-9_-]*)/g;

// ID_CHAR matches a single id body character — used to walk a token under the
// caret without re-running the anchored regex.
const ID_CHAR = /[A-Za-z0-9_-]/;

function idSet(members) {
  if (members instanceof Set) {
    return members;
  }
  return new Set((members ?? []).map((m) => (typeof m === "string" ? m : m.id)));
}

// extractMentions lifts the `@id` tokens in `content` that resolve to an actual
// channel member into the publish payload's `mentions` array — first-seen order,
// de-duplicated, capped at MAX_MENTIONS. A token that matches no member (a typo,
// or `@everyone`) is left as plain text rather than sent: the server would
// reject an unknown participant, and silently dropping it keeps the human prose
// intact while only the resolvable mentions drive the fan-out gate.
export function extractMentions(content, members) {
  const valid = idSet(members);
  const out = [];
  const seen = new Set();
  TOKEN_RE.lastIndex = 0;
  let m;
  while ((m = TOKEN_RE.exec(content ?? "")) !== null) {
    const id = m[2];
    if (valid.has(id) && !seen.has(id)) {
      seen.add(id);
      out.push(id);
      if (out.length >= MAX_MENTIONS) {
        break;
      }
    }
  }
  return out;
}

// findActiveMention reports the `@partial` token the caret currently sits in (so
// the composer can open + filter the typeahead), or null when the caret isn't in
// one. It walks left over id-chars to the run start, requires an `@` immediately
// before it, and requires that `@` to sit at a boundary — the same anchoring as
// TOKEN_RE, so a menu never opens inside an email or a `foo@bar` literal.
export function findActiveMention(text, caret) {
  const value = text ?? "";
  const pos = Math.max(0, Math.min(caret ?? value.length, value.length));
  let i = pos;
  while (i > 0 && ID_CHAR.test(value[i - 1])) {
    i--;
  }
  if (i === 0 || value[i - 1] !== "@") {
    return null;
  }
  const at = i - 1;
  if (at > 0 && !/\s/.test(value[at - 1])) {
    return null;
  }
  return { start: at, query: value.slice(i, pos) };
}

// applyMention replaces the active token (the `@` plus any id-chars running past
// the caret, so re-picking inside a finished token rewrites it cleanly) with
// `@id ` and reports the new caret just past the inserted trailing space, ready
// for the next word.
export function applyMention(text, start, caret, id) {
  const value = text ?? "";
  let end = Math.max(0, Math.min(caret ?? value.length, value.length));
  while (end < value.length && ID_CHAR.test(value[end])) {
    end++;
  }
  // Append a separating space only when the token isn't already followed by
  // whitespace — re-picking a completed `@id ` token must not double the space.
  const followedBySpace = end < value.length && /\s/.test(value[end]);
  const insert = `@${id}${followedBySpace ? "" : " "}`;
  return {
    text: value.slice(0, start) + insert + value.slice(end),
    // Land the caret past the separator either way (the inserted one, or the
    // pre-existing space we left in place).
    caret: start + insert.length + (followedBySpace ? 1 : 0),
  };
}

// mentionCandidates filters the channel's members for the typeahead: a case-
// insensitive match of `query` against the member id or its display name, the
// operator's own id excluded (you don't @-mention yourself), decorated with the
// agent's name/role for a readable row and capped at MAX_CANDIDATES. An empty
// query (just-typed `@`) offers the whole roster.
export function mentionCandidates(query, members, { agentsById = {}, exclude } = {}) {
  const q = (query ?? "").toLowerCase();
  const out = [];
  for (const member of members ?? []) {
    const id = typeof member === "string" ? member : member.id;
    if (!id || id === exclude) {
      continue;
    }
    const agent = agentsById[id];
    const name = agent?.name ?? "";
    if (q && !id.toLowerCase().includes(q) && !name.toLowerCase().includes(q)) {
      continue;
    }
    out.push({ id, name, role: agent?.role ?? "" });
    if (out.length >= MAX_CANDIDATES) {
      break;
    }
  }
  return out;
}

// buildPublishPayload assembles the publishMessage argument: always the sender +
// content, plus a `mentions` array only when `@id` tokens resolved to members of
// THIS channel — keeping the no-mention call shape byte-identical to the
// pre-feature wire (the API client + server field are both optional).
export function buildPublishPayload(senderId, content, members) {
  const mentions = extractMentions(content, members);
  return mentions.length > 0
    ? { senderId, content, mentions }
    : { senderId, content };
}

// segmentMentions splits `content` into ordered render segments, marking the
// `@id` tokens that resolve to one of the message's stored `mentions` so the row
// can highlight them. Returns text segments verbatim (Svelte escapes them on
// render — no {@html}, so a `<script>@id` in a message can never inject), so the
// caller just maps over `{ text, mention }`.
export function segmentMentions(content, mentions) {
  const value = content ?? "";
  const set = idSet(mentions);
  const segments = [];
  let last = 0;
  TOKEN_RE.lastIndex = 0;
  let m;
  while ((m = TOKEN_RE.exec(value)) !== null) {
    const id = m[2];
    if (!set.has(id)) {
      continue;
    }
    const tokenStart = m.index + m[1].length; // skip the captured boundary ws
    if (tokenStart > last) {
      segments.push({ text: value.slice(last, tokenStart), mention: false });
    }
    segments.push({ text: `@${id}`, mention: true });
    last = tokenStart + 1 + id.length;
  }
  if (last < value.length || segments.length === 0) {
    segments.push({ text: value.slice(last), mention: false });
  }
  return segments;
}
