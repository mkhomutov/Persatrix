//! Tests for [`super`] (the [`crate::commands::channel_config_autonomous`] module)
//! plus the RFC 0052 autonomous behaviour of the `channel_config` parse/build path.
//! Wired in via `#[path = "channel_config_autonomous_tests.rs"] mod tests;` so the
//! production file stays under the 500-line file-size cap (same convention as
//! `channel_config_reasoning_tests.rs`).

use super::*;
use crate::commands::channel_config::{build_set_patch, build_unset_patch, parse_set_assignment};
use crate::commands::channel_config_reasoning::{classify_yaml_key, YamlNestedKey};
use crate::commands::channel_config_yaml::parse_channel_block;

// ─── autonomous module: coerce_list ────────────────────────────────

#[test]
fn coerce_list_splits_trims_and_drops_blanks() {
    // The comma is the CLI delimiter; each item is trimmed and blanks dropped,
    // mirroring the server's non-blank, whitespace-trimmed agenda-item check.
    assert_eq!(
        coerce_list("Build cost,  Coupling risk ,Migration effort"),
        serde_json::json!(["Build cost", "Coupling risk", "Migration effort"])
    );
    // Trailing/empty separators collapse out rather than producing blank items.
    assert_eq!(coerce_list("a,,b, ,c"), serde_json::json!(["a", "b", "c"]));
}

#[test]
fn coerce_list_empty_value_is_an_explicit_empty_agenda() {
    // An empty (or all-separator) value yields `[]` — an explicit empty-agenda
    // override, distinct from `unset` (clear back to inherit).
    assert_eq!(coerce_list(""), serde_json::json!([]));
    assert_eq!(coerce_list(" , , "), serde_json::json!([]));
}

// ─── autonomous module: AutonomousConfigView::field ────────────────

#[test]
fn autonomous_view_field_resolves_each_sub_knob_and_rejects_others() {
    let v: AutonomousConfigView = serde_json::from_value(serde_json::json!({
        "enabled":    {"value": true,                       "source": "channel"},
        "topic":      {"value": "Monorepo?",                "source": "channel"},
        "agenda":     {"value": ["Cost", "Coupling"],       "source": "channel"},
        "convener":   {"value": "nova-sparrow",             "source": "channel"},
        "goal":       {"value": "A recommendation",         "source": "channel"},
        "max_rounds": {"value": 12,                          "source": "default"},
        "schedule_interval_seconds": {"value": 3600, "source": "channel"},
        "max_convenings":            {"value": 10,   "source": "channel"},
        "standing_budget_tokens":    {"value": 0,    "source": "default"},
    }))
    .unwrap();
    assert_eq!(
        v.field("autonomous.enabled").unwrap().value,
        serde_json::json!(true)
    );
    assert_eq!(
        v.field("autonomous.agenda").unwrap().value,
        serde_json::json!(["Cost", "Coupling"])
    );
    assert_eq!(
        v.field("autonomous.max_rounds").unwrap().value,
        serde_json::json!(12)
    );
    // RFC 0052 §E / PR 7 standing sub-knobs resolve through their own arms.
    assert_eq!(
        v.field("autonomous.schedule_interval_seconds")
            .unwrap()
            .value,
        serde_json::json!(3600)
    );
    assert_eq!(
        v.field("autonomous.max_convenings").unwrap().value,
        serde_json::json!(10)
    );
    assert_eq!(
        v.field("autonomous.standing_budget_tokens").unwrap().value,
        serde_json::json!(0)
    );
    // A flat knob and an unknown autonomous sub-knob both miss — the render
    // delegation falls through to its `unreachable!` only for a real registry knob.
    assert!(v.field("floor_control").is_none());
    assert!(v.field("autonomous.bogus").is_none());
}

// ─── parse_set_assignment: autonomous sub-knobs ────────────────────

