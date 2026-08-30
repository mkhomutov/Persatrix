// The flat + nested-reasoning governance knobs of the RFC 0050 channel-config
// surface, in render order — the sibling registry of autonomousKnobs.js, carved
// out of ChannelSettings.svelte when the ISSUE-0114 per-channel cascade-depth
// knob (v0.3.13) pushed that panel past the 500-line cap. Each descriptor is
// {key, label, type} (plus `options` for enums), typed so the control and the
// patch coercion are driven by one source of truth. `int` knobs are
// non-negative integers (mirrored as input bounds; the server stays the
// authority). `chair` is a member-constrained persona picker; `bool` is
// floor_control. `enum` (RFC 0051 reasoning.{mode,model,depth}) is a generic
// string select over a fixed `options` set — `depth` lists only `shallow`
// because `deep` is RFC 0051 Phase 4 (validate-rejected), so the panel offers
// the accepted value rather than a lone dead `deep` entry.
//
// The `reasoning.*` keys are DOTTED: the reasoning block is the first NESTED
// knob, so it reads back at resp.reasoning.<sub> and patches as
// {reasoning: {<sub>: …}}. The panel's `fieldFor`/`setBody` resolve the dotted
// path; every other (flat) knob is untouched by that.
export const KNOBS = [
  { key: "floor_control", label: "Floor control", type: "bool" },
  {
    key: "salience_max_channel_members",
    label: "Salience max channel members",
    type: "int",
  },
  {
    key: "max_replies_per_participant_per_interaction",
    label: "Max replies per participant per interaction",
    type: "int",
  },
  // ISSUE-0114 (v0.3.13): the per-channel Layer 0 cascade-depth cap — the
  // productive-discussion length knob. The server rejects non-positive
  // overrides (an explicit 0 would be a lying no-op; inherit rides null) and
  // warns server-side on a value above the fleet cap rather than rejecting.
  { key: "max_cascade_depth", label: "Max cascade depth", type: "int" },
  { key: "end_vote_threshold", label: "End-vote threshold", type: "int" },
  { key: "end_vote_window", label: "End-vote window (seconds)", type: "int" },
  {
    key: "interaction_idle_timeout_seconds",
    label: "Interaction idle timeout (seconds)",
    type: "int",
  },
  {
    key: "interaction_budget_tokens",
    label: "Interaction budget (tokens)",
    type: "int",
  },
  { key: "escalation_chair_id", label: "Escalation chair", type: "chair" },
  {
    key: "reasoning.mode",
    label: "Reasoning mode",
    type: "enum",
    options: ["off", "bid", "plan"],
  },
  {
    key: "reasoning.model",
    label: "Reasoning model",
    type: "enum",
    options: ["fast", "quality"],
  },
  {
    key: "reasoning.depth",
    label: "Reasoning depth",
    type: "enum",
    options: ["shallow"],
  },
  {
    key: "reasoning.revise",
    label: "Reasoning revise rounds",
    type: "int",
    // No client-side upper bound, unlike depth offering only `shallow`. The
    // server gates revise: it must be 0..2 and `>= 1` requires mode: plan (the
    // reflexion critic re-reads the draft against the plan, RFC 0051 Phase 5).
    // A `<select>` can only OFFER its options whereas a number `max` triggers
    // form constraint validation: an out-of-range value would make
    // `<form onsubmit>` invalid and silently block the WHOLE save. The
    // revise↔mode rule cannot be a static `max` at all. So revise defers to the
    // server's 400, which at least surfaces a reason — the server stays the
    // authority.
  },
];
