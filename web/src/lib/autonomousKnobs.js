// RFC 0052 (v0.3.11) autonomous-channel config knobs — the web analogue of the
// CLI's `channel_config_autonomous.rs` registry. Shared by ChannelSettings.svelte
// (which folds these into its one draft/patch/save path) and AutonomousSettings.svelte
// (which renders the rows). Each descriptor mirrors the flat-knob shape the panel
// already uses ({key, label, type}); the keys are DOTTED (`autonomous.<sub>`) so they
// read back at resp.autonomous.<sub> and patch as {autonomous: {<sub>: …}} via the
// panel's existing fieldFor/setBody dotted-path handling.
//
// Two control types are new to this block: `text` (a free-text input for the
// topic/goal strings — the flat knobs only had bool/int/enum/chair) and `list` (the
// multiline agenda, a `[]string` on the wire). `convener` is a member picker like
// the chair, but over its own candidate set.
export const AUTONOMOUS_KNOBS = [
  {
    key: "autonomous.enabled",
    label: "Autonomous (human-free) mode",
    type: "bool",
  },
  { key: "autonomous.topic", label: "Topic", type: "text" },
  {
    key: "autonomous.agenda",
    label: "Agenda (one item per line)",
    type: "list",
  },
  { key: "autonomous.convener", label: "Convener", type: "convener" },
  { key: "autonomous.goal", label: "Goal", type: "text" },
  { key: "autonomous.max_rounds", label: "Max rounds", type: "int" },
];

// agendaToText renders the wire value (a JSON string array) as the newline-joined
// text a <textarea> binds to. Tolerates a string draft (already-edited) and a
// null/absent value (renders empty) so adopt and re-render are both safe.
export function agendaToText(value) {
  if (Array.isArray(value)) return value.join("\n");
  return value == null ? "" : String(value);
}

// agendaToList coerces the <textarea> text (one item per line) back to the wire
// shape: a trimmed, non-empty string array, mirroring the server's `[]string`
// decode and the CLI's comma-split. Also accepts an array (the original snapshot)
// so the change-detection compare can normalize both sides through one function.
export function agendaToList(value) {
  const items = Array.isArray(value) ? value : String(value ?? "").split("\n");
  return items.map((s) => s.trim()).filter((s) => s.length > 0);
}
