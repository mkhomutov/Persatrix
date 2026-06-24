//! `reasoning.*` CLI surface — RFC 0051 PR 5 (the first NESTED, enum-valued knob).
//!
//! Split out of [`channel_config`](super::channel_config) (which must stay under
//! the 500-line file-size cap) exactly as the server split
//! `channel_config_reasoning.go` out of `channel_config_handlers.go`: reasoning is
//! the first nested block, so it needs a sub-key shape the flat knobs do not — an
//! enum value-set the parser checks client-side, dotted `reasoning.<sub>` keys, a
//! nested-object PATCH body, and a per-sub-knob view cell.
//!
//! `channel_config` owns the flat registry + the `set`/`get`/`unset` plumbing and
//! pulls these in: [`KNOBS`] is appended to its editable set, [`coerce_enum`]
//! backs the `Enum` arm of its `key=value` parser, [`nest_dotted`] lifts the
//! collected dotted keys into the nested PATCH body, and [`ReasoningConfigView`]
//! is the nested cell of its `ChannelConfigView`.

use serde::Deserialize;
use serde_json::{Map, Value};

use super::channel_config::{ConfigField, KnobType};

/// The accepted value sets for the enum-valued reasoning knobs — the values this
/// build *accepts*, not merely the values the wire could carry. `DEPTHS` lists
/// only `shallow` because `deep` is RFC 0051 Phase 4 (validate-rejected server-
/// side), so the CLI declines it client-side rather than offer a value that always
/// 400s. Promoting a value (adding `deep` when Phase 4 ships) is a one-line change.
pub(crate) const MODES: &[&str] = &["off", "bid", "plan"];
pub(crate) const MODELS: &[&str] = &["fast", "quality"];
pub(crate) const DEPTHS: &[&str] = &["shallow"];

/// The dotted reasoning knobs, appended to the flat `CONFIG_KNOBS` set so the
/// registry covers the union the server's two merge switches accept (flat
/// `mergeConfigPatch` ∪ nested `mergeReasoningPatch`). `mode`/`model`/`depth` are
/// closed enums; `revise` is a plain int whose `≥1` values the server capability-
/// gates (Phase 5), so the CLI only type-checks it.
pub(crate) const KNOBS: &[(&str, KnobType)] = &[
    ("reasoning.mode", KnobType::Enum(MODES)),
    ("reasoning.model", KnobType::Enum(MODELS)),
    ("reasoning.depth", KnobType::Enum(DEPTHS)),
    ("reasoning.revise", KnobType::Int),
];

/// The nested `reasoning` block of `ChannelConfigView`, mirroring the Go
/// `reasoningConfigResponse`. Each sub-knob is a resolved `{value, source}` cell
/// exactly like a flat knob; the CLI flattens them to dotted `reasoning.<sub>`
/// rows for the registry, the parser, and the render.
#[derive(Deserialize)]
pub(crate) struct ReasoningConfigView {
    pub(crate) mode: ConfigField,
    pub(crate) model: ConfigField,
    pub(crate) depth: ConfigField,
    pub(crate) revise: ConfigField,
}

impl ReasoningConfigView {
    /// The cell for a dotted `reasoning.<sub>` key, or `None` if `key` is not a
    /// reasoning sub-knob — the render delegation point for `config_rows`.
    pub(crate) fn field(&self, key: &str) -> Option<&ConfigField> {
        match key {
            "reasoning.mode" => Some(&self.mode),
            "reasoning.model" => Some(&self.model),
            "reasoning.depth" => Some(&self.depth),
            "reasoning.revise" => Some(&self.revise),
            _ => None,
        }
    }
}

/// Coerce a closed-enum knob's raw value: accept only a member of `allowed`,
/// naming both the knob and the value set on a miss (the same fail-before-the-
/// round-trip posture as the wrong-typed-scalar reject). Rides the wire as a plain
/// JSON string. Backs the `Enum` arm of `channel_config`'s `key=value` parser.
pub(crate) fn coerce_enum(key: &str, allowed: &[&str], raw: &str) -> Result<Value, String> {
    if allowed.contains(&raw) {
        Ok(Value::String(raw.to_string()))
    } else {
        Err(format!(
            "knob '{key}' expects one of [{}], got '{raw}'",
            allowed.join(", ")
        ))
    }
}

/// Lift a flat `{knob: value}` map into the wire PATCH body, nesting any dotted
/// key (`reasoning.mode`) under its namespace object (`{"reasoning": {"mode": …}}`)
/// so the body matches the server's nested knob shape — the top-level `reasoning`
/// case in `mergeConfigPatch` dispatches to `mergeReasoningPatch`, which merges the
/// sub-object key by key. Flat keys stay top-level. A `null` value (an `unset`)
/// nests identically, so the server clears just that sub-knob. Duplicate detection
/// runs on the FLAT key before this step, so the nest is collision-free.
pub(crate) fn nest_dotted(flat: Map<String, Value>) -> Map<String, Value> {
    let mut out = Map::new();
    for (key, value) in flat {
        match key.split_once('.') {
            Some((ns, sub)) => {
                let entry = out
                    .entry(ns.to_string())
                    .or_insert_with(|| Value::Object(Map::new()));
                if let Value::Object(obj) = entry {
                    obj.insert(sub.to_string(), value);
                }
            }
            None => {
                out.insert(key, value);
            }
        }
    }
    out
}

#[cfg(test)]
#[path = "channel_config_reasoning_tests.rs"]
mod tests;
