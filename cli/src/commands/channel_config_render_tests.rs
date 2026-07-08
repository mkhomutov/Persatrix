//! Tests for [`super`] (the [`crate::commands::channel_config_render`] module).
//! Wired in via `#[path = "channel_config_render_tests.rs"] mod tests;`. These
//! moved here with `render_value` when RFC 0052 PR 2 split the presentation
//! helpers out of `channel_config` to keep it under the 500-line cap.

use super::*;
use serde_json::Value;

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

#[test]
fn render_value_joins_agenda_list_and_names_empty() {
    // A list knob (autonomous.agenda) reads as a human row (items joined by
    // ", "), not raw JSON; an empty list reads `(none)`, paralleling the empty
    // string — "no agenda items", with the provenance tag carrying the rest.
    assert_eq!(
        render_value(&serde_json::json!(["Build cost", "Coupling risk"])),
        "Build cost, Coupling risk"
    );
    assert_eq!(render_value(&serde_json::json!([])), "(none)");
}

// ─── autonomous_runtime_summary (RFC 0052 §E convening readout) ─────────

/// A full config payload with `autonomous.enabled` + `autonomous_runtime` set by
/// the caller; every other knob carries a harmless default so the required
/// [`ChannelConfigView`] fields all deserialize.
fn sample_view(enabled: bool, runtime: serde_json::Value) -> ChannelConfigView {
    let json = serde_json::json!({
        "revision": 0,
        "floor_control": {"value": true, "source": "default"},
        "salience_max_channel_members": {"value": 8, "source": "default"},
        "max_replies_per_participant_per_interaction": {"value": 2, "source": "default"},
        "end_vote_threshold": {"value": 3, "source": "default"},
        "end_vote_window": {"value": 5, "source": "default"},
        "escalation_chair_id": {"value": "bob", "source": "channel"},
        "interaction_idle_timeout_seconds": {"value": 600, "source": "default"},
        "interaction_budget_tokens": {"value": 4000, "source": "channel"},
        "reasoning": {
            "mode": {"value": "bid", "source": "default"},
            "model": {"value": "fast", "source": "default"},
            "depth": {"value": "shallow", "source": "default"},
            "revise": {"value": 0, "source": "default"},
        },
        "autonomous": {
            "enabled": {"value": enabled, "source": "channel"},
            "topic": {"value": "Monorepo?", "source": "channel"},
            "agenda": {"value": [], "source": "default"},
            "convener": {"value": "nova-sparrow", "source": "channel"},
            "goal": {"value": "A recommendation.", "source": "channel"},
            "max_rounds": {"value": 12, "source": "default"},
            "schedule_interval_seconds": {"value": 0, "source": "default"},
            "max_convenings": {"value": 3, "source": "channel"},
            "standing_budget_tokens": {"value": 0, "source": "default"},
        },
        "autonomous_runtime": runtime,
    });
    serde_json::from_value(json).unwrap()
}

#[test]
fn autonomous_runtime_summary_reports_count_and_remaining_when_bounded() {
    // Armed + a positive max_convenings ⇒ the count and the remaining allowance.
    let view = sample_view(
        true,
        serde_json::json!({"convening_count": 1, "convenings_remaining": 2}),
    );
    assert_eq!(
        autonomous_runtime_summary(&view).as_deref(),
        Some("1 used, 2 remaining")
    );
}

#[test]
fn autonomous_runtime_summary_names_unbounded_when_no_remaining() {
    // A null remaining is the unbounded signal (no positive max_convenings) — the
    // count is still reported, but there is no allowance to count down.
    let view = sample_view(
        true,
        serde_json::json!({"convening_count": 4, "convenings_remaining": null}),
    );
    assert_eq!(
        autonomous_runtime_summary(&view).as_deref(),
        Some("4 used (no aggregate bound)")
    );
}

#[test]
fn autonomous_runtime_summary_absent_when_not_armed() {
    // A non-autonomous channel has no convening story to tell — the readout is
    // suppressed rather than rendering a misleading "0 used".
    let view = sample_view(
        false,
        serde_json::json!({"convening_count": 0, "convenings_remaining": null}),
    );
    assert_eq!(autonomous_runtime_summary(&view), None);
}

#[test]
fn autonomous_runtime_view_defaults_when_field_absent() {
    // Backward compatibility: a payload predating the runtime block (or a stub
    // test fixture) still deserializes — the field defaults to count 0 /
    // unbounded rather than failing the whole GET decode.
    let json = serde_json::json!({
        "revision": 0,
        "floor_control": {"value": true, "source": "default"},
        "salience_max_channel_members": {"value": 8, "source": "default"},
        "max_replies_per_participant_per_interaction": {"value": 2, "source": "default"},
        "end_vote_threshold": {"value": 3, "source": "default"},
        "end_vote_window": {"value": 5, "source": "default"},
        "escalation_chair_id": {"value": "", "source": "default"},
        "interaction_idle_timeout_seconds": {"value": 600, "source": "default"},
        "interaction_budget_tokens": {"value": null, "source": "default"},
        "reasoning": {
            "mode": {"value": "bid", "source": "default"},
            "model": {"value": "fast", "source": "default"},
            "depth": {"value": "shallow", "source": "default"},
            "revise": {"value": 0, "source": "default"},
        },
        "autonomous": {
            "enabled": {"value": false, "source": "default"},
            "topic": {"value": "", "source": "default"},
            "agenda": {"value": [], "source": "default"},
            "convener": {"value": "", "source": "default"},
            "goal": {"value": "", "source": "default"},
            "max_rounds": {"value": 12, "source": "default"},
            "schedule_interval_seconds": {"value": 0, "source": "default"},
            "max_convenings": {"value": 0, "source": "default"},
            "standing_budget_tokens": {"value": 0, "source": "default"},
        },
    });
    let view: ChannelConfigView = serde_json::from_value(json).unwrap();
    assert_eq!(view.autonomous_runtime.convening_count, 0);
    assert!(view.autonomous_runtime.convenings_remaining.is_none());
}
