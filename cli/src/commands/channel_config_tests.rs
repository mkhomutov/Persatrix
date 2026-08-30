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
        "reasoning": {
            "mode":   {"value": "bid",     "source": "channel"},
            "model":  {"value": "fast",    "source": "default"},
            "depth":  {"value": "shallow", "source": "default"},
            "revise": {"value": 0,         "source": "default"},
        },
        "autonomous": {
            "enabled":    {"value": true,             "source": "channel"},
            "topic":      {"value": "Monorepo?",      "source": "channel"},
            "agenda":     {"value": ["Cost"],         "source": "channel"},
            "convener":   {"value": "nova-sparrow",   "source": "channel"},
            "goal":       {"value": "",               "source": "default"},
            "max_rounds": {"value": 12,               "source": "default"},
            "schedule_interval_seconds": {"value": 0, "source": "default"},
            "max_convenings":            {"value": 0, "source": "default"},
            "standing_budget_tokens":    {"value": 0, "source": "default"},
        },
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
    // The nested reasoning block deserializes per-sub-knob, each with its own
    // value + provenance (mode overridden here, the rest inherited).
    assert_eq!(view.reasoning.mode.value, serde_json::json!("bid"));
    assert_eq!(view.reasoning.mode.source, "channel");
    assert_eq!(view.reasoning.model.value, serde_json::json!("fast"));
    assert_eq!(view.reasoning.model.source, "default");
    assert_eq!(view.reasoning.revise.value, serde_json::json!(0));
    // The nested autonomous block deserializes per-sub-knob too, including the
    // list-valued `agenda` cell (a JSON array, not a flattened string).
    assert_eq!(view.autonomous.enabled.value, serde_json::json!(true));
    assert_eq!(view.autonomous.enabled.source, "channel");
    assert_eq!(view.autonomous.agenda.value, serde_json::json!(["Cost"]));
    assert_eq!(view.autonomous.max_rounds.value, serde_json::json!(12));
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

// (RFC 0051 reasoning: enum + dotted-key parse/build tests live in
// channel_config_reasoning_tests.rs, beside the module they exercise.)

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
    // config_rows drives off editable_knobs(), so the render set is exactly the
    // full editable set (flat + nested reasoning), in declaration order. This also
    // exercises the match arm for every knob (an unmatched key would `unreachable!`).
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
        "reasoning": {
            "mode":   {"value": "off",     "source": "default"},
            "model":  {"value": "fast",    "source": "default"},
            "depth":  {"value": "shallow", "source": "default"},
            "revise": {"value": 0,         "source": "default"},
        },
        "autonomous": {
            "enabled":    {"value": false, "source": "default"},
            "topic":      {"value": "",    "source": "default"},
            "agenda":     {"value": [],    "source": "default"},
            "convener":   {"value": "",    "source": "default"},
            "goal":       {"value": "",    "source": "default"},
            "max_rounds": {"value": 12,    "source": "default"},
            "schedule_interval_seconds": {"value": 0, "source": "default"},
            "max_convenings":            {"value": 0, "source": "default"},
            "standing_budget_tokens":    {"value": 0, "source": "default"},
        },
    }))
    .unwrap();
    let rows = config_rows(&view);
    let labels: Vec<&str> = rows.iter().map(|(k, _)| *k).collect();
    let expected: Vec<&str> = editable_knobs().map(|(k, _)| *k).collect();
    assert_eq!(labels, expected);
    // The dotted reasoning rows resolve to the nested view cells (also exercising
    // the reasoning.* match arms — an unmatched dotted key would `unreachable!`).
    let mode = rows.iter().find(|(k, _)| *k == "reasoning.mode").unwrap().1;
    assert_eq!(mode.value, serde_json::json!("off"));
    // The dotted autonomous rows resolve through the `.or_else(autonomous.field)`
    // arm — the list-valued agenda cell renders too (an unmatched key would panic).
    let agenda = rows
        .iter()
        .find(|(k, _)| *k == "autonomous.agenda")
        .unwrap()
        .1;
    assert_eq!(agenda.value, serde_json::json!([]));
}

// ─── lockstep guard: CLI knob set == server merge switches ──────────

