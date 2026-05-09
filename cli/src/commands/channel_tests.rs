//! Pure-helper tests for [`super`] (the [`crate::commands::channel`]
//! module). Wired in via `#[path = "channel_tests.rs"] mod tests;` so
//! `channel.rs` stays under the 500-line review cap. Logic-only —
//! HTTP-touching paths are exercised by the integration tests at
//! `tests/`.

use super::*;
use crate::commands::channel_types::{ChannelMember, ChannelMessage};

fn member(id: &str, respond: &str) -> ChannelMember {
    ChannelMember {
        id: id.to_string(),
        respond_policy: respond.to_string(),
        joined_at: "2026-05-09T10:00:00Z".to_string(),
    }
}

fn message(id: &str, ts: &str) -> ChannelMessage {
    ChannelMessage {
        id: id.to_string(),
        channel_id: "group:planning".to_string(),
        sender_id: "alice".to_string(),
        content: format!("hello from {id}"),
        timestamp: ts.to_string(),
        thread_id: String::new(),
        mentions: Vec::new(),
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
    let mentions = expand_mentions(
        &["bob".to_string(), "carol".to_string()],
        false,
        &[],
        "alice",
    );
    assert_eq!(mentions, vec!["bob".to_string(), "carol".to_string()]);
}

#[test]
fn expand_mentions_mention_all_excludes_sender() {
    // `--mention-all` expands to every member except the sender.
    let members = vec![
        member("alice", "always"),
        member("bob", "when_mentioned"),
        member("carol", "when_mentioned"),
    ];
    let mentions = expand_mentions(&[], true, &members, "alice");
    assert_eq!(mentions, vec!["bob".to_string(), "carol".to_string()]);
}

#[test]
fn expand_mentions_dedupes_overlap() {
    // `--mention bob --mention-all` does not list bob twice.
    let members = vec![member("bob", "when_mentioned"), member("carol", "always")];
    let mentions = expand_mentions(&["bob".to_string()], true, &members, "alice");
    assert_eq!(mentions, vec!["bob".to_string(), "carol".to_string()]);
}

#[test]
fn expand_mentions_explicit_dedupes_repeats() {
    // Repeated `--mention bob --mention bob` collapses to one entry.
    let mentions = expand_mentions(
        &["bob".to_string(), "bob".to_string(), "carol".to_string()],
        false,
        &[],
        "alice",
    );
    assert_eq!(mentions, vec!["bob".to_string(), "carol".to_string()]);
}

#[test]
fn expand_mentions_drops_self_mention() {
    // Sender cannot @-mention themselves — the gate would just fan the
    // message back to the same actor.
    let members = vec![member("alice", "always"), member("bob", "always")];
    let mentions = expand_mentions(&["alice".to_string()], true, &members, "alice");
    assert_eq!(mentions, vec!["bob".to_string()]);
}

#[test]
fn expand_mentions_no_flag_no_members_is_empty() {
    let mentions = expand_mentions(&[], false, &[], "alice");
    assert!(mentions.is_empty());
}

// ─── WatchState ────────────────────────────────────────────────────

#[test]
fn watch_state_first_batch_returns_all_oldest_first() {
    // First poll: every message is new. The orchestrator returns
    // newest-first; the human-facing watch loop wants oldest-first so
    // the new messages read in chronological order.
    let mut state = WatchState::new();
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
    let mut state = WatchState::new();
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
    let mut state = WatchState::new();
    let printed = state.apply_batch(vec![]);
    assert!(printed.is_empty());
}

#[test]
fn watch_state_dedupes_within_single_batch() {
    // Defensive: if the server somehow returns the same id twice in
    // one page, we still only emit it once.
    let mut state = WatchState::new();
    let printed = state.apply_batch(vec![
        message("msg-1", "2026-05-09T10:01:00Z"),
        message("msg-1", "2026-05-09T10:01:00Z"),
    ]);
    let ids: Vec<&str> = printed.iter().map(|m| m.id.as_str()).collect();
    assert_eq!(ids, vec!["msg-1"]);
}
