//! Tests for [`super`] (the [`crate::commands::channel_config_reasoning`] module)
//! plus the RFC 0051 reasoning behaviour of the `channel_config` parse/build path.
//! Wired in via `#[path = "channel_config_reasoning_tests.rs"] mod tests;` so the
//! production file stays under the 500-line file-size cap (same convention as
//! `channel_config_tests.rs`).

use super::*;
use crate::commands::channel_config::{build_set_patch, build_unset_patch, parse_set_assignment};

// ─── reasoning module: coerce_enum ─────────────────────────────────

#[test]
fn coerce_enum_accepts_a_member_and_rejects_others() {
    assert_eq!(
        coerce_enum("reasoning.mode", MODES, "bid").unwrap(),
        Value::String("bid".into())
    );
    let err = coerce_enum("reasoning.mode", MODES, "loud").unwrap_err();
    assert!(err.contains("reasoning.mode"), "names the knob: {err}");
    assert!(
        err.contains("off") && err.contains("bid") && err.contains("plan"),
        "lists the accepted values: {err}"
    );
}

// ─── reasoning module: nest_dotted ─────────────────────────────────

#[test]
fn nest_dotted_nests_dotted_keys_and_leaves_flat_keys() {
    let mut flat = serde_json::Map::new();
    flat.insert("floor_control".into(), Value::Bool(true));
    flat.insert("reasoning.mode".into(), Value::String("bid".into()));
    flat.insert("reasoning.model".into(), Value::String("fast".into()));
    let out = nest_dotted(flat);
    assert_eq!(out["floor_control"], Value::Bool(true));
    assert_eq!(
        out["reasoning"],
        serde_json::json!({"mode": "bid", "model": "fast"})
    );
}

#[test]
fn nest_dotted_preserves_a_null_sub_value() {
    let mut flat = serde_json::Map::new();
    flat.insert("reasoning.mode".into(), Value::Null);
    assert_eq!(
        nest_dotted(flat)["reasoning"],
        serde_json::json!({ "mode": null })
    );
}

// ─── reasoning module: ReasoningConfigView::field ──────────────────

#[test]
fn reasoning_view_field_resolves_each_sub_knob_and_rejects_others() {
    let v: ReasoningConfigView = serde_json::from_value(serde_json::json!({
        "mode":   {"value": "plan",    "source": "channel"},
        "model":  {"value": "fast",    "source": "default"},
        "depth":  {"value": "shallow", "source": "default"},
        "revise": {"value": 0,         "source": "default"},
    }))
    .unwrap();
    assert_eq!(
        v.field("reasoning.mode").unwrap().value,
        serde_json::json!("plan")
    );
    assert_eq!(
        v.field("reasoning.revise").unwrap().value,
        serde_json::json!(0)
    );
    assert!(v.field("floor_control").is_none());
    assert!(v.field("reasoning.bogus").is_none());
}

// ─── parse_set_assignment: reasoning enum + int ────────────────────

#[test]
fn parse_set_assignment_accepts_reasoning_enum_and_int_values() {
    // The enum sub-knobs coerce to a plain JSON string (the wire type the server
    // decodes); `revise` is a plain int knob.
    assert_eq!(
        parse_set_assignment("reasoning.mode=bid").unwrap(),
        ("reasoning.mode".to_string(), Value::String("bid".into()))
    );
    assert_eq!(
        parse_set_assignment("reasoning.model=quality").unwrap(),
        (
            "reasoning.model".to_string(),
            Value::String("quality".into())
        )
    );
    assert_eq!(
        parse_set_assignment("reasoning.depth=shallow").unwrap(),
        (
            "reasoning.depth".to_string(),
            Value::String("shallow".into())
        )
    );
    assert_eq!(
        parse_set_assignment("reasoning.revise=0").unwrap().1,
        serde_json::json!(0)
    );
}

#[test]
fn parse_set_assignment_rejects_bad_reasoning_enum_value() {
    // A value outside the closed set fails locally, naming both the knob and the
    // accepted values — not a round-tripped 400.
    let m = parse_set_assignment("reasoning.mode=loud").unwrap_err();
    assert!(m.contains("reasoning.mode"), "names the knob: {m}");
    assert!(
        m.contains("off") && m.contains("bid") && m.contains("plan"),
        "lists the accepted values: {m}"
    );
    // `deep` is RFC 0051 Phase 4 — not an accepted depth value in this build, so
    // the CLI declines it client-side rather than offer a value that always 400s.
    let d = parse_set_assignment("reasoning.depth=deep").unwrap_err();
    assert!(
        d.contains("reasoning.depth") && d.contains("shallow"),
        "depth offers only shallow: {d}"
    );
}

// ─── build_set_patch / build_unset_patch: nesting ──────────────────

#[test]
fn build_set_patch_nests_dotted_reasoning_keys() {
    // Dotted keys lift into ONE top-level "reasoning" object — the nested shape
    // the server's mergeReasoningPatch consumes, not flat dotted keys.
    let specs = vec![
        "reasoning.mode=bid".to_string(),
        "reasoning.model=fast".to_string(),
    ];
    let patch = build_set_patch(&specs).unwrap();
    assert_eq!(patch.len(), 1);
    assert_eq!(
        patch["reasoning"],
        serde_json::json!({"mode": "bid", "model": "fast"})
    );
}

#[test]
fn build_set_patch_mixes_flat_and_nested_knobs() {
    // A command touching both a flat knob and a reasoning sub-knob produces both a
    // top-level scalar and the nested reasoning object in one body.
    let specs = vec![
        "floor_control=true".to_string(),
        "reasoning.mode=plan".to_string(),
    ];
    let patch = build_set_patch(&specs).unwrap();
    assert_eq!(patch["floor_control"], Value::Bool(true));
    assert_eq!(patch["reasoning"], serde_json::json!({"mode": "plan"}));
}

#[test]
fn build_set_patch_rejects_duplicate_dotted_knob() {
    // Duplicate detection runs on the flat dotted key before nesting.
    let specs = vec![
        "reasoning.mode=bid".to_string(),
        "reasoning.mode=plan".to_string(),
    ];
    assert!(build_set_patch(&specs)
        .unwrap_err()
        .contains("more than once"));
}

#[test]
fn build_unset_patch_nests_dotted_reasoning_keys() {
    // Clearing a sub-knob nests an explicit null so the server clears just THAT
    // sub-knob back to inherit (mergeReasoningPatch's per-sub-knob null branch).
    let patch = build_unset_patch(&["reasoning.mode".to_string()]).unwrap();
    assert_eq!(patch["reasoning"], serde_json::json!({ "mode": null }));
}
