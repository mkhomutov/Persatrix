//! Tests for [`super`] (the [`crate::commands::channel_config`] module).
//! Wired in via `#[path = "channel_config_tests.rs"] mod tests;` so
//! `channel_config.rs` stays under the 500-line file-size cap (same
//! convention as `channel_tests.rs`). Pure-helper + lockstep coverage;
//! the HTTP dispatch paths are exercised live against a toggle-on server.

use super::*;

// ─── wire DTOs ─────────────────────────────────────────────────────

#[test]
fn channel_config_view_deserializes_full_payload() {
    // GET/PATCH /api/v1/channels/{id}/config — every governed knob carries its
    // effective `value` + `source` provenance, plus the channel revision.
    // Mirrors the Go `channelConfigResponse` shape.
    let json = serde_json::json!({
        "revision": 3,
        "floor_control":                                {"value": true,  "source": "channel"},
        "salience_max_channel_members":                 {"value": 8,     "source": "default"},
        "max_replies_per_participant_per_interaction":  {"value": 2,     "source": "channel"},
        "end_vote_threshold":                           {"value": 3,     "source": "default"},
        "end_vote_window":                              {"value": 5,     "source": "default"},
        "escalation_chair_id":                          {"value": "ada", "source": "channel"},
        "interaction_idle_timeout_seconds":             {"value": 600,   "source": "default"},
        "interaction_budget_tokens":                    {"value": null,  "source": "default"},
    });
    let view: ChannelConfigView = serde_json::from_value(json).unwrap();
    assert_eq!(view.revision, 3);
    assert_eq!(view.floor_control.value, serde_json::json!(true));
    assert_eq!(view.floor_control.source, "channel");
    assert_eq!(view.escalation_chair_id.value, serde_json::json!("ada"));
    // A JSON null value (not an absent field) deserializes to Value::Null rather
    // than being dropped — the dynamic cell tolerates a null knob defensively.
    assert!(view.interaction_budget_tokens.value.is_null());
    assert_eq!(view.interaction_budget_tokens.source, "default");
}

// ─── parse_set_assignment ──────────────────────────────────────────

#[test]
fn parse_set_assignment_coerces_per_knob_type() {
    // bool / int / string knobs each coerce to the matching JSON type.
    assert_eq!(
        parse_set_assignment("floor_control=true").unwrap(),
        ("floor_control".to_string(), Value::Bool(true))
    );
    assert_eq!(
        parse_set_assignment("end_vote_window=5").unwrap(),
        ("end_vote_window".to_string(), serde_json::json!(5))
    );
    assert_eq!(
        parse_set_assignment("escalation_chair_id=ada").unwrap(),
        (
            "escalation_chair_id".to_string(),
            Value::String("ada".into())
        )
    );
}

#[test]
fn parse_set_assignment_trims_surrounding_whitespace() {
    // `--` shell-quoting can leave padding; trim both halves.
    let (k, v) = parse_set_assignment("  floor_control = false ").unwrap();
    assert_eq!(k, "floor_control");
    assert_eq!(v, Value::Bool(false));
}

#[test]
fn parse_set_assignment_rejects_missing_equals() {
    let err = parse_set_assignment("floor_control").unwrap_err();
    assert!(err.contains("key=value"), "explains the shape: {err}");
}

#[test]
fn parse_set_assignment_rejects_unknown_knob() {
    let err = parse_set_assignment("turbo_mode=true").unwrap_err();
    assert!(err.contains("turbo_mode"), "names the bad knob: {err}");
    assert!(err.contains("floor_control"), "lists the vocabulary: {err}");
}

#[test]
fn parse_set_assignment_rejects_wrong_typed_value() {
    // A non-boolean for a bool knob and a non-integer for an int knob both
    // fail locally, naming the knob — not a round-tripped 400.
    let b = parse_set_assignment("floor_control=maybe").unwrap_err();
    assert!(b.contains("floor_control") && b.contains("boolean"), "{b}");
    let i = parse_set_assignment("end_vote_window=two").unwrap_err();
    assert!(
        i.contains("end_vote_window") && i.contains("integer"),
        "{i}"
    );
}

