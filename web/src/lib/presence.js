// Pure helpers behind the live "who's working" indicator (RFC 0048 console).
// The conversation panel shows an at-a-glance status line above the composer
// ("Ember Owl is thinking…", "Waiting for you") so the operator can see a turn
// is in flight rather than staring at a silent timeline between the 3s polls.
// Kept as pure functions (no component state) so the phrasing and the
// reconciliation are unit-tested directly — the PresenceBar component and its
// ConversationFeed owner render over the same logic without re-deriving it (the
// lib/format.js + lib/agents.js + lib/interactions.js pattern). The thresholds
// below are exported constants; the controller (lib/presence.svelte.js) drives
// the transitions off setTimeout, covered by the timer specs in
// presence.controller.test.js and the wiring specs in
// ChannelTimeline.presence.test.js.
//
// The indicator fuses two sources (Tier 1): the orchestrator's authoritative
// per-channel /activity set — accurate for EVERY trigger and across reloads —
// and a short-lived optimistic overlay for the console's own just-fired turns,
// which bridges the ≤3s gap until the next poll confirms them. A group overlay
// fades on a grace timer if the server never confirms it (a wrong guess); a DM
// overlay is sticky (DMs don't poll /activity — they're single-trigger, so the
// synchronous send lifecycle is the whole signal). See mergeThinking.

// SLOW_AFTER_MS softens a still-pending turn to "taking a while…" so a slow
// reply doesn't read as a stall. GRACE_MS is how long an unconfirmed optimistic
// group add survives without server confirmation (~one poll interval + margin).
// STALE_AFTER_MS is the dead-poll backstop: if /activity stops responding (or a
// DM reply never lands), the indicator self-clears rather than freezing — set
// above the server's 90s activity TTL so it never races a still-live turn.
export const SLOW_AFTER_MS = 12000;
export const GRACE_MS = 6000;
export const STALE_AFTER_MS = 120000;

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

// mergeThinking folds the two presence sources into the displayed set: the
// server's authoritative /activity ids and the optimistic overlay (each carried
// as { id, expiresAt }). An optimistic id shows while its grace is unexpired
// (expiresAt > nowMs) or sticky (Infinity); a server id always shows. Returns
// the sorted, de-duped display list plus the optimistic ids whose grace has
// lapsed, so the controller can drop them from its overlay. Pure for testing;
// the controller supplies nowMs and the timers.
export function mergeThinking(serverIds = [], graceEntries = [], nowMs = 0) {
  const live = new Set((serverIds ?? []).filter(Boolean));
  const expired = [];
  for (const { id, expiresAt } of graceEntries ?? []) {
    if (expiresAt > nowMs) {
      live.add(id);
    } else {
      expired.push(id);
    }
  }
  return { ids: [...live].sort(), expired };
}
