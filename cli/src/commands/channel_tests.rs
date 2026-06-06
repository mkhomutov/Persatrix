//! Pure-helper tests for [`super`] (the [`crate::commands::channel`]
//! module). Wired in via `#[path = "channel_tests.rs"] mod tests;` so
//! `channel.rs` stays under the 500-line review cap. Logic-only —
//! HTTP-touching paths are exercised by the integration tests at
//! `tests/`.

use super::*;
use crate::commands::channel_types::ChannelMessage;
// WatchState + watch-ring constants moved to `channel_watch` (RFC 0031 Phase 3
// PR 4 size-cap relief); import them here so the watch tests keep resolving.
use crate::commands::channel_watch::{
    watch_seen_cap_for, WatchState, FULL_PAGE_WARNING_TEXT, WATCH_SEEN_CAP, WATCH_SEEN_CAP_CEILING,
};

fn message(id: &str, ts: &str) -> ChannelMessage {
    ChannelMessage {
        id: id.to_string(),
        channel_id: "group:planning".to_string(),
        sender_id: "alice".to_string(),
        content: format!("hello from {id}"),
        timestamp: ts.to_string(),
        thread_id: String::new(),
        mentions: Vec::new(),
        metadata: None,
    }
}

// ─── canonicalize_channel_id ───────────────────────────────────────

#[test]
fn canonicalize_bare_name_prefixes_group() {
    // Bare name → group prefix matches handleCreateChannel's
    // canonical id derivation in internal/server/channel_handlers.go.
    assert_eq!(canonicalize_channel_id("planning"), "group:planning");
}

#[test]
fn canonicalize_passthrough_for_qualified_id() {
    // `:` in input → already-qualified id, pass through verbatim.
    assert_eq!(canonicalize_channel_id("group:planning"), "group:planning");
    assert_eq!(canonicalize_channel_id("dm:alice:bob"), "dm:alice:bob");
    assert_eq!(canonicalize_channel_id("thread:msg-100"), "thread:msg-100");
}

// ─── expand_mentions ────────────────────────────────────────────────

#[test]
fn expand_mentions_explicit_only() {
    let mentions = expand_mentions(&["bob".to_string(), "carol".to_string()], false, "alice");
    assert_eq!(mentions, vec!["bob".to_string(), "carol".to_string()]);
}

#[test]
fn expand_mentions_mention_all_emits_everyone_sentinel() {
    // `--mention-all` is the broadcast: it emits the roster-independent
    // `@everyone` sentinel (RFC 0030 Tier A, decision D3), NOT an
    // enumerated member list. The server's directed-elsewhere filter keys
    // on the sentinel's presence, so an un-named `participant` is admitted
    // without the CLI having to fetch and list the roster.
    let mentions = expand_mentions(&[], true, "alice");
    assert_eq!(mentions, vec![MENTION_EVERYONE.to_string()]);
}

#[test]
fn expand_mentions_mention_all_keeps_explicit_then_sentinel() {
    // `--mention bob --mention-all`: the explicit id (addressed first under
    // the gate's mentioned-first ordering) precedes the broadcast sentinel.
    let mentions = expand_mentions(&["bob".to_string()], true, "alice");
    assert_eq!(
        mentions,
        vec!["bob".to_string(), MENTION_EVERYONE.to_string()]
    );
}

#[test]
fn expand_mentions_mention_all_dedupes_explicit_sentinel() {
    // `--mention @everyone --mention-all` must not list the sentinel twice.
    let mentions = expand_mentions(&[MENTION_EVERYONE.to_string()], true, "alice");
    assert_eq!(mentions, vec![MENTION_EVERYONE.to_string()]);
}

#[test]
fn expand_mentions_explicit_dedupes_repeats() {
    // Repeated `--mention bob --mention bob` collapses to one entry.
    let mentions = expand_mentions(
        &["bob".to_string(), "bob".to_string(), "carol".to_string()],
        false,
        "alice",
    );
    assert_eq!(mentions, vec!["bob".to_string(), "carol".to_string()]);
}

