//! Tests for [`super`] (the [`crate::commands::channel_config_yaml`] module).
//! Wired in via `#[path = "channel_config_yaml_tests.rs"] mod tests;` so the
//! production file stays under the 500-line file-size cap (same convention as
//! `channel_config_tests.rs`). Pure-helper coverage — the HTTP dispatch paths
//! (`export`/`import`/`diff` against a toggle-on server) are live-verified.

use super::*;

// A `ChannelConfigView` built from a JSON payload, mirroring how the wire DTO
// arrives off `GET …/config`. `over` lists the knobs whose `source` is
// `"channel"` (an explicit override); every other knob reads `"default"`.
fn view(revision: i64, over: &[(&str, Value)]) -> ChannelConfigView {
    // Effective defaults for the inherit case; overridden entries replace these.
    let mut knobs: serde_json::Map<String, Value> = serde_json::Map::new();
    let defaults = [
        ("floor_control", serde_json::json!(true)),
        ("salience_max_channel_members", serde_json::json!(20)),
        (
            "max_replies_per_participant_per_interaction",
            serde_json::json!(0),
        ),
        ("end_vote_threshold", serde_json::json!(2)),
        ("end_vote_window", serde_json::json!(3)),
        ("escalation_chair_id", serde_json::json!("")),
        ("interaction_idle_timeout_seconds", serde_json::json!(600)),
        ("interaction_budget_tokens", serde_json::json!(null)),
    ];
    let over_map: std::collections::HashMap<&str, &Value> =
        over.iter().map(|(k, v)| (*k, v)).collect();
    for (knob, def) in defaults {
        let (value, source) = match over_map.get(knob) {
            Some(v) => ((*v).clone(), "channel"),
            None => (def, "default"),
        };
        knobs.insert(
            knob.to_string(),
            serde_json::json!({"value": value, "source": source}),
        );
    }
    let mut payload = serde_json::Map::new();
    payload.insert("revision".to_string(), serde_json::json!(revision));
    payload.extend(knobs);
    serde_json::from_value(Value::Object(payload)).expect("view payload deserializes")
}

// ─── yaml_to_knob_json ─────────────────────────────────────────────────────

#[test]
fn yaml_to_knob_json_coerces_each_wire_type() {
    assert_eq!(
        yaml_to_knob_json("floor_control", KnobType::Bool, &Yaml::from(true)).unwrap(),
        serde_json::json!(true)
    );
    assert_eq!(
        yaml_to_knob_json("end_vote_window", KnobType::Int, &Yaml::from(5)).unwrap(),
        serde_json::json!(5)
    );
    assert_eq!(
        yaml_to_knob_json("escalation_chair_id", KnobType::Str, &Yaml::from("ada")).unwrap(),
        serde_json::json!("ada")
    );
}

#[test]
fn yaml_to_knob_json_rejects_wrong_type_naming_the_knob() {
    // The classic config-edit foot-guns: a bool knob given a word, an int knob
    // given a string. Each fails locally with the knob named.
    let bad_bool = yaml_to_knob_json("floor_control", KnobType::Bool, &Yaml::from("maybe"));
    assert!(bad_bool.unwrap_err().contains("floor_control"));
    let bad_int = yaml_to_knob_json("end_vote_window", KnobType::Int, &Yaml::from("two"));
    assert!(bad_int.unwrap_err().contains("end_vote_window"));
    // A YAML scalar that parses as bool must NOT slip into the string knob.
    let bad_str = yaml_to_knob_json("escalation_chair_id", KnobType::Str, &Yaml::from(true));
    assert!(bad_str.unwrap_err().contains("escalation_chair_id"));
}

// ─── parse_channel_block ───────────────────────────────────────────────────

fn yaml_block(text: &str) -> Yaml {
    serde_yml::from_str(text).expect("test YAML parses")
}

#[test]
fn parse_channel_block_lifts_name_revision_and_knobs() {
    let block = yaml_block(
        "name: planning\nrevision: 4\nfloor_control: false\nend_vote_window: 5\n\
         escalation_chair_id: nova-sparrow\n",
    );
    let parsed = parse_channel_block(&block).unwrap();
    assert_eq!(parsed.id, "group:planning");
    assert_eq!(parsed.revision, Some(4));
    assert_eq!(
        parsed.patch.get("floor_control"),
        Some(&serde_json::json!(false))
    );
    assert_eq!(
        parsed.patch.get("end_vote_window"),
        Some(&serde_json::json!(5))
    );
    assert_eq!(
        parsed.patch.get("escalation_chair_id"),
        Some(&serde_json::json!("nova-sparrow"))
    );
    assert_eq!(parsed.patch.len(), 3);
}

