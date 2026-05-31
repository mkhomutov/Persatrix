//! Channel subcommand group — RFC 0011 §F.
//!
//! Thin-client pattern (per `.github/instructions/rust-cli.instructions.md`):
//! every subcommand marshals args into a REST call and prints the response.
//! Wire shapes mirror `internal/server/channel_types.go`. `--json` output
//! preserves every field that is explicitly modeled in `channel_types.rs`;
//! unknown server fields are dropped (the DTOs do not set
//! `deny_unknown_fields` and do not flatten extras).

use std::collections::HashSet;
use std::time::Duration;

use colored::Colorize;
use serde_json::json;

use crate::commands::channel_render::{fetch_agent_display_names, format_message_line};
use crate::commands::channel_types::{
    AddMemberRequest, ChannelMember, ChannelMessage, ChannelView, HistoryResponse,
    ListChannelsResponse, PublishMessageRequest,
};
use crate::commands::channel_watch::{watch_seen_cap_for, WatchState, FULL_PAGE_WARNING_TEXT};
use crate::types::{api_error_message, validate_path_param, validate_resource_id};

/// 5 s poll cadence for `channel watch` (RFC 0011 OQ #4 default).
pub(crate) const DEFAULT_WATCH_INTERVAL_SECS: u64 = 5;

/// Mirrors `channelDefaultHistoryLimit` so CLI and server agree.
pub(crate) const DEFAULT_HISTORY_LIMIT: u32 = 50;

/// Mirrors `channelMaxMentionsPerPublish` so the CLI fails fast with a
/// clear message instead of round-tripping a generic 400.
pub(crate) const MAX_MENTIONS_PER_PUBLISH: usize = 10;

// ─── Pure helpers (testable without an HTTP server) ─────────────────────

/// Normalise the user-facing `<name>` into a canonical channel id.
///
/// Bare names map to `group:<name>` (matches `handleCreateChannel`'s
/// canonical id derivation). Inputs already containing `:` (e.g.
/// `dm:alice:bob`, `thread:msg-100`) pass through unchanged.
pub(crate) fn canonicalize_channel_id(input: &str) -> String {
    if input.contains(':') {
        input.to_string()
    } else {
        format!("group:{input}")
    }
}

/// Resolve the `mentions` array sent on a publish request.
///
/// `--mention-all` resolves client-side (RFC 0011 PR plan PR 6) so the
/// orchestrator surface stays unchanged. The sender is excluded — the
/// gate would only fan the message back to the same actor. Order is
/// stable: explicit flags first (input order), then remaining members
/// (server order); duplicates collapse.
pub(crate) fn expand_mentions(
    explicit: &[String],
    mention_all: bool,
    members: &[ChannelMember],
    sender_id: &str,
) -> Vec<String> {
    let mut seen: HashSet<String> = HashSet::new();
    let mut out: Vec<String> = Vec::new();
    for m in explicit {
        if m == sender_id || !seen.insert(m.clone()) {
            continue;
        }
        out.push(m.clone());
    }
    if mention_all {
        for m in members {
            if m.id == sender_id || !seen.insert(m.id.clone()) {
                continue;
            }
            out.push(m.id.clone());
        }
    }
    out
}

/// Reject empty/whitespace `message_id` for the `Reply` subcommand.
///
/// clap accepts `""` as a positional, and serde would then drop the
/// `thread_id` field via `skip_serializing_if = "String::is_empty"`,
/// silently degrading a `reply` into a top-level `send`. The CLI
/// rejects locally so the surprise never reaches the wire.
pub(crate) fn validate_message_id(input: &str) -> Result<(), String> {
    if input.trim().is_empty() {
        return Err("message id must not be empty".into());
    }
    Ok(())
}

/// Validate `--as` (sender) and each `--mention` against the resource-id
/// shape (`^[a-z0-9][a-z0-9-]*[a-z0-9]$`). Parity with `cmd_chat`. The
/// server's `participantIDPattern` is broader, but every configured
/// agent id matches the stricter shape — rejecting locally beats a
/// generic 400. `default_user_id` already conforms (quickcheck in
/// `main.rs`); only explicit values gain coverage. PR #302 finding 3.
pub(crate) fn validate_send_inputs(sender_id: &str, mentions: &[String]) -> Result<(), String> {
    validate_resource_id(sender_id, "sender id")?;
    for m in mentions {
        validate_resource_id(m, "mention")?;
    }
    Ok(())
}