#[test]
fn expand_mentions_mention_all_drops_self_then_adds_sentinel() {
    // Sender cannot @-mention themselves; the broadcast sentinel still
    // lands (the gate excludes the sender from a broadcast on its side).
    let mentions = expand_mentions(&["alice".to_string()], true, "alice");
    assert_eq!(mentions, vec![MENTION_EVERYONE.to_string()]);
}

#[test]
fn expand_mentions_drops_explicit_self_mention_without_mention_all() {
    // PR #302 deep-review finding N7: the explicit-self-drop is
    // unconditional in `expand_mentions`, independent of the flag.
    let mentions = expand_mentions(&["alice".to_string(), "bob".to_string()], false, "alice");
    assert_eq!(
        mentions,
        vec!["bob".to_string()],
        "explicit @-self should drop even when --mention-all is off"
    );
}

#[test]
fn expand_mentions_no_flag_is_empty() {
    let mentions = expand_mentions(&[], false, "alice");
    assert!(mentions.is_empty());
}

// ─── WatchState ────────────────────────────────────────────────────

#[test]
fn watch_state_first_batch_returns_all_oldest_first() {
    // First poll: every message is new. The orchestrator returns
    // newest-first; the human-facing watch loop wants oldest-first so
    // the new messages read in chronological order.
    let mut state = WatchState::default();
    let batch = vec![
        message("msg-3", "2026-05-09T10:03:00Z"),
        message("msg-2", "2026-05-09T10:02:00Z"),
        message("msg-1", "2026-05-09T10:01:00Z"),
    ];
    let printed = state.apply_batch(batch);
    let ids: Vec<&str> = printed.iter().map(|m| m.id.as_str()).collect();
    assert_eq!(ids, vec!["msg-1", "msg-2", "msg-3"]);
}

#[test]
fn watch_state_dedupes_subsequent_polls() {
    // Second poll returns msg-1..msg-3 again (server window) plus
    // msg-4. Only msg-3 (the new one in the second batch here) should
    // print.
    let mut state = WatchState::default();
    state.apply_batch(vec![
        message("msg-2", "2026-05-09T10:02:00Z"),
        message("msg-1", "2026-05-09T10:01:00Z"),
    ]);
    let printed = state.apply_batch(vec![
        message("msg-3", "2026-05-09T10:03:00Z"),
        message("msg-2", "2026-05-09T10:02:00Z"),
        message("msg-1", "2026-05-09T10:01:00Z"),
    ]);
    let ids: Vec<&str> = printed.iter().map(|m| m.id.as_str()).collect();
    assert_eq!(ids, vec!["msg-3"]);
}

#[test]
fn watch_state_empty_batch_emits_nothing() {
    let mut state = WatchState::default();
    let printed = state.apply_batch(vec![]);
    assert!(printed.is_empty());
}

#[test]
fn watch_state_dedupes_within_single_batch() {
    // Defensive: if the server somehow returns the same id twice in
    // one page, we still only emit it once.
    let mut state = WatchState::default();
    let printed = state.apply_batch(vec![
        message("msg-1", "2026-05-09T10:01:00Z"),
        message("msg-1", "2026-05-09T10:01:00Z"),
    ]);
    let ids: Vec<&str> = printed.iter().map(|m| m.id.as_str()).collect();
    assert_eq!(ids, vec!["msg-1"]);
}