#[test]
fn parse_channel_block_ignores_non_knob_keys_and_absent_revision() {
    // The real config/channels.yaml block carries description + members and may
    // predate RFC 0050 (no `revision:`). Neither is a governance knob; both are
    // ignored, and an absent revision reads as None (not an error).
    let block = yaml_block(
        "name: planning\ndescription: demo\nmembers:\n  - id: ada\n    respond: participant\n\
         salience_max_channel_members: 8\n",
    );
    let parsed = parse_channel_block(&block).unwrap();
    assert_eq!(parsed.revision, None);
    assert_eq!(parsed.patch.len(), 1);
    assert_eq!(
        parsed.patch.get("salience_max_channel_members"),
        Some(&serde_json::json!(8))
    );
}

#[test]
fn parse_channel_block_skips_null_knob_as_inherit() {
    // An explicit YAML `null` is inherit, not a sparse-patch entry (unset is its
    // own verb) — so it must not appear in the patch.
    let block = yaml_block("name: planning\nfloor_control: ~\nend_vote_window: 5\n");
    let parsed = parse_channel_block(&block).unwrap();
    assert!(!parsed.patch.contains_key("floor_control"));
    assert_eq!(parsed.patch.len(), 1);
}

#[test]
fn parse_channel_block_qualified_id_passes_through() {
    let block = yaml_block("name: group:planning\nfloor_control: true\n");
    assert_eq!(parse_channel_block(&block).unwrap().id, "group:planning");
}

#[test]
fn parse_channel_block_rejects_missing_name_and_bad_types() {
    assert!(parse_channel_block(&yaml_block("floor_control: true\n"))
        .unwrap_err()
        .contains("name"));
    assert!(
        parse_channel_block(&yaml_block("name: planning\nrevision: soon\n"))
            .unwrap_err()
            .contains("revision")
    );
    assert!(
        parse_channel_block(&yaml_block("name: planning\nend_vote_window: two\n"))
            .unwrap_err()
            .contains("end_vote_window")
    );
    assert!(parse_channel_block(&yaml_block("- not\n- a\n- mapping\n"))
        .unwrap_err()
        .contains("mapping"));
}

// ─── parse_channels_doc ────────────────────────────────────────────────────

#[test]
fn parse_channels_doc_reads_every_block() {
    let doc = "channels:\n  - name: planning\n    floor_control: false\n  - name: ops\n    \
               end_vote_threshold: 3\n";
    let parsed = parse_channels_doc(doc).unwrap();
    assert_eq!(parsed.len(), 2);
    assert_eq!(parsed[0].id, "group:planning");
    assert_eq!(parsed[1].id, "group:ops");
    assert_eq!(
        parsed[1].patch.get("end_vote_threshold"),
        Some(&serde_json::json!(3))
    );
}

#[test]
fn parse_channels_doc_rejects_missing_or_empty_channels() {
    assert!(parse_channels_doc("max_channels: 50\n")
        .unwrap_err()
        .contains("channels"));
    assert!(parse_channels_doc("channels: []\n")
        .unwrap_err()
        .contains("empty"));
    assert!(parse_channels_doc(": : not yaml : :")
        .unwrap_err()
        .to_lowercase()
        .contains("yaml"));
}

// ─── render_export_doc ─────────────────────────────────────────────────────

#[test]
fn render_export_emits_only_overrides_stamped_next_revision() {
    // revision 3, two explicit overrides; the rest inherit. Export must carry
    // ONLY the two overrides, stamped revision 4, and must not freeze inherited
    // defaults into explicit overrides.
    let v = view(
        3,
        &[
            ("floor_control", serde_json::json!(false)),
            ("end_vote_window", serde_json::json!(5)),
        ],
    );
    let doc = render_export_doc("planning", &v).unwrap();

    // Round-trip: parsing the export back yields exactly the override patch at
    // revision store+1 — the strongest guarantee export and import agree.
    let reparsed = parse_channels_doc(&doc).unwrap();
    assert_eq!(reparsed.len(), 1);
    assert_eq!(reparsed[0].id, "group:planning");
    assert_eq!(reparsed[0].revision, Some(4));
    assert_eq!(reparsed[0].patch.len(), 2);
    assert_eq!(
        reparsed[0].patch.get("floor_control"),
        Some(&serde_json::json!(false))
    );
    assert_eq!(
        reparsed[0].patch.get("end_vote_window"),
        Some(&serde_json::json!(5))
    );
    // Inherited knobs are absent — not frozen.
    assert!(!reparsed[0]
        .patch
        .contains_key("salience_max_channel_members"));
    assert!(!doc.contains("salience_max_channel_members"));
}

