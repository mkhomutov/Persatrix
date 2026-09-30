// The flat + nested-reasoning governance knobs of the RFC 0050 channel-config
// surface, in render order — the sibling registry of autonomousKnobs.js, carved
// out of ChannelSettings.svelte when the ISSUE-0114 per-channel cascade-depth
// knob (v0.3.13) pushed that panel past the 500-line cap. Each descriptor is
// {key, label, type, group, hint} (plus `options` for enums), typed so the
// control and the patch coercion are driven by one source of truth. `group` is
// the settings section the knob renders under; `hint` is its one-line
// explanation for an operator. `int` knobs are non-negative integers (mirrored
// as input bounds; the server stays the authority). `chair` is a
// member-constrained persona picker; `bool` is floor_control. `enum` (RFC 0051
// reasoning.{mode,model,depth}) is a generic string select over a fixed
// `options` set — `depth` lists only `shallow` because `deep` is RFC 0051
// Phase 4 (validate-rejected), so the panel offers the accepted value rather
// than a lone dead `deep` entry.
//
// The `reasoning.*` keys are DOTTED: the reasoning block is the first NESTED
// knob, so it reads back at resp.reasoning.<sub> and patches as
// {reasoning: {<sub>: …}}. The panel's `fieldFor`/`setBody` resolve the dotted
// path; every other (flat) knob is untouched by that.
const TURNS = "Turn-taking";
const CLOSING = "Closing an interaction";
const REASONING = "Reasoning";

export const KNOBS = [
  {
    key: "floor_control",
    label: "Floor control",
    type: "bool",
    group: TURNS,
    hint: "Responders take the floor one at a time instead of all replying at once.",
  },
  {
    key: "salience_max_channel_members",
    label: "Salience max channel members",
    type: "int",
    group: TURNS,
    hint: "Above this many members the salience bid is skipped and only addressed members reply.",
  },
  {
    key: "max_replies_per_participant_per_interaction",
    label: "Max replies per participant per interaction",
    type: "int",
    group: TURNS,
    hint: "How many posts one participant may make in one interaction; 0 means no cap.",
  },
  // ISSUE-0114 (v0.3.13): the per-channel Layer 0 cascade-depth cap — the
  // productive-discussion length knob. The server rejects non-positive
  // overrides (an explicit 0 would be a lying no-op; inherit rides null) and
  // warns server-side on a value above the fleet cap rather than rejecting.
  {
    key: "max_cascade_depth",
    label: "Max cascade depth",
    type: "int",
    group: TURNS,
    hint: "How many hops of agents replying to agents one post can set off.",
  },
  {
    key: "end_vote_threshold",
    label: "End-vote threshold",
    type: "int",
    group: CLOSING,
    hint: "How many participants must vote that they are done to close the interaction.",
  },
  // W counts consecutive TURNS, not seconds (channels.EndVoteWindow): K
  // distinct votes within W consecutive turns close the interaction.
  {
    key: "end_vote_window",
    label: "End-vote window (turns)",
    type: "int",
    group: CLOSING,
    hint: "The votes must land within this many consecutive turns.",
  },
  {
    key: "interaction_idle_timeout_seconds",
    label: "Interaction idle timeout (seconds)",
    type: "int",
    group: CLOSING,
    hint: "After this long without a post, the next post starts a new interaction; 0 turns this off.",
  },
  {
    key: "interaction_budget_tokens",
    label: "Interaction budget (tokens)",
    type: "int",
    group: CLOSING,
    hint: "Most LLM tokens one interaction may spend; replies stop when it runs out. 0 means no cap.",
  },
  {
    key: "escalation_chair_id",
    label: "Escalation chair",
    type: "chair",
    group: CLOSING,
    hint: "Gets one forced turn when a round stalls with no replies, to sum up or pass the question on.",
  },
  {
    key: "reasoning.mode",
    label: "Reasoning mode",
    type: "enum",
    options: ["off", "bid", "plan"],
    group: REASONING,
    hint: "off answers directly; bid first decides whether to speak; plan also plans the post before writing it.",
  },
  {
    key: "reasoning.model",
    label: "Reasoning model",
    type: "enum",
    options: ["fast", "quality"],
    group: REASONING,
    // ISSUE-0167: the value is stored but never reaches the agents, so the
    // hint must not promise it picks the model.
    hint: "Meant to pick the model for the reasoning step; for now the personas use fast whatever is set (ISSUE-0167).",
  },
  {
    key: "reasoning.depth",
    label: "Reasoning depth",
    type: "enum",
    options: ["shallow"],
    group: REASONING,
    hint: "How deep the reasoning goes; only shallow is available so far.",
  },
  {
    key: "reasoning.revise",
    label: "Reasoning revise rounds",
    type: "int",
    group: REASONING,
    hint: "Critique-and-revise rounds on a draft, 0 to 2; 1 or more needs the plan mode.",
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

// KNOB_SECTIONS groups KNOBS by their `group`, in first-appearance order, for
// the settings form's section headings.
export const KNOB_SECTIONS = KNOBS.reduce((sections, knob) => {
  const last = sections.at(-1);
  if (last && last.title === knob.group) {
    last.knobs.push(knob);
  } else {
    sections.push({ title: knob.group, knobs: [knob] });
  }
  return sections;
}, []);