/// Reject mention arrays that exceed the server cap.
///
/// `--mention-all` resolves to every channel member client-side; on a
/// channel with > [`MAX_MENTIONS_PER_PUBLISH`] members the server's
/// `BAD_REQUEST` is opaque. Failing fast names both the actual count
/// and the cap so the user can pivot to explicit `--mention <id>`.
pub(crate) fn validate_mention_count(mentions: &[String]) -> Result<(), String> {
    if mentions.len() > MAX_MENTIONS_PER_PUBLISH {
        return Err(format!(
            "mentions expanded to {} ids; server caps at {}. Use explicit --mention <id> repeats.",
            mentions.len(),
            MAX_MENTIONS_PER_PUBLISH
        ));
    }
    Ok(())
}

// ─── Subcommand entry points ────────────────────────────────────────────

pub(crate) async fn cmd_channel_list(
    client: &reqwest::Client,
    server: &str,
    json_out: bool,
) -> Result<(), String> {
    let resp = client
        .get(format!("{server}/api/v1/channels"))
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }
    let body: ListChannelsResponse = resp
        .json()
        .await
        .map_err(|e| format!("invalid response: {e}"))?;
    // Non-empty `next_cursor` = "more rows exist". Stderr keeps `--json`
    // parseable; warn rather than auto-paginate. PR #302 finding 1.
    if !body.next_cursor.is_empty() {
        let label = "warning:".yellow().bold();
        eprintln!("{label} channel list truncated; more channels exist past the first page");
    }
    if json_out {
        // Single-line output (not pretty-printed): keeps `--json` consistent
        // across subcommands so downstream tools that count lines or pipe
        // through `jq` see the same shape regardless of which subcommand
        // emitted it.
        println!("{}", serde_json::to_string(&body.channels).unwrap());
        return Ok(());
    }
    if body.channels.is_empty() {
        println!("No channels.");
        return Ok(());
    }
    for ch in &body.channels {
        println!(
            "{}  {}  {}",
            ch.id.cyan(),
            ch.channel_type,
            ch.created_at.dimmed()
        );
    }
    Ok(())
}

pub(crate) async fn cmd_channel_join(
    client: &reqwest::Client,
    server: &str,
    name: &str,
    user_id: &str,
    respond: &str,
    json_out: bool,
) -> Result<(), String> {
    let canonical = canonicalize_channel_id(name);
    validate_path_param(&canonical, "channel id")?;
    // Same shape chat.rs validates user_id against; see [`validate_send_inputs`].
    validate_resource_id(user_id, "user id")?;
    let req = AddMemberRequest {
        id: user_id.to_string(),
        respond: respond.to_string(),
    };
    let resp = client
        .post(format!("{server}/api/v1/channels/{canonical}/members"))
        .json(&req)
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }
    if json_out {
        let payload = json!({"channel_id": canonical, "user_id": user_id, "respond": respond});
        println!("{}", serde_json::to_string(&payload).unwrap());
    } else {
        println!(
            "Joined {} as {}",
            format!("#{canonical}").cyan(),
            user_id.bold()
        );
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
pub(crate) async fn cmd_channel_send(
    client: &reqwest::Client,
    server: &str,
    name: &str,
    message: &str,
    sender_id: &str,
    explicit_mentions: &[String],
    mention_all: bool,
    thread_id: &str,
    session_flag: Option<&str>,
    epoch_flag: Option<&str>,
    json_out: bool,
) -> Result<(), String> {
    let canonical = canonicalize_channel_id(name);
    validate_path_param(&canonical, "channel id")?;
    // `--mention-all` resolves from the server's member list (already
    // validated at join time); only the explicit inputs need a check.
    validate_send_inputs(sender_id, explicit_mentions)?;
    // RFC 0031 Phase 3 `--session` override (OQ #6 precedence; see session_resolve).
    let session_id = crate::session_resolve::resolve_for_invocation(client, server, session_flag)
        .await?
        .unwrap_or_default();
    // ISSUE-0085 PR 5 `--epoch` override (flag > PERSATRIX_EPOCH env; see
    // epoch_resolve). No registry lookup — epoch has no lifecycle.
    let epoch_id = crate::epoch_resolve::resolve_epoch(epoch_flag).unwrap_or_default();
    let mentions = if mention_all {
        let members = fetch_channel_members(client, server, &canonical).await?;
        expand_mentions(explicit_mentions, true, &members, sender_id)
    } else {
        expand_mentions(explicit_mentions, false, &[], sender_id)
    };
    // Server caps mentions at MAX_MENTIONS_PER_PUBLISH; failing fast
    // here surfaces a clear message instead of an opaque 400 from the
    // unauthenticated REST surface.
    validate_mention_count(&mentions)?;
    let req = PublishMessageRequest {
        sender_id: sender_id.to_string(),
        content: message.to_string(),
        thread_id: thread_id.to_string(),
        mentions,
        session_id,
        epoch_id,
    };
    let resp = client
        .post(format!("{server}/api/v1/channels/{canonical}/messages"))
        .json(&req)
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }
    let stored: ChannelMessage = resp
        .json()
        .await
        .map_err(|e| format!("invalid response: {e}"))?;
    if json_out {
        println!("{}", serde_json::to_string(&stored).unwrap());
    } else if stored.thread_id.is_empty() {
        println!(
            "Sent {} to {}",
            stored.id.bold(),
            format!("#{canonical}").cyan()
        );
    } else {
        println!(
            "Sent {} to {} (reply to {})",
            stored.id.bold(),
            format!("#{canonical}").cyan(),
            stored.thread_id.dimmed()
        );
    }
    Ok(())
}