#[test]
fn parse_set_assignment_rejects_fractional_for_int_knob() {
    // i64 parse refuses a fractional literal — mirrors the server's strict
    // integer decode (json.Unmarshal won't coerce 1.5 into an int).
    assert!(parse_set_assignment("salience_max_channel_members=1.5").is_err());
}

#[test]
fn parse_set_assignment_accepts_negative_and_large_ints() {
    // The CLI does not impose value ranges — the server validates bounds.
    // It only enforces the wire TYPE, so a negative or 64-bit value parses.
    assert_eq!(
        parse_set_assignment("interaction_budget_tokens=5000000000")
            .unwrap()
            .1,
        serde_json::json!(5_000_000_000_i64)
    );
    assert_eq!(
        parse_set_assignment("end_vote_threshold=-1").unwrap().1,
        serde_json::json!(-1)
    );
}

#[test]
fn parse_set_assignment_keeps_empty_string_for_string_knob() {
    // `escalation_chair_id=` is NOT a typo: the empty string is the server's
    // explicit "disable escalation" override, distinct from `unset` (inherit).
    // The CLI must be able to express it, so the string knob keeps an empty value
    // rather than rejecting it. (ChannelConfigOverrides.EscalationChairID:
    // "An explicit empty string disables escalation; nil inherits.")
    assert_eq!(
        parse_set_assignment("escalation_chair_id=").unwrap(),
        (
            "escalation_chair_id".to_string(),
            Value::String(String::new())
        )
    );
    // Trimmed-to-empty is the same: whitespace-only is still the disable sentinel.
    assert_eq!(
        parse_set_assignment("escalation_chair_id=   ").unwrap(),
        (
            "escalation_chair_id".to_string(),
            Value::String(String::new())
        )
    );
}

#[test]
fn parse_set_assignment_rejects_empty_value_for_numeric_knob_pointing_at_unset() {
    // There is no empty int/bool, so an empty value for a numeric/boolean knob is
    // a typo — rejected with a steer to `unset` (the clear-to-inherit path).
    let err = parse_set_assignment("end_vote_window=").unwrap_err();
    assert!(err.contains("unset"), "points at the unset verb: {err}");
    let b = parse_set_assignment("floor_control=").unwrap_err();
    assert!(b.contains("unset"), "bool knob too: {b}");
}

// ─── build_set_patch ───────────────────────────────────────────────

#[test]
fn build_set_patch_collects_multiple_assignments() {
    let specs = vec![
        "floor_control=true".to_string(),
        "end_vote_window=5".to_string(),
    ];
    let patch = build_set_patch(&specs).unwrap();
    assert_eq!(patch.len(), 2);
    assert_eq!(patch["floor_control"], Value::Bool(true));
    assert_eq!(patch["end_vote_window"], serde_json::json!(5));
}

#[test]
fn build_set_patch_rejects_empty() {
    assert!(build_set_patch(&[]).is_err());
}

#[test]
fn build_set_patch_rejects_duplicate_knob() {
    // Setting the same knob twice in one command is ambiguous — reject
    // rather than let serde_json's last-wins silently pick.
    let specs = vec![
        "floor_control=true".to_string(),
        "floor_control=false".to_string(),
    ];
    let err = build_set_patch(&specs).unwrap_err();
    assert!(err.contains("more than once"), "{err}");
}

// ─── build_unset_patch ─────────────────────────────────────────────

#[test]
fn build_unset_patch_maps_each_key_to_null() {
    let keys = vec![
        "floor_control".to_string(),
        "escalation_chair_id".to_string(),
    ];
    let patch = build_unset_patch(&keys).unwrap();
    assert_eq!(patch.len(), 2);
    assert_eq!(patch["floor_control"], Value::Null);
    assert_eq!(patch["escalation_chair_id"], Value::Null);
}

#[test]
fn build_unset_patch_rejects_empty_and_unknown_and_dupe() {
    assert!(build_unset_patch(&[]).is_err());
    assert!(build_unset_patch(&["turbo_mode".to_string()]).is_err());
    let dupe = vec!["floor_control".to_string(), "floor_control".to_string()];
    assert!(build_unset_patch(&dupe)
        .unwrap_err()
        .contains("more than once"));
}