#[test]
fn watch_state_evicts_oldest_beyond_cap() {
    // Long-running watches must not grow `seen` without bound. A
    // bounded ring evicts the oldest ids first; ids re-appearing after
    // eviction are treated as new again. That's the deliberate
    // tradeoff — the cap should be > 4× page size so a burst within
    // one or two pages never re-prints across normal cadences.
    //
    // apply_batch reverses input (server returns newest-first), so
    // after the first batch the insertion order is msg-1, msg-2, msg-3.
    let mut state = WatchState::with_cap(3);
    state.apply_batch(vec![
        message("msg-3", "2026-05-09T10:03:00Z"),
        message("msg-2", "2026-05-09T10:02:00Z"),
        message("msg-1", "2026-05-09T10:01:00Z"),
    ]);
    // Ring (oldest→newest): [msg-1, msg-2, msg-3]. Adding msg-4 evicts
    // msg-1. Ring: [msg-2, msg-3, msg-4].
    let printed = state.apply_batch(vec![message("msg-4", "2026-05-09T10:04:00Z")]);
    assert_eq!(printed.len(), 1);
    assert_eq!(printed[0].id, "msg-4");

    // msg-3 and msg-4 are still in the ring → deduped. msg-2 is also
    // in the ring at this point.
    let printed = state.apply_batch(vec![
        message("msg-4", "2026-05-09T10:04:00Z"),
        message("msg-3", "2026-05-09T10:03:00Z"),
        message("msg-2", "2026-05-09T10:02:00Z"),
    ]);
    assert!(printed.is_empty(), "ids still in cap remain deduped");

    // msg-1 was evicted, so a fresh sighting prints again. This
    // re-insertion evicts msg-2 (now the oldest). Ring: [msg-3, msg-4, msg-1].
    let printed = state.apply_batch(vec![message("msg-1", "2026-05-09T10:01:00Z")]);
    assert_eq!(
        printed.len(),
        1,
        "evicted id is re-emitted on next sighting"
    );
}

#[test]
fn watch_state_cap_zero_clamps_to_one() {
    // Defensive: with_cap(0) would otherwise allow unbounded growth or
    // immediate eviction of the only entry. Clamp to ≥1 so the ring
    // always retains at least the most recent id.
    let mut state = WatchState::with_cap(0);
    let printed = state.apply_batch(vec![message("msg-1", "2026-05-09T10:01:00Z")]);
    assert_eq!(printed.len(), 1);
    let printed = state.apply_batch(vec![message("msg-1", "2026-05-09T10:01:00Z")]);
    assert!(printed.is_empty(), "the kept entry still dedupes");
}

// ─── validate_message_id ────────────────────────────────────────────

#[test]
fn validate_message_id_rejects_empty() {
    // Reproducer for PR #302 finding #1: clap accepts "" as a positional,
    // serde then drops `thread_id` via skip_serializing_if, and the server
    // accepts the call as a top-level publish — silently degrading a
    // `reply` into a `send`. The CLI must reject before constructing the
    // request.
    let err = validate_message_id("").unwrap_err();
    assert!(err.contains("message id"), "error names the field: {err}");
}

#[test]
fn validate_message_id_rejects_whitespace() {
    // A whitespace-only id would also serialize as `thread_id: " "`
    // and the server's id-format check would reject — but emitting a
    // clear local error is friendlier than a 400 round-trip.
    assert!(validate_message_id("   ").is_err());
    assert!(validate_message_id("\t\n").is_err());
}

#[test]
fn validate_message_id_accepts_normal_id() {
    // Server-issued message ids are UUIDs and similar opaque tokens.
    assert!(validate_message_id("msg-100").is_ok());
    assert!(validate_message_id("550e8400-e29b-41d4-a716-446655440000").is_ok());
}

// ─── validate_mention_count ─────────────────────────────────────────

#[test]
fn validate_mention_count_under_cap_is_ok() {
    // Server cap is `channelMaxMentionsPerPublish = 10`
    // (internal/server/channel_handlers.go). Under the cap, no error.
    let mentions: Vec<String> = (0..10).map(|i| format!("user-{i}")).collect();
    assert!(validate_mention_count(&mentions).is_ok());
}

#[test]
fn validate_mention_count_over_cap_is_err() {
    // PR #302 finding #4: --mention-all on a >10-member channel must
    // fail fast client-side rather than round-trip a generic 400.
    let mentions: Vec<String> = (0..11).map(|i| format!("user-{i}")).collect();
    let err = validate_mention_count(&mentions).unwrap_err();
    assert!(err.contains("11"), "error mentions the actual count: {err}");
    assert!(err.contains("10"), "error mentions the server cap: {err}");
}

// ─── watch_seen_cap_for (PR #302 deep-review finding 2) ─────────────