pub(crate) async fn cmd_channel_history(
    client: &reqwest::Client,
    server: &str,
    name: &str,
    limit: u32,
    json_out: bool,
) -> Result<(), String> {
    let canonical = canonicalize_channel_id(name);
    validate_path_param(&canonical, "channel id")?;
    let url = format!("{server}/api/v1/channels/{canonical}/messages?limit={limit}");
    let resp = client
        .get(&url)
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }
    let body: HistoryResponse = resp
        .json()
        .await
        .map_err(|e| format!("invalid response: {e}"))?;
    if json_out {
        // Single-line: see comment in cmd_channel_list.
        println!("{}", serde_json::to_string(&body.messages).unwrap());
        return Ok(());
    }
    if body.messages.is_empty() {
        println!("No messages.");
        return Ok(());
    }
    let names = fetch_agent_display_names(client, server).await;
    for msg in &body.messages {
        println!("{}", format_message_line(msg, &names));
    }
    Ok(())
}

pub(crate) async fn cmd_channel_watch(
    client: &reqwest::Client,
    server: &str,
    name: &str,
    interval_secs: u64,
    limit: u32,
    json_out: bool,
) -> Result<(), String> {
    let canonical = canonicalize_channel_id(name);
    validate_path_param(&canonical, "channel id")?;
    let url = format!("{server}/api/v1/channels/{canonical}/messages?limit={limit}");
    // Ring sized from `--limit`; see [`watch_seen_cap_for`].
    let mut state = WatchState::with_cap(watch_seen_cap_for(limit));
    let interval = Duration::from_secs(interval_secs.max(1));
    // One-shot pre-loop fetch: agents.yaml is static, so resolving once
    // saves a GET per poll. New mid-watch registrations fall back to id.
    let names = fetch_agent_display_names(client, server).await;
    eprintln!(
        "Watching {} (poll every {}s; times in UTC; Ctrl-C to stop)",
        format!("#{canonical}").cyan(),
        interval_secs
    );
    // First-poll suppression: a fresh watch always returns the latest
    // page newest-first. The "all unseen + full page" condition is
    // expected on poll #1 (it just means the channel had ≥ limit prior
    // messages), so warning then would be noise. After the first poll,
    // an all-unseen full page means messages may have fallen off the
    // window between polls and been silently lost.
    let mut first_poll = true;
    loop {
        let resp = client
            .get(&url)
            .send()
            .await
            .map_err(|e| format!("connection failed: {e}"))?;
        if !resp.status().is_success() {
            return Err(api_error_message(resp).await);
        }
        let body: HistoryResponse = resp
            .json()
            .await
            .map_err(|e| format!("invalid response: {e}"))?;
        let batch_size = body.messages.len();
        let printed = state.apply_batch(body.messages);
        let printed_count = printed.len();
        for msg in printed {
            if json_out {
                println!("{}", serde_json::to_string(&msg).unwrap());
            } else {
                println!("{}", format_message_line(&msg, &names));
            }
        }
        if !first_poll
            && printed_count == batch_size
            && batch_size as u32 == limit
            && batch_size > 0
        {
            eprintln!("{} {}", "warning:".yellow().bold(), FULL_PAGE_WARNING_TEXT);
        }
        first_poll = false;
        tokio::time::sleep(interval).await;
    }
}

async fn fetch_channel_members(
    client: &reqwest::Client,
    server: &str,
    canonical_id: &str,
) -> Result<Vec<ChannelMember>, String> {
    let resp = client
        .get(format!("{server}/api/v1/channels/{canonical_id}"))
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }
    let view: ChannelView = resp
        .json()
        .await
        .map_err(|e| format!("invalid response: {e}"))?;
    Ok(view.members)
}

#[cfg(test)]
#[path = "channel_tests.rs"]
mod tests;
