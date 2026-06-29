//! Tests for [`super`] (the [`crate::commands::channel_config_autonomous`] module)
//! plus the RFC 0052 autonomous behaviour of the `channel_config` parse/build path.
//! Wired in via `#[path = "channel_config_autonomous_tests.rs"] mod tests;` so the
//! production file stays under the 500-line file-size cap (same convention as
//! `channel_config_reasoning_tests.rs`).

use super::*;
use crate::commands::channel_config::{build_set_patch, build_unset_patch, parse_set_assignment};

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