// ─── render_value ──────────────────────────────────────────────────

#[test]
fn render_value_dashes_null_and_unquotes_string() {
    // An inherited interaction_budget reports JSON null — render as `—`, not
    // the literal "null". Strings drop their quotes; numbers/bools stay.
    assert_eq!(render_value(&Value::Null), "\u{2014}");
    assert_eq!(render_value(&Value::String("ada".into())), "ada");
    assert_eq!(render_value(&serde_json::json!(600)), "600");
    assert_eq!(render_value(&Value::Bool(true)), "true");
}

#[test]
fn render_value_names_empty_string_rather_than_blanking() {
    // The one string knob (escalation_chair_id) renders empty as `(none)`, not a
    // blank cell that reads as a missing field — the empty value is a real state
    // (no chair / escalation disabled). Kept distinct from `—` (a JSON null);
    // the row's [channel]/[default] tag separates an explicit disable from an
    // inherited no-chair.
    assert_eq!(render_value(&Value::String(String::new())), "(none)");
    assert_ne!(render_value(&Value::String(String::new())), "\u{2014}");
}

// ─── conflict_hint (409 recovery copy) ─────────────────────────────

#[test]
fn conflict_hint_points_at_reread() {
    // A stale-revision 409 must steer the operator to re-read and retry, and must
    // preserve the server's own message. This is the only automated guard on the
    // hint copy — the HTTP path that emits it is live-verified only.
    let hint = conflict_hint("409 Conflict: revision mismatch");
    assert!(hint.contains("409 Conflict: revision mismatch"), "{hint}");
    assert!(
        hint.contains("channel config get"),
        "names the re-read verb: {hint}"
    );
    assert!(hint.contains("retry"), "{hint}");
}

// ─── passthrough_json (--json fidelity) ────────────────────────────

#[test]
fn passthrough_json_preserves_unmodeled_server_fields() {
    // `--json` echoes the server body verbatim rather than re-serializing the
    // typed view, so a field the server reports that the CLI does not model
    // survives into `--json` (a typed round-trip would silently drop it). The
    // server's encoder appends a newline; that one is trimmed.
    let raw = "{\"revision\":1,\"future_knob\":{\"value\":7,\"source\":\"channel\"}}\n";
    let out = passthrough_json(raw);
    assert!(
        out.contains("future_knob"),
        "unmodeled field survives: {out}"
    );
    assert_eq!(out, raw.trim_end(), "verbatim minus trailing newline");
}

// ─── config_rows ───────────────────────────────────────────────────

#[test]
fn config_rows_covers_every_knob_in_registry_order() {
    // config_rows drives off CONFIG_KNOBS, so the render set is exactly the
    // editable set, in declaration order. This also exercises the match arm
    // for every knob (an unmatched key would `unreachable!`).
    let view: ChannelConfigView = serde_json::from_value(serde_json::json!({
        "revision": 0,
        "floor_control": {"value": false, "source": "default"},
        "salience_max_channel_members": {"value": 8, "source": "default"},
        "max_replies_per_participant_per_interaction": {"value": 2, "source": "default"},
        "end_vote_threshold": {"value": 3, "source": "default"},
        "end_vote_window": {"value": 5, "source": "default"},
        "escalation_chair_id": {"value": "", "source": "default"},
        "interaction_idle_timeout_seconds": {"value": 600, "source": "default"},
        "interaction_budget_tokens": {"value": null, "source": "default"},
    }))
    .unwrap();
    let rows = config_rows(&view);
    let labels: Vec<&str> = rows.iter().map(|(k, _)| *k).collect();
    let expected: Vec<&str> = CONFIG_KNOBS.iter().map(|(k, _)| *k).collect();
    assert_eq!(labels, expected);
}

// ─── lockstep guard: CLI knob set == server merge switch ────────────

