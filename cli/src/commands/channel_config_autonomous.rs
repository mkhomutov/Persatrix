//! `autonomous.*` CLI surface — RFC 0052 PR 2 (the SECOND nested config block,
//! after RFC 0051 `reasoning`). Split out of [`channel_config`](super::channel_config)
//! (which must stay under the 500-line file-size cap) exactly as
//! `channel_config_reasoning` was, and for the same reason the server split
//! `channel_config_autonomous.go` out of `channel_config_handlers.go`: the
//! `autonomous` block is a nested object whose own sub-keys carry the
//! set / null-unset / absent tri-state, one level below the flat knobs.
//!
//! It also lands one wire shape the reasoning block did not need: a LIST-valued
//! sub-knob, `autonomous.agenda` (a `[]string` on the server). So this file owns
//! the [`coerce_list`] the flat `key=value` parser dispatches to for the new
//! [`KnobType::List`](super::channel_config::KnobType) arm, the autonomous analogue
//! of `reasoning`'s [`coerce_enum`](super::channel_config_reasoning::coerce_enum).
//!
//! `channel_config` owns the flat registry + the `set`/`get`/`unset` plumbing and
//! pulls these in: [`KNOBS`] is appended to its editable set (after
//! `reasoning::KNOBS`), [`coerce_list`] backs the `List` arm of its `key=value`
//! parser, and [`AutonomousConfigView`] is the nested cell of its
//! `ChannelConfigView` (resolved by [`AutonomousConfigView::field`] alongside the
//! reasoning cells). The convener / cap / distinct-from-chair cross-field rules are
//! all server-side (`validate`), so the CLI only type-checks each sub-knob here.

use serde::Deserialize;
use serde_json::Value;
use serde_yaml_ng::Value as Yaml;

use super::channel_config::{ConfigField, KnobType};

/// The dotted autonomous knobs, appended to the flat `CONFIG_KNOBS` set so the CLI
/// registry covers the union the server's THREE merge switches accept (flat
/// `mergeConfigPatch` ∪ nested `mergeReasoningPatch` ∪ nested `mergeAutonomousPatch`).
/// `enabled` is bool, `max_rounds` int, `agenda` a string list; `topic` / `convener`
/// / `goal` are plain strings. The "convener must be a member distinct from the
/// chair", the "an armed channel needs a positive `interaction_budget_tokens` cap",
/// and the agenda-length rules are cross-field / cross-knob, so the CLI defers them
/// to the server's `validate` 400 — it only type-checks the wire shape here.
pub(crate) const KNOBS: &[(&str, KnobType)] = &[
    ("autonomous.enabled", KnobType::Bool),
    ("autonomous.topic", KnobType::Str),
    ("autonomous.agenda", KnobType::List),
    ("autonomous.convener", KnobType::Str),
    ("autonomous.goal", KnobType::Str),
    ("autonomous.max_rounds", KnobType::Int),
];

/// The nested `autonomous` block of `ChannelConfigView`, mirroring the Go
/// `autonomousConfigResponse`. Each sub-knob is a resolved `{value, source}` cell
/// exactly like a flat knob; the CLI flattens them to dotted `autonomous.<sub>`
/// rows for the registry, the parser, and the render.
#[derive(Deserialize)]
pub(crate) struct AutonomousConfigView {
    pub(crate) enabled: ConfigField,
    pub(crate) topic: ConfigField,
    pub(crate) agenda: ConfigField,
    pub(crate) convener: ConfigField,
    pub(crate) goal: ConfigField,
    pub(crate) max_rounds: ConfigField,
}

impl AutonomousConfigView {
    /// The cell for a dotted `autonomous.<sub>` key, or `None` if `key` is not an
    /// autonomous sub-knob — the render delegation point for `config_rows` (tried
    /// after the reasoning cells).
    pub(crate) fn field(&self, key: &str) -> Option<&ConfigField> {
        match key {
            "autonomous.enabled" => Some(&self.enabled),
            "autonomous.topic" => Some(&self.topic),
            "autonomous.agenda" => Some(&self.agenda),
            "autonomous.convener" => Some(&self.convener),
            "autonomous.goal" => Some(&self.goal),
            "autonomous.max_rounds" => Some(&self.max_rounds),
            _ => None,
        }
    }
}

/// Coerce a list-valued knob's raw value into a JSON array of trimmed, non-empty
/// items — the wire shape the server's `decodeKnob[[]string]` decodes. The CLI
/// delimiter is the comma (agenda items are short sub-topic phrases); an item that
/// needs a literal comma is the case for the nested `autonomous:` YAML
/// config-as-code path the boot loader applies, not this convenience surface.
///
/// The trim + blank-drop here is a CLIENT-SIDE convenience of the comma surface, NOT
/// a mirror of the server: `decodeKnob[[]string]` stores items VERBATIM, and the
/// server's agenda-item check (`AutonomousConfig.validateFields`) trims only to
/// REJECT a blank/whitespace-only item (a `400`), never to normalize. So the same
/// whitespace-bearing input persists differently across the two entry surfaces — the
/// boot-loader YAML path keeps `" Cost "` verbatim and `400`s on a blank item, where
/// this surface stores `"Cost"` and drops the blank. Kept deliberately forgiving so a
/// stray space or trailing comma in `set` is not a round-trip error.
///
/// An empty (or all-separator/blank) raw value yields `[]` — an explicit empty-
/// agenda override, the list analogue of `escalation_chair_id=`'s empty-string
/// override, and a state DISTINCT from `unset` (clear back to inherit). It cannot
/// fail (any string maps to an array), so unlike [`coerce_enum`] it returns the
/// value directly rather than a `Result`.
pub(crate) fn coerce_list(raw: &str) -> Value {
    let items: Vec<Value> = raw
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(|s| Value::String(s.to_string()))
        .collect();
    Value::Array(items)
}

/// Coerce a declared-YAML list value (a sequence of string scalars) into the same
/// JSON-array wire shape [`coerce_list`] builds — items trimmed, blanks dropped. The
/// YAML analogue of [`coerce_list`], dispatched from `channel_config_yaml`'s
/// `yaml_to_knob_json` for the `List` arm so that file (at the 500-line cap) carries
/// only a one-line delegation. In practice the agenda rides the nested `autonomous:`
/// block the boot loader applies, not a flat-dotted YAML key — this exists to keep
/// the YAML coercion exhaustive over [`KnobType`] and honest about the wire type.
pub(crate) fn coerce_yaml_list(key: &str, y: &Yaml) -> Result<Value, String> {
    let items = y
        .as_sequence()
        .ok_or_else(|| format!("knob '{key}' expects a list of strings"))?;
    let mut out = Vec::with_capacity(items.len());
    for item in items {
        let s = item
            .as_str()
            .ok_or_else(|| format!("knob '{key}' expects a list of strings"))?
            .trim();
        if !s.is_empty() {
            out.push(Value::String(s.to_string()));
        }
    }
    Ok(Value::Array(out))
}

#[cfg(test)]
#[path = "channel_config_autonomous_tests.rs"]
mod tests;
