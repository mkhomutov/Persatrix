// Pure helpers behind the v0.3.8 interaction-summary surface (RFC 0020 §C/§D).
//
// The closed-interaction read API is per-agent: each persona that took part in
// an interaction persists its own synthesised summary row at close. The
// conversation view shows ONE affordance for the channel's latest closed
// interaction, so the surface fetches across the channel's participating agents
// and merges with `pickLatestClosed`. The close trigger and the failure
// sentinel are rendered honestly (SS3) via the small mappers here. Kept as pure
// functions (no component state) so the merge + labelling are unit-tested
// directly, mirroring format.js.

// SUMMARY_UNAVAILABLE_TEXT is the failure sentinel the agent persists when the
// on-close summariser could not produce a summary (interaction_janitor.py
// SUMMARY_UNAVAILABLE_TEXT). The read path forwards it verbatim; the surface
// renders it as an explicit "unavailable" state, never blanked.
export const SUMMARY_UNAVAILABLE_TEXT = "[interaction summary unavailable]";

// pickLatestClosed reduces a flat list of closed-interaction records (merged
// from several agents' read calls) to the single newest one by `closed_at`, or
// null when the list is empty. The same interaction summarised by multiple
// agents collapses naturally — they share a `closed_at`, so whichever the
// reducer reaches first wins and only one affordance shows.
export function pickLatestClosed(records) {
  if (!Array.isArray(records) || records.length === 0) {
    return null;
  }
  return records.reduce((latest, rec) =>
    (rec?.closed_at ?? 0) > (latest?.closed_at ?? 0) ? rec : latest,
  );
}

// closeTriggerLabel turns the RFC 0020 `close_reason` into a short human label
// for the affordance. The reasons are the boundary_detectors.py literals:
//   - "cost"       — the RFC 0030 Layer 1 per-interaction cost ceiling hit.
//   - "idle_gap"   — the conversation went quiet past the idle window.
//   - "structural" — an explicit end (RFC 0020 END_INTERACTION / the Layer 4
//                    end-vote, which routes through the structural close). The
//                    episode row does not distinguish a vote-close from a plain
//                    structural close, so "ended" is the honest label rather
//                    than over-claiming "by vote".
// An unknown / empty reason degrades to a neutral "closed".
export function closeTriggerLabel(reason) {
  switch (reason) {
    case "cost":
      return "cost limit reached";
    case "idle_gap":
      return "went idle";
    case "structural":
      return "ended";
    default:
      return "closed";
  }
}

// participantAgentIds resolves the personas whose closed-interaction summaries
// the surface should query for a conversation: a DM's single peer, or a group
// channel's members. The human principal (`exclude`) is dropped — it has no
// agent-side episodic memory to query. Member entries may be raw ids or `{id}`
// objects (the channel DTO carries `{id, respond}`), and blanks are filtered.
export function participantAgentIds({
  isDM = false,
  peerId = "",
  members = [],
  exclude,
} = {}) {
  if (isDM) {
    return peerId && peerId !== exclude ? [peerId] : [];
  }
  return (members ?? [])
    .map((m) => (typeof m === "string" ? m : m?.id))
    .filter((id) => id && id !== exclude);
}

// isSummaryUnavailable reports whether a summary body is the failure sentinel
// (so the surface shows "unavailable" rather than the raw marker). A blank /
// absent summary is NOT the sentinel — the read path already filters blanks, so
// a record reaching the surface either has a real summary or the sentinel.
export function isSummaryUnavailable(summary) {
  return summary === SUMMARY_UNAVAILABLE_TEXT;
}