/// Parse the server's COMPLETE leaf-knob set + wire types straight from the Go
/// source — the lockstep ground truth both guards below read. The regression this
/// prevents is "the CLI editable-knob set/types drift from the server's closed
/// set". A hardcoded list can't catch that; parsing the Go source means adding a
/// knob server-side and forgetting the CLI fails here with the token named.
///
/// `reasoning` made this span TWO switches (it is the first NESTED knob):
///   * `channel_config_handlers.go` — `mergeConfigPatch`. A `case "<knob>":` arm
///     that decodes a scalar (`decodeKnob[T]`) is a LEAF knob; the `case
///     "reasoning":` arm instead delegates to `mergeReasoningPatch` (a `…Patch(`
///     call, no `decodeKnob`), so it is a NAMESPACE, not a leaf — skipped here, its
///     sub-knobs come from the nested file.
///   * `channel_config_reasoning.go` — `mergeReasoningPatch`. Each
///     `decodeKnob[T]("reasoning.<sub>", …)` is one nested leaf knob whose CLI key
///     is the dotted literal in that first argument.
///   * `channel_config_autonomous.go` — `mergeAutonomousPatch` (RFC 0052). Same
///     nested shape as reasoning; its `decodeKnob[[]string]("autonomous.agenda", …)`
///     is the first SLICE-typed leaf, which maps to the CLI's `list` class.
///
/// The CLI registry mirrors the union as dotted keys, so the guards compare it
/// against this. Go `int`/`int64` both map to the CLI's single integer class; an
/// enum knob rides the wire as a `string` (`decodeKnob[string]`); a `[]string`
/// (the agenda list) maps to the CLI's `list` class.
fn server_leaf_knob_types() -> std::collections::BTreeMap<String, &'static str> {
    use std::collections::BTreeMap;

    fn class_of(go_type: &str) -> &'static str {
        match go_type {
            "bool" => "bool",
            "string" => "string",
            "int" | "int64" => "int",
            "[]string" => "list",
            other => panic!("unmapped server knob type {other:?}; extend the map"),
        }
    }
    // The `<T>` of a `decodeKnob[<T>]…` occurrence, given the slice starting at it.
    // Walks to the bracket that BALANCES the opening one, so a slice type whose own
    // `[]` nests inside (`decodeKnob[[]string]`) yields `[]string`, not a truncated
    // `[`. A plain `[bool]` closes at the first `]` exactly as before.
    fn decode_type(at_kw: &str) -> &str {
        let open = at_kw.find('[').expect("decodeKnob must be followed by [");
        let bytes = at_kw.as_bytes();
        let mut depth = 0usize;
        for (i, &b) in bytes.iter().enumerate().skip(open) {
            match b {
                b'[' => depth += 1,
                b']' => {
                    depth -= 1;
                    if depth == 0 {
                        return &at_kw[open + 1..i];
                    }
                }
                _ => {}
            }
        }
        panic!("decodeKnob[ must close with ]");
    }
    let read = |rel: &str| {
        std::fs::read_to_string(format!("{}/../{}", env!("CARGO_MANIFEST_DIR"), rel)).unwrap_or_else(
            |e| panic!("{rel} must be readable for the knob lockstep guard ({e}); update the path if it moved"),
        )
    };

    let mut server: BTreeMap<String, &'static str> = BTreeMap::new();

    // Flat knobs — mergeConfigPatch's switch (own file since #810's cap split).
    let handlers = read("internal/server/channel_config_merge.go");
    let mut current: Option<String> = None;
    for line in handlers.lines().map(str::trim_start) {
        if let Some(rest) = line.strip_prefix("case \"") {
            current = rest.find('"').map(|e| rest[..e].to_string());
        } else if line.starts_with("default:") {
            current = None;
        } else if let Some(open) = line.find("decodeKnob[") {
            // First `decodeKnob[<T>]` in an open arm is that knob's wire type (the
            // null-unset branch returns before the decode). The `func decodeKnob[T
            // any]` definition also matches but is reached with no open case.
            if let Some(knob) = current.take() {
                server.insert(knob, class_of(decode_type(&line[open..])));
            }
        } else if line.contains("Patch(") && current.is_some() {
            // A namespace arm (e.g. `reasoning` → mergeReasoningPatch): no
            // decodeKnob, its leaves come from the nested file. Drop the namespace
            // name without recording it as a leaf knob.
            current = None;
        }
    }

    let flat_count = server.len();
    // Nested sub-knobs — each `merge<Block>Patch`'s decodeKnob lines, whose first
    // string-literal argument IS the dotted key (e.g. "reasoning.mode",
    // "autonomous.agenda"). Both nested files share one shape, so parse them the
    // same way: reasoning (RFC 0051) and autonomous (RFC 0052).
    for rel in [
        "internal/server/channel_config_reasoning.go",
        "internal/server/channel_config_autonomous.go",
    ] {
        let nested = read(rel);
        for line in nested.lines().map(str::trim_start) {
            let Some(open) = line.find("decodeKnob[") else {
                continue;
            };
            let at_kw = &line[open..];
            let ty = decode_type(at_kw);
            let Some(q1) = at_kw.find('"') else { continue };
            let tail = &at_kw[q1 + 1..];
            let Some(q2) = tail.find('"') else { continue };
            server.insert(tail[..q2].to_string(), class_of(ty));
        }
    }

    // Per-HALF, not a bare `!server.is_empty()`: at #810 the flat parse returned
    // ZERO yet the nested knobs satisfied that, so the guard silently went blind.
    assert!(
        flat_count > 0 && server.len() > flat_count,
        "parsed no {} knobs — a merge switch moved file or changed shape; \
             update this guard's paths (v0.3.13 release-prep PR 1 finding)",
        if flat_count == 0 { "FLAT" } else { "NESTED" },
    );
    server
}

// TRUE lockstep guard — the same shape as `respond_policy_covers_server_
// vocabulary` in channel_dispatch: the CLI editable-knob set must equal the
// server's closed leaf set (flat mergeConfigPatch knobs ∪ nested reasoning.*).
#[test]
fn cli_knob_set_matches_server_merge_switch() {
    use std::collections::BTreeSet;

    let server: BTreeSet<String> = server_leaf_knob_types().into_keys().collect();
    let client: BTreeSet<String> = editable_knobs().map(|(k, _)| k.to_string()).collect();

    assert_eq!(
        client,
        server,
        "CLI config-knob set drifted from the server merge switches \
             (flat mergeConfigPatch + nested mergeReasoningPatch + mergeAutonomousPatch): \
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
// server's `decodeKnob[T]` instantiates for it. An enum knob rides the wire as a
// `string`, so it maps to the `string` class here.
#[test]
fn cli_knob_types_match_server_decode_types() {
    use std::collections::BTreeMap;

    let server = server_leaf_knob_types();
    let client: BTreeMap<String, &str> = editable_knobs()
        .map(|(k, t)| {
            let class = match t {
                KnobType::Bool => "bool",
                KnobType::Int => "int",
                KnobType::Str => "string",
                KnobType::Enum(_) => "string",
                KnobType::List => "list",
            };
            (k.to_string(), class)
        })
        .collect();

    assert_eq!(
        client, server,
        "CLI knob wire-types drifted from server decodeKnob types (knob → type)",
    );
}