#[test]
fn render_export_with_no_overrides_is_inherit_only_block() {
    let doc = render_export_doc("planning", &view(7, &[])).unwrap();
    let reparsed = parse_channels_doc(&doc).unwrap();
    assert_eq!(reparsed[0].revision, Some(8));
    assert!(reparsed[0].patch.is_empty());
}

// ─── diff_rows / has_drift ─────────────────────────────────────────────────

fn row<'a>(rows: &'a [DiffRow], knob: &str) -> &'a DiffRow {
    rows.iter().find(|r| r.knob == knob).expect("knob present")
}

#[test]
fn diff_rows_classifies_each_knob() {
    // Effective: floor_control overridden false, end_vote_window overridden 5,
    // everything else inherited (incl. the null deferred budget).
    let v = view(
        4,
        &[
            ("floor_control", serde_json::json!(false)),
            ("end_vote_window", serde_json::json!(5)),
        ],
    );
    // Declared: floor_control matches (in sync), end_vote_window differs (drift),
    // a declared budget against the null effective (indeterminate); the rest
    // undeclared (inherited).
    let mut declared = Map::new();
    declared.insert("floor_control".into(), serde_json::json!(false));
    declared.insert("end_vote_window".into(), serde_json::json!(9));
    declared.insert("interaction_budget_tokens".into(), serde_json::json!(1000));
    let rows = diff_rows(&declared, &v);

    assert_eq!(rows.len(), 8); // every knob, registry order
    assert_eq!(row(&rows, "floor_control").status, DiffStatus::InSync);
    assert_eq!(row(&rows, "end_vote_window").status, DiffStatus::Drift);
    assert_eq!(
        row(&rows, "interaction_budget_tokens").status,
        DiffStatus::Indeterminate
    );
    assert_eq!(
        row(&rows, "end_vote_threshold").status,
        DiffStatus::Inherited
    );
    assert!(has_drift(&rows));
}

#[test]
fn diff_rows_no_drift_when_all_in_sync_or_inherited() {
    let v = view(1, &[("end_vote_window", serde_json::json!(5))]);
    let mut declared = Map::new();
    declared.insert("end_vote_window".into(), serde_json::json!(5)); // matches
    let rows = diff_rows(&declared, &v);
    assert_eq!(row(&rows, "end_vote_window").status, DiffStatus::InSync);
    assert!(!has_drift(&rows));
}

#[test]
fn diff_rows_flags_undeclared_store_override_as_drift() {
    // The store carries an explicit `floor_control` override (`source ==
    // "channel"`, e.g. a live `config set`), but the YAML declares nothing for
    // it. The boot reconcile REPLACES the whole override blob with the declared
    // set (ReconcileChannelConfig is a full-blob write), so this override would
    // be cleared on boot — real drift, NOT a benign "inherited". A knob with no
    // store override stays Inherited.
    let v = view(5, &[("floor_control", serde_json::json!(false))]);
    let declared = Map::new(); // YAML omits every knob
    let rows = diff_rows(&declared, &v);
    assert_eq!(row(&rows, "floor_control").status, DiffStatus::Undeclared);
    assert_eq!(row(&rows, "end_vote_window").status, DiffStatus::Inherited);
    assert!(has_drift(&rows));
}

#[test]
fn diff_status_machine_tags_are_stable() {
    // `--json` serializes these tags, so they are a wire contract decoupled from
    // the Rust variant names — a variant rename must not silently change them.
    assert_eq!(DiffStatus::InSync.tag(), "in_sync");
    assert_eq!(DiffStatus::Drift.tag(), "drift");
    assert_eq!(DiffStatus::Inherited.tag(), "inherited");
    assert_eq!(DiffStatus::Indeterminate.tag(), "deferred");
    assert_eq!(DiffStatus::Undeclared.tag(), "undeclared");
}

// ─── validate_channel_ids ──────────────────────────────────────────────────

#[test]
fn validate_channel_ids_rejects_malformed_id_before_any_write() {
    // `import` must validate every channel id up front: a malformed `name:` in a
    // later block has to abort before the first PATCH, not after earlier blocks
    // were already written.
    let good =
        parse_channels_doc("channels:\n  - name: planning\n    floor_control: true\n").unwrap();
    assert!(validate_channel_ids(&good).is_ok());

    let bad = parse_channels_doc(
        "channels:\n  - name: planning\n    floor_control: true\n  - name: bad/name\n    \
         floor_control: true\n",
    )
    .unwrap();
    assert!(validate_channel_ids(&bad)
        .unwrap_err()
        .contains("channel id"));
}