#[test]
fn watch_seen_cap_at_default_limit_uses_default_floor() {
    // At the default 50-page, the historic 1024 cap is ~20× the page —
    // the original sizing rationale. Keep the floor unchanged for the
    // common case so existing watchers behave identically.
    assert_eq!(watch_seen_cap_for(DEFAULT_HISTORY_LIMIT), WATCH_SEEN_CAP);
}

#[test]
fn watch_seen_cap_at_high_limit_scales_with_limit() {
    // At --limit 1000, the historic 1024 cap is only ~1× the page, so
    // a between-poll burst evicts in-page ids that re-appear on the
    // next poll. The 4× scale preserves the "ring covers a few full
    // pages of overlap" property the default sizing was chosen for.
    assert_eq!(watch_seen_cap_for(1000), 4000);
    assert_eq!(watch_seen_cap_for(500), 2000);
}

#[test]
fn watch_seen_cap_floor_protects_small_limits() {
    // A small --limit must not shrink the ring below the historic
    // floor — multi-channel monitors and thin pages still need a
    // cushion against same-id re-sightings across many polls.
    assert_eq!(watch_seen_cap_for(10), WATCH_SEEN_CAP);
    assert_eq!(watch_seen_cap_for(0), WATCH_SEEN_CAP);
}

#[test]
fn watch_seen_cap_does_not_overflow_on_max_u32() {
    // Defensive: limit comes from clap as `u32`. A pathological
    // u32::MAX must not panic in the multiply — saturating_mul keeps
    // us at usize::MAX, then the ceiling clamp brings the value back
    // to a bounded cap. PR #302 deep-review finding S1 — the prior
    // assertion stopped at "must not panic" but the *next* step
    // (`WatchState::with_cap`) would still attempt to allocate a
    // multi-GB HashSet.
    let cap = watch_seen_cap_for(u32::MAX);
    assert_eq!(
        cap, WATCH_SEEN_CAP_CEILING,
        "u32::MAX must clamp to the ceiling, not pass through usize::MAX"
    );
}

#[test]
fn watch_seen_cap_clamps_to_ceiling_for_large_limit() {
    // PR #302 deep-review finding S1: clap accepts `--limit: u32` with
    // no client-side ceiling; the server silently caps at
    // `channelMaxLimit = 1000`, but the CLI eagerly preallocates a
    // `HashSet`/`VecDeque` of `4 × limit` capacity. A typo like
    // `--limit 1000000000` would attempt to reserve gigabytes of heap.
    // The ceiling clamp keeps the pre-allocation bounded for any
    // pathological `--limit` value while leaving real values
    // (server-capped at 1000 anyway) unchanged.
    let cap = watch_seen_cap_for(1_000_000_000);
    assert_eq!(cap, WATCH_SEEN_CAP_CEILING);
    // `WatchState::with_cap` at the ceiling allocates a bounded ring
    // and the ring still functions (records and dedupes correctly).
    let mut state = WatchState::with_cap(cap);
    let printed = state.apply_batch(vec![message("msg-1", "2026-05-09T10:01:00Z")]);
    assert_eq!(printed.len(), 1, "ring at ceiling still records new ids");
    let printed = state.apply_batch(vec![message("msg-1", "2026-05-09T10:01:00Z")]);
    assert!(printed.is_empty(), "ring at ceiling still dedupes");
}

#[test]
fn watch_seen_cap_below_ceiling_passes_through() {
    // Real `--limit` values (server-capped at 1000) never hit the
    // ceiling — the 4× scale at limit=1000 is 4000, comfortably below
    // `WATCH_SEEN_CAP_CEILING` (16 384). The const-block assertion
    // makes a future ceiling lowering a compile error rather than a
    // runtime test failure.
    const _: () = assert!(WATCH_SEEN_CAP_CEILING >= 4 * 1000);
    assert_eq!(watch_seen_cap_for(1000), 4000);
}

// ─── validate_send_inputs (PR #302 deep-review finding 3) ───────────

