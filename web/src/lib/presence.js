// Pure helpers behind the live "who's working" indicator (Tier 0, frontend-only
// — RFC 0048 console). The conversation panel shows an at-a-glance status line
// above the composer ("Ember Owl is thinking…", "Waiting for you") so the
// operator can see a turn is in flight rather than staring at a silent timeline
// between the 3s polls. Kept as pure functions (no component state) so the
// phrasing, the optimistic clear, and the escalation thresholds are unit-tested
// directly — the PresenceBar component and its ChannelTimeline owner render over
// the same logic without re-deriving it (the lib/format.js + lib/agents.js +
// lib/interactions.js pattern).
//
// Tier 0 is optimistic: a DM turn is driven by the synchronous send lifecycle,
// and a group turn by the agents an outbound message @-addressed, cleared when
// each replies. There is no server activity signal yet, so the indicator only
// knows about turns THIS console triggered; Tier 1 (a per-channel /activity
// endpoint) will make it accurate for every trigger and across reloads.

// SLOW_AFTER_MS / EXPIRE_AFTER_MS bound the optimistic indicator. After
// SLOW_AFTER_MS a still-pending turn softens to "taking a while…" so a slow
// reply doesn't read as a stall; after EXPIRE_AFTER_MS it self-clears, so an
// ignored mention (or an agent that never answers) can't leave a stuck
// "thinking…" line. EXPIRE clears the DM handler's 30s default with headroom;
// group publishes have no server bound, so the ceiling is their only backstop.
export const SLOW_AFTER_MS = 12000;
export const EXPIRE_AFTER_MS = 60000;

// shortAgentName is the indicator's display name for a persona: the bare name
// (NOT the picker's "name — role" — the bar is glanceable, the role is noise
// here), falling back to the raw id for an agent absent from the best-effort
// map, mirroring senderLabel's unknown-id fallback.
export function shortAgentName(agentId, agentsById = {}) {
  const agent = agentsById[agentId];
  return (agent && agent.name) || agentId;
}

// nameList renders the subject of the indicator phrase: one name, two joined
// with "and", or an "N agents" tally past two (a long roster of names would
// overflow the one-line bar and stop being glanceable). Blank ids are dropped.
export function nameList(agentIds = [], agentsById = {}) {
  const ids = (agentIds ?? []).filter(Boolean);
  if (ids.length === 0) return "";
  if (ids.length === 1) return shortAgentName(ids[0], agentsById);
  if (ids.length === 2) {
    return `${shortAgentName(ids[0], agentsById)} and ${shortAgentName(ids[1], agentsById)}`;
  }
  return `${ids.length} agents`;
}

// thinkingPhrase is the full indicator line, or "" when nobody is working (the
// bar then renders nothing). The verb agrees with the subject count; the tail
// softens to "taking a while…" once a turn passes the slow threshold.
export function thinkingPhrase(agentIds = [], agentsById = {}, { slow = false } = {}) {
  const ids = (agentIds ?? []).filter(Boolean);
  if (ids.length === 0) return "";
  const subject = nameList(ids, agentsById);
  const verb = ids.length === 1 ? "is" : "are";
  const tail = slow ? "taking a while…" : "thinking…";
  return `${subject} ${verb} ${tail}`;
}

// pruneThinking drops the personas that have since posted: a sender appearing in
// the freshly-arrived messages has produced its reply and is no longer working;
// the rest stay pending. (Tier 1 replaces this optimistic clear with the
// server's real per-channel activity signal.)
export function pruneThinking(thinkingIds = [], messages = []) {
  const senders = new Set(
    (messages ?? []).map((m) => m && m.sender_id).filter(Boolean),
  );
  return (thinkingIds ?? []).filter((id) => id && !senders.has(id));
}

// elapsedPhase maps how long a turn has been pending to the indicator's phase:
// "active" → "slow" (soften the copy) → "expired" (self-clear). Pure over an
// elapsed-ms input so the thresholds are tested without timers; the component
// drives the actual transitions off setTimeout.
export function elapsedPhase(
  elapsedMs,
  { slowAfterMs = SLOW_AFTER_MS, expireAfterMs = EXPIRE_AFTER_MS } = {},
) {
  if (elapsedMs >= expireAfterMs) return "expired";
  if (elapsedMs >= slowAfterMs) return "slow";
  return "active";
}