// TRUE lockstep guard — the same shape as `respond_policy_covers_server_
// vocabulary` in channel_dispatch. The regression this prevents is "the CLI
// editable-knob set drifts from the server's closed set". A hardcoded list
// can't catch that — it stays green when the server grows a knob. So we parse
// the `case "<knob>":` labels straight out of the server's mergeConfigPatch
// and assert the CLI registry covers exactly that set. Add a knob server-side
// and forget the CLI, and this fails with the missing token named.
#[test]
fn cli_knob_set_matches_server_merge_switch() {
    use std::collections::BTreeSet;

    let go_src = std::fs::read_to_string(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../internal/server/channel_config_handlers.go"
    ))
    .expect(
        "channel_config_handlers.go must be readable for the knob lockstep \
             guard; update the path if it moved",
    );

    // mergeConfigPatch's switch has one `case "<knob>":` per editable knob.
    // The `default:` arm (unknown knob) carries no quoted token, so a trimmed
    // line starting with `case "` and bearing a quoted token selects exactly
    // the knob cases.
    let server: BTreeSet<String> = go_src
        .lines()
        .map(str::trim_start)
        .filter(|l| l.starts_with("case \""))
        .filter_map(|l| {
            let start = l.find('"')? + 1;
            let end = l[start..].find('"')? + start;
            Some(l[start..end].to_string())
        })
        .collect();

    assert!(
        !server.is_empty(),
        "parsed no `case \"<knob>\"` labels from channel_config_handlers.go — \
             the switch format changed; update this guard's parser",
    );

    let client: BTreeSet<String> = CONFIG_KNOBS.iter().map(|(k, _)| k.to_string()).collect();

    assert_eq!(
        client,
        server,
        "CLI config-knob set drifted from server mergeConfigPatch: \
             missing from CLI = {:?}, extra in CLI = {:?}",
        server.difference(&client).collect::<Vec<_>>(),
        client.difference(&server).collect::<Vec<_>>(),
    );
}

// The sibling guard above is name-only: it stays green if the server *retypes* a
// knob (e.g. an int knob becomes a string) while keeping its name. But the CLI's
// reason to exist is mirroring the server's wire TYPE so a wrong-typed value
// fails before the round-trip — a guarantee that silently rots under a name-only
// check. So we also pin each knob's CLI [`KnobType`] against the Go type the
// server's `decodeKnob[T]` instantiates for that case. Go `int`/`int64` both map
// to the CLI's single integer type.
#[test]
fn cli_knob_types_match_server_decode_types() {
    use std::collections::BTreeMap;

    let go_src = std::fs::read_to_string(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../internal/server/channel_config_handlers.go"
    ))
    .expect(
        "channel_config_handlers.go must be readable for the knob-type lockstep \
             guard; update the path if it moved",
    );

    // Walk mergeConfigPatch: a `case "<knob>":` opens a knob arm; the first
    // `decodeKnob[<goType>]` that follows is that knob's wire type (the null-unset
    // branch returns before the decode, so first-after-case is unambiguous). The
    // `func decodeKnob[T any]` definition also contains `decodeKnob[` but is
    // reached with no open case, so it is skipped.
    let mut server: BTreeMap<String, &str> = BTreeMap::new();
    let mut current: Option<String> = None;
    for line in go_src.lines().map(str::trim_start) {
        if let Some(rest) = line.strip_prefix("case \"") {
            if let Some(end) = rest.find('"') {
                current = Some(rest[..end].to_string());
            }
        } else if let Some(open) = line.find("decodeKnob[") {
            if let Some(knob) = current.take() {
                let ty = &line[open + "decodeKnob[".len()..];
                let ty = &ty[..ty.find(']').expect("decodeKnob[ must close with ]")];
                let class = match ty {
                    "bool" => "bool",
                    "string" => "string",
                    "int" | "int64" => "int",
                    other => panic!("unmapped server knob type {other:?}; extend the map"),
                };
                server.insert(knob, class);
            }
        }
    }

    assert!(
        !server.is_empty(),
        "parsed no knob decode types from channel_config_handlers.go — the \
             case/decodeKnob format changed; update this guard's parser",
    );

    let client: BTreeMap<String, &str> = CONFIG_KNOBS
        .iter()
        .map(|(k, t)| {
            let class = match t {
                KnobType::Bool => "bool",
                KnobType::Int => "int",
                KnobType::Str => "string",
            };
            (k.to_string(), class)
        })
        .collect();

    assert_eq!(
        client, server,
        "CLI knob wire-types drifted from server decodeKnob types (knob → type)",
    );
}