#[test]
fn validate_send_inputs_accepts_resource_ids() {
    // Mirrors the agent-id shape used everywhere else (chat.rs,
    // agent.rs, workflow.rs). default_user_id() is already invariant
    // on this shape, so the OS-derived fallback always passes —
    // explicit --as / --mention values are the ones that need a check.
    assert!(validate_send_inputs("alice", &["bob".to_string(), "carol-1".to_string()]).is_ok());
    assert!(validate_send_inputs("user-01", &[]).is_ok());
}

#[test]
fn validate_send_inputs_rejects_uppercase_sender() {
    // Server's participantIDPattern accepts uppercase, but every
    // configured agent id matches the stricter resource-id shape.
    // Failing locally with a clear field name is friendlier than a
    // round-tripped 400 with the raw regex string.
    let err = validate_send_inputs("Alice", &[]).unwrap_err();
    assert!(err.contains("sender id"), "label names the field: {err}");
}

#[test]
fn validate_send_inputs_rejects_invalid_mention() {
    // Underscores pass the server regex but fail validate_resource_id.
    // Because the rest of the CLI is consistent on the stricter shape,
    // we hold --mention to the same bar — preventing a partial publish
    // where some mentions land and the request 400s on the typo'd one.
    let err =
        validate_send_inputs("alice", &["bob".to_string(), "not_valid".to_string()]).unwrap_err();
    assert!(err.contains("mention"), "label names the field: {err}");
}

#[test]
fn validate_send_inputs_accepts_everyone_sentinel() {
    // `@everyone` is the D3 broadcast sentinel, not a participant id — it
    // must clear client-side validation (mirrors the server-side carve-out,
    // ISSUE-0094) so `--mention-all` (and an explicit `--mention @everyone`)
    // is not rejected before it reaches the wire.
    assert!(validate_send_inputs("alice", &[MENTION_EVERYONE.to_string()]).is_ok());
    assert!(
        validate_send_inputs("alice", &["bob".to_string(), MENTION_EVERYONE.to_string()]).is_ok()
    );
}

// ─── full_page_warning_text (PR #302 deep-review finding 5) ─────────

#[test]
fn full_page_warning_avoids_asserting_data_loss() {
    // Original wording asserted "older messages may have been missed",
    // but the warning also fires on a benign burst of exactly `limit`
    // new messages with zero prior-page overlap (no data loss). The
    // softer wording stays accurate in both cases while pointing at
    // the same corrective action.
    let text = FULL_PAGE_WARNING_TEXT;
    assert!(
        !text.contains("missed"),
        "asserts data loss in the no-loss burst case: {text}"
    );
    assert!(
        text.contains("rolled over") || text.contains("roll over"),
        "softer wording present: {text}"
    );
    assert!(
        text.contains("--limit") && text.contains("--interval"),
        "still names both corrective knobs: {text}"
    );
}

// ─── apply_batch_full_page signal (finding #3) ──────────────────────

#[test]
fn watch_state_signals_full_page_when_all_unseen() {
    // PR #302 finding #3: if every message on a polled page is new, the
    // older messages have likely fallen off the page — emit a warning.
    // The signal is "len(unseen) == len(batch)" *after* dedup; the
    // watch loop combines that with "len(batch) == limit" to decide
    // whether to warn.
    let mut state = WatchState::default();
    let batch = vec![
        message("msg-3", "2026-05-09T10:03:00Z"),
        message("msg-2", "2026-05-09T10:02:00Z"),
        message("msg-1", "2026-05-09T10:01:00Z"),
    ];
    let batch_size = batch.len();
    let printed = state.apply_batch(batch);
    assert_eq!(printed.len(), batch_size, "first poll: every id is unseen");
}

#[test]
fn watch_state_partial_page_does_not_signal() {
    // Subsequent poll with overlap: `apply_batch` returns fewer entries
    // than the input. The watch loop won't warn because dedup
    // suppressed at least one entry.
    let mut state = WatchState::default();
    state.apply_batch(vec![message("msg-1", "2026-05-09T10:01:00Z")]);
    let batch = vec![
        message("msg-2", "2026-05-09T10:02:00Z"),
        message("msg-1", "2026-05-09T10:01:00Z"),
    ];
    let batch_size = batch.len();
    let printed = state.apply_batch(batch);
    assert!(printed.len() < batch_size);
}
