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

// ─── value-set lockstep: CLI enum vocab == server ACCEPTED values ──────────
//
// The cross-switch guards in `channel_config_tests.rs` pin knob NAMES and wire-type
// CLASSES (an enum rides the wire as `string`) but NOT the accepted value SETS — so
// a server-side vocab change (rename a mode, or promote `deep` when Phase 4 ships)
// would silently drift MODES/MODELS/DEPTHS and round-trip 400s instead of failing
// here. This guard closes that gap: it parses the accepted values straight from the
// server's `ReasoningOverrides.validate()` (the override path the REST PATCH runs,
// which is exactly what the CLI mirrors) and pins each CLI value-set to it. `deep`
// is a defined constant but a REJECTED case, so it is correctly absent from the
// accepted set — the same capability gate the CLI's DEPTHS encodes by omission.

/// Read the channels-package reasoning source the value-set guard parses.
fn reasoning_go_src() -> String {
    std::fs::read_to_string(format!(
        "{}/../internal/channels/config_reasoning.go",
        env!("CARGO_MANIFEST_DIR")
    ))
    .expect("internal/channels/config_reasoning.go must be readable for the value-set guard")
}

/// Map every `Reasoning…* = "literal"` const to its string value (skips aliases
/// like `DefaultReasoningMode = ReasoningModeOff`, whose RHS is not a quoted lit).
fn go_reasoning_const_literals(src: &str) -> std::collections::BTreeMap<String, String> {
    let mut m = std::collections::BTreeMap::new();
    for raw in src.lines() {
        // Drop any `//` comment so a commented-out `=` never parses as a const.
        let line = raw.split("//").next().unwrap_or("").trim();
        let Some((lhs, rhs)) = line.split_once('=') else {
            continue;
        };
        let name = lhs.trim();
        if !name.starts_with("Reasoning") || name.contains(char::is_whitespace) {
            continue;
        }
        if let Some(lit) = rhs
            .trim()
            .strip_prefix('"')
            .and_then(|s| s.strip_suffix('"'))
        {
            m.insert(name.to_string(), lit.to_string());
        }
    }
    m
}

/// The constant names whose arm in `switch_header`'s switch is ACCEPTED — an arm
/// with no `return` before the next `case`/`default`/closing brace. A rejected
/// value (e.g. `deep`) sits in its own arm that returns an error.
fn go_accepted_case_consts(src: &str, switch_header: &str) -> Vec<String> {
    let lines: Vec<&str> = src
        .lines()
        .map(|l| l.split("//").next().unwrap_or("").trim())
        .collect();
    let start = lines
        .iter()
        .position(|l| l.starts_with(switch_header))
        .unwrap_or_else(|| panic!("switch `{switch_header}` not found; guard parser is stale"));
    let mut accepted = Vec::new();
    for (i, line) in lines.iter().enumerate().skip(start + 1) {
        if let Some(rest) = line.strip_prefix("case ") {
            let arm = lines.get(i + 1).copied().unwrap_or("}");
            // An empty (accepted) arm: the next line is another case/default/close.
            if arm.starts_with("case ") || arm.starts_with("default:") || arm == "}" {
                accepted.extend(
                    rest.trim_end_matches(':')
                        .split(',')
                        .map(|c| c.trim().to_string()),
                );
            }
        } else if line.starts_with("default:") || *line == "}" {
            break; // end of this switch
        }
    }
    accepted
}

#[test]
fn cli_enum_value_sets_match_server_accepted_values() {
    use std::collections::BTreeSet;

    let src = reasoning_go_src();
    let consts = go_reasoning_const_literals(&src);
    let resolve = |names: Vec<String>| -> BTreeSet<String> {
        names
            .into_iter()
            .map(|n| {
                consts
                    .get(&n)
                    .unwrap_or_else(|| panic!("no string literal for const {n}; parser is stale"))
                    .clone()
            })
            .collect()
    };

    for (header, cli_values) in [
        ("switch *o.Mode {", MODES),
        ("switch *o.Model {", MODELS),
        ("switch *o.Depth {", DEPTHS),
    ] {
        let server = resolve(go_accepted_case_consts(&src, header));
        let client: BTreeSet<String> = cli_values.iter().map(|s| s.to_string()).collect();
        assert_eq!(
            client,
            server,
            "CLI enum value-set for `{header}` drifted from the server's accepted \
             values: missing from CLI = {:?}, extra in CLI = {:?}",
            server.difference(&client).collect::<Vec<_>>(),
            client.difference(&server).collect::<Vec<_>>(),
        );
    }
}
