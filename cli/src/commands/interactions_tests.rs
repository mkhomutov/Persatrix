//! Pure-helper tests for [`super`] (the [`crate::commands::interactions`]
//! module). Wired in via `#[path = "interactions_tests.rs"] mod tests;` so
//! `interactions.rs` stays under the 500-line review cap. Logic-only — the
//! HTTP-touching `cmd_agent_interactions` path is a thin marshal/print over
//! these helpers, matching the channel/session test split.

use super::*;

fn interaction(reason: &str, summary: &str) -> ClosedInteraction {
    ClosedInteraction {
        interaction_id: "int-100".to_string(),
        scope: "group:planning".to_string(),
        started_at: 1000.0,
        closed_at: 1200.0,
        turn_count: 4,
        close_reason: reason.to_string(),
        summary: summary.to_string(),
        participants: vec!["iron-fox".to_string(), "stone-owl".to_string()],
    }
}

// ─── build_closed_interactions_query ────────────────────────────────────

#[test]
fn query_limit_only_when_no_filters() {
    // No scope / id / min_turns → just the always-pinned page size.
    let q = build_closed_interactions_query(None, None, 20, None);
    assert_eq!(q, "?limit=20");
}

#[test]
fn query_includes_scope_and_interaction_id() {
    let q = build_closed_interactions_query(Some("group:planning"), Some("int-100"), 5, None);
    // `:` is percent-encoded so the scope value can't break the query.
    assert_eq!(q, "?scope=group%3Aplanning&interaction_id=int-100&limit=5");
}

#[test]
fn query_includes_min_turns_when_supplied() {
    let q = build_closed_interactions_query(None, None, 20, Some(2));
    assert_eq!(q, "?limit=20&min_turns=2");
}

#[test]
fn query_omits_empty_scope_and_id() {
    // Empty strings are treated as absent (clap gives Some("") only if the user
    // passed `--scope ""`); they must not emit `scope=` / `interaction_id=`.
    let q = build_closed_interactions_query(Some(""), Some(""), 10, None);
    assert_eq!(q, "?limit=10");
}

#[test]
fn query_encodes_dm_scope_with_colons() {
    let q = build_closed_interactions_query(Some("dm:alice:bob"), None, 20, None);
    assert_eq!(q, "?scope=dm%3Aalice%3Abob&limit=20");
}

// ─── close_trigger_label ────────────────────────────────────────────────

#[test]
fn trigger_label_known_reasons() {
    // Must match web/src/lib/interactions.js closeTriggerLabel so the two
    // surfaces label a close identically.
    assert_eq!(close_trigger_label("cost"), "cost limit reached");
    assert_eq!(close_trigger_label("idle_gap"), "went idle");
    assert_eq!(close_trigger_label("structural"), "ended");
}

#[test]
fn trigger_label_unknown_degrades_to_closed() {
    assert_eq!(close_trigger_label(""), "closed");
    assert_eq!(close_trigger_label("something-new"), "closed");
}

// ─── is_summary_unavailable ─────────────────────────────────────────────

#[test]
fn sentinel_detected() {
    assert!(is_summary_unavailable(SUMMARY_UNAVAILABLE_TEXT));
    assert!(is_summary_unavailable("[interaction summary unavailable]"));
}

#[test]
fn non_sentinel_summaries_are_available() {
    assert!(!is_summary_unavailable("The team agreed to ship Friday."));
    // A blank is NOT the sentinel — the read path filters blanks upstream.
    assert!(!is_summary_unavailable(""));
}

// ─── format_interaction ─────────────────────────────────────────────────

#[test]
fn format_renders_summary_trigger_and_participants() {
    // colored disables ANSI when not a TTY (tests run non-TTY), so assert on
    // plain substrings.
    let out = format_interaction(&interaction("structural", "Shipped the plan."));
    assert!(out.contains("int-100"));
    assert!(out.contains("Conversation ended"));
    assert!(out.contains("4 turns"));
    assert!(out.contains("Shipped the plan."));
    assert!(out.contains("iron-fox, stone-owl"));
    assert!(out.contains("group:planning"));
}

#[test]
fn format_renders_cost_trigger() {
    let out = format_interaction(&interaction("cost", "Capped by budget."));
    assert!(out.contains("Conversation cost limit reached"));
}

#[test]
fn format_renders_sentinel_honestly() {
    // SS3: the failure sentinel surfaces as an explicit unavailable line, never
    // the raw marker and never a blank.
    let out = format_interaction(&interaction("idle_gap", SUMMARY_UNAVAILABLE_TEXT));
    assert!(out.contains("Summary unavailable for this interaction."));
    assert!(!out.contains(SUMMARY_UNAVAILABLE_TEXT));
    assert!(out.contains("Conversation went idle"));
}