#[test]
fn parse_set_assignment_coerces_autonomous_sub_knobs_per_type() {
    // enabled is bool, max_rounds int, topic/convener/goal plain strings.
    assert_eq!(
        parse_set_assignment("autonomous.enabled=true").unwrap(),
        ("autonomous.enabled".to_string(), Value::Bool(true))
    );
    assert_eq!(
        parse_set_assignment("autonomous.max_rounds=20").unwrap(),
        ("autonomous.max_rounds".to_string(), serde_json::json!(20))
    );
    // RFC 0052 §E / PR 7 standing sub-knobs are ints (schedule_interval_seconds +
    // max_convenings a Go int, standing_budget_tokens a Go int64 — all int-class).
    assert_eq!(
        parse_set_assignment("autonomous.schedule_interval_seconds=3600").unwrap(),
        (
            "autonomous.schedule_interval_seconds".to_string(),
            serde_json::json!(3600)
        )
    );
    assert_eq!(
        parse_set_assignment("autonomous.standing_budget_tokens=5000000").unwrap(),
        (
            "autonomous.standing_budget_tokens".to_string(),
            serde_json::json!(5000000)
        )
    );
    assert_eq!(
        parse_set_assignment("autonomous.convener=nova-sparrow").unwrap(),
        (
            "autonomous.convener".to_string(),
            Value::String("nova-sparrow".into())
        )
    );
}

#[test]
fn parse_set_assignment_coerces_agenda_list_to_a_json_array() {
    // The list knob rides the wire as a JSON array (server `decodeKnob[[]string]`),
    // NOT a comma string — the CLI splits client-side so a wrong shape never 400s.
    let (k, v) = parse_set_assignment("autonomous.agenda=Cost, Coupling, Migration").unwrap();
    assert_eq!(k, "autonomous.agenda");
    assert_eq!(v, serde_json::json!(["Cost", "Coupling", "Migration"]));
}

#[test]
fn parse_set_assignment_rejects_wrong_typed_autonomous_scalars() {
    // The same fail-before-the-round-trip discipline the flat knobs use applies to
    // the nested autonomous scalars (a non-bool / non-int names the knob locally).
    let b = parse_set_assignment("autonomous.enabled=maybe").unwrap_err();
    assert!(
        b.contains("autonomous.enabled") && b.contains("boolean"),
        "{b}"
    );
    let i = parse_set_assignment("autonomous.max_rounds=lots").unwrap_err();
    assert!(
        i.contains("autonomous.max_rounds") && i.contains("integer"),
        "{i}"
    );
}

// ─── build_set_patch / build_unset_patch: autonomous nesting ───────

#[test]
fn build_set_patch_nests_autonomous_sub_knobs_under_the_block() {
    // The dotted autonomous keys nest under one `autonomous` object so the body
    // matches the server's nested-knob shape (mergeConfigPatch's `case "autonomous"`
    // → mergeAutonomousPatch, merged sub-key by sub-key). A flat knob stays top-level.
    let patch = build_set_patch(&[
        "floor_control=true".to_string(),
        "autonomous.enabled=true".to_string(),
        "autonomous.agenda=Cost, Coupling".to_string(),
        "autonomous.convener=nova-sparrow".to_string(),
    ])
    .unwrap();
    assert_eq!(patch["floor_control"], Value::Bool(true));
    assert_eq!(
        patch["autonomous"],
        serde_json::json!({
            "enabled": true,
            "agenda": ["Cost", "Coupling"],
            "convener": "nova-sparrow",
        })
    );
}

#[test]
fn build_unset_patch_nests_a_null_to_clear_one_autonomous_sub_knob() {
    // `unset autonomous.topic` clears just that sub-knob (the server's per-sub-knob
    // null branch), not the whole block — the nested null analogue of reasoning.
    let patch = build_unset_patch(&["autonomous.topic".to_string()]).unwrap();
    assert_eq!(patch["autonomous"], serde_json::json!({ "topic": null }));
}

// ─── YAML config-as-code deferral: the autonomous nested block ──────
// The autonomous analogue of the reasoning deferral tests in
// channel_config_yaml_tests.rs. The classifier is registry-driven, so adding the
// autonomous dotted knobs makes `autonomous:` defer (and `autonomous.<sub>:` reject)
// with no extra wiring — these pin that it actually happens, not just for reasoning.

fn yaml_block(text: &str) -> serde_yaml_ng::Value {
    serde_yaml_ng::from_str(text).expect("test YAML parses")
}

