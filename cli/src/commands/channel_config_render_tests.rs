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