#[test]
fn format_singular_turn_label() {
    let mut it = interaction("structural", "One and done.");
    it.turn_count = 1;
    let out = format_interaction(&it);
    assert!(out.contains("(1 turn)"));
    assert!(!out.contains("1 turns"));
}

#[test]
fn format_omits_turns_when_non_positive() {
    // A non-positive turn_count only arises from a forward-compat default (the
    // read path guarantees turn_count >= 1). Mirror the web surface, which hides
    // the count when falsy, rather than print a nonsensical "(0 turns)".
    let mut it = interaction("structural", "Done.");
    it.turn_count = 0;
    let out = format_interaction(&it);
    assert!(!out.contains("turn"));
    assert!(out.contains("Conversation ended"));
}

// ─── validate_interactions_filters ──────────────────────────────────────

#[test]
fn filters_reject_zero_limit() {
    // The server's parseLimit bounces limit <= 0 with a 400; catch it locally.
    assert!(validate_interactions_filters(0, None).is_err());
}

#[test]
fn filters_reject_explicit_zero_min_turns() {
    // parseMinTurns rejects an explicit min_turns=0 (no interaction has < 1
    // turn). An omitted min_turns (None) is fine — it forwards the 0 sentinel.
    assert!(validate_interactions_filters(20, Some(0)).is_err());
}

#[test]
fn filters_accept_valid_and_absent_min_turns() {
    assert!(validate_interactions_filters(20, None).is_ok());
    assert!(validate_interactions_filters(1, Some(1)).is_ok());
}

// ─── empty_state_filters ────────────────────────────────────────────────

#[test]
fn empty_state_note_lists_every_active_filter() {
    // A query that comes up empty *because of* --interaction-id / --min-turns
    // must say so, not read as "the agent has no closed interactions at all".
    let n = empty_state_filters(Some("group:planning"), Some("int-9"), Some(2));
    assert!(n.contains("scope group:planning"));
    assert!(n.contains("interaction int-9"));
    assert!(n.contains("min-turns 2"));
}

#[test]
fn empty_state_note_blank_when_no_filters() {
    assert_eq!(empty_state_filters(None, None, None), "");
    // Empty-string args (clap `--scope ""`) count as absent, like the query builder.
    assert_eq!(empty_state_filters(Some(""), Some(""), None), "");
}

// ─── raw_interactions_json (lossless --json passthrough) ─────────────────

#[test]
fn raw_json_preserves_unknown_row_fields() {
    // `--json` promises the raw row list, so a field a newer server adds must
    // survive — the typed struct would otherwise drop it on re-serialization.
    let raw = r#"{"interactions":[{"interaction_id":"int-1","future_field":true}]}"#;
    let out = raw_interactions_json(raw).unwrap();
    assert!(out.contains("future_field"));
    assert!(out.starts_with('['));
}

#[test]
fn raw_json_none_on_unexpected_shape() {
    // Falls back to the typed re-serialization when the body isn't the envelope.
    assert!(raw_interactions_json("{}").is_none());
    assert!(raw_interactions_json("not json").is_none());
    // `interactions` present but not an array → no safe passthrough.
    assert!(raw_interactions_json(r#"{"interactions":42}"#).is_none());
}

// ─── DTO deserialization (forward-compat) ───────────────────────────────

#[test]
fn deserializes_envelope_and_tolerates_missing_fields() {
    // An older server might omit participants / scope; `#[serde(default)]`
    // keeps the row parseable. Unknown fields are dropped (no deny_unknown).
    let json = r#"{"interactions":[{"interaction_id":"int-1","summary":"hi","close_reason":"cost","turn_count":2,"future_field":true}]}"#;
    let body: ClosedInteractionsResponse = serde_json::from_str(json).unwrap();
    assert_eq!(body.interactions.len(), 1);
    let it = &body.interactions[0];
    assert_eq!(it.interaction_id, "int-1");
    assert_eq!(it.summary, "hi");
    assert_eq!(it.close_reason, "cost");
    assert_eq!(it.turn_count, 2);
    assert!(it.participants.is_empty());
    assert_eq!(it.scope, "");
}

#[test]
fn deserializes_empty_envelope() {
    let body: ClosedInteractionsResponse = serde_json::from_str(r#"{"interactions":[]}"#).unwrap();
    assert!(body.interactions.is_empty());
    // A server that omits the field entirely still parses (serde default).
    let body2: ClosedInteractionsResponse = serde_json::from_str("{}").unwrap();
    assert!(body2.interactions.is_empty());
}