#[test]
fn classify_yaml_key_recognises_the_autonomous_namespace() {
    // Derived from the registry: `autonomous` defers like `reasoning`, a dotted
    // sub-key is FlatDotted (rejectable), and a flat knob stays on the normal path.
    assert!(matches!(
        classify_yaml_key("autonomous"),
        YamlNestedKey::NestedBlock("autonomous")
    ));
    assert!(matches!(
        classify_yaml_key("autonomous.agenda"),
        YamlNestedKey::FlatDotted
    ));
    assert!(matches!(
        classify_yaml_key("floor_control"),
        YamlNestedKey::Other
    ));
}

#[test]
fn parse_channel_block_flags_nested_autonomous_block_and_skips_it() {
    // A declared `autonomous:` mapping is the config-as-code form the boot loader
    // honors; import/diff don't build the nested PATCH, so it must be FLAGGED (a
    // note), not silently dropped — exactly as the reasoning block is.
    let block = yaml_block(
        "name: planning\nfloor_control: true\nautonomous:\n  enabled: true\n  convener: nova-sparrow\n",
    );
    let parsed = parse_channel_block(&block).unwrap();
    assert!(
        parsed.deferred_blocks.contains(&"autonomous"),
        "the autonomous block is flagged"
    );
    assert!(
        !parsed.patch.contains_key("autonomous"),
        "the nested block is not lifted into the flat patch"
    );
    assert_eq!(
        parsed.patch.len(),
        1,
        "only the flat floor_control survives"
    );
}

#[test]
fn parse_channel_block_rejects_flat_dotted_autonomous_key() {
    // A flat dotted `autonomous.enabled:` can never round-trip (the server switch has
    // only the `autonomous` namespace, no leaf case) — rejected client-side, naming
    // the key + steering to the live verb, not lifted into a 400-bound patch.
    let err =
        parse_channel_block(&yaml_block("name: planning\nautonomous.enabled: true\n")).unwrap_err();
    assert!(err.contains("autonomous.enabled"), "names the key: {err}");
    assert!(err.contains("autonomous:"), "names the nested block: {err}");
    assert!(
        err.contains("channel config set"),
        "steers to the live verb: {err}"
    );
}

#[test]
fn parse_channel_block_defers_both_nested_blocks_at_once() {
    // reasoning + autonomous in one block: BOTH defer (each recorded for its note),
    // only the flat knob is lifted — the deferral is generic over every namespace.
    let block = yaml_block(
        "name: planning\nfloor_control: true\nreasoning:\n  mode: bid\nautonomous:\n  enabled: true\n",
    );
    let parsed = parse_channel_block(&block).unwrap();
    assert!(parsed.deferred_blocks.contains(&"reasoning"));
    assert!(parsed.deferred_blocks.contains(&"autonomous"));
    assert_eq!(parsed.patch.len(), 1, "only the flat knob is lifted");
}

// ─── coerce_yaml_list (the List wire-type arm) ─────────────────────

#[test]
fn coerce_yaml_list_maps_a_string_sequence_to_a_trimmed_json_array() {
    // The YAML analogue of coerce_list: a sequence of string scalars → a trimmed,
    // non-empty JSON array (the `[]string` wire shape). Exhaustive over KnobType
    // even though the agenda rides the nested block, not a flat YAML key.
    let seq = yaml_block("- Build cost\n- '  Coupling risk '\n- ''\n");
    assert_eq!(
        coerce_yaml_list("autonomous.agenda", &seq).unwrap(),
        serde_json::json!(["Build cost", "Coupling risk"])
    );
    // A non-sequence (a bare scalar) is a typo worth naming.
    let bad = coerce_yaml_list("autonomous.agenda", &yaml_block("not a list\n"));
    assert!(bad.unwrap_err().contains("autonomous.agenda"));
    // A sequence carrying a non-string scalar (an int item) is also rejected,
    // naming the knob — the `[]string` wire shape admits only string items.
    let mixed = coerce_yaml_list("autonomous.agenda", &yaml_block("- ok\n- 7\n"));
    assert!(mixed.unwrap_err().contains("autonomous.agenda"));
}
