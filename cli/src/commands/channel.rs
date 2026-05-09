//! Channel subcommand group — RFC 0011 §F.
//!
//! Thin-client pattern (per `.github/instructions/rust-cli.instructions.md`):
//! every subcommand marshals args into a REST call and prints the response.
//! Wire shapes mirror `internal/server/channel_types.go`. `--json` output
//! preserves every field that is explicitly modeled in `channel_types.rs`;
//! unknown server fields are dropped (the DTOs do not set
//! `deny_unknown_fields` and do not flatten extras).

use std::collections::{HashSet, VecDeque};
use std::time::Duration;

use colored::Colorize;
use serde_json::json;

use crate::commands::channel_types::{
    AddMemberRequest, ChannelMember, ChannelMessage, ChannelView, HistoryResponse,
    ListChannelsResponse, PublishMessageRequest,
};
use crate::types::{api_error_message, validate_path_param, validate_resource_id};

/// 5 s poll cadence for `channel watch` (RFC 0011 OQ #4 default).
pub(crate) const DEFAULT_WATCH_INTERVAL_SECS: u64 = 5;

/// Mirrors `channelDefaultHistoryLimit` so CLI and server agree.
pub(crate) const DEFAULT_HISTORY_LIMIT: u32 = 50;

/// Mirrors `channelMaxMentionsPerPublish` so the CLI fails fast with a
/// clear message instead of round-tripping a generic 400.
pub(crate) const MAX_MENTIONS_PER_PUBLISH: usize = 10;

/// Floor for the `WatchState` dedup ring (~20× the default 50-page).
/// Back-to-back full pages never re-emit at the default; on overflow
/// the oldest id is evicted and a re-appearing id is treated as new.
/// [`watch_seen_cap_for`] scales the ring at higher `--limit` values.
pub(crate) const WATCH_SEEN_CAP: usize = 1024;

/// Stderr text for the watch full-page warning. Stays accurate for
/// both genuine page rollover (data loss) and benign bursts of exactly
/// `--limit` new messages (no loss); the corrective knobs are the same
/// either way. PR #302 finding 5.
pub(crate) const FULL_PAGE_WARNING_TEXT: &str =
    "polled page was completely full of new messages — the page may have rolled over; consider raising --limit or lowering --interval";

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

/// True when the server signals "more pages exist" via non-empty
/// `next_cursor`. Used by `cmd_channel_list` to warn on stderr (keeps
/// `--json` parseable) rather than auto-paginate, which would mask the
/// scaling concern. PR #302 finding 1.
pub(crate) fn should_warn_truncation(next_cursor: &str) -> bool {
    !next_cursor.is_empty()
}

/// Scale the [`WatchState`] dedup ring with the per-poll page size. At
/// `--limit 1000` the historic 1024 cap is only ~1× the page, so a
/// between-poll burst evicts in-page ids and they reprint on the next
/// poll. 4× preserves the original "ring covers a few full pages of
/// overlap" property at any limit; the 1024 floor protects multi-
/// channel monitors. PR #302 finding 2.
pub(crate) fn watch_seen_cap_for(limit: u32) -> usize {
    let scaled = (limit as usize).saturating_mul(4);
    scaled.max(WATCH_SEEN_CAP)
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

/// Tracks the high-watermark for `persatrix channel watch`.
///
/// Each poll returns the latest N messages newest-first; we filter by
/// message id (not timestamp — SQLite's ms resolution can repeat) and
/// only print ids we have not seen yet. The `seen` set is bounded by a
/// FIFO ring so a long-running watch does not grow heap monotonically;
/// on overflow the oldest id is evicted, and a re-sighting after
/// eviction prints again. The cap defaults to [`WATCH_SEEN_CAP`].
#[derive(Debug)]
pub(crate) struct WatchState {
    seen: HashSet<String>,
    order: VecDeque<String>,
    cap: usize,
}

impl Default for WatchState {
    fn default() -> Self {
        Self::with_cap(WATCH_SEEN_CAP)
    }
}

impl WatchState {
    /// Construct a `WatchState` with an explicit ring capacity (test seam).
    /// `cap` is clamped to ≥1 — a zero cap would make the ring useless
    /// and reintroduce the unbounded-growth risk via the `HashSet`.
    pub(crate) fn with_cap(cap: usize) -> Self {
        let cap = cap.max(1);
        Self {
            seen: HashSet::with_capacity(cap),
            order: VecDeque::with_capacity(cap),
            cap,
        }
    }

    /// Filter `batch` to unseen messages and reverse to oldest-first
    /// (the server returns newest-first; humans want chronological).
    pub(crate) fn apply_batch(&mut self, mut batch: Vec<ChannelMessage>) -> Vec<ChannelMessage> {
        batch.reverse();
        let mut out: Vec<ChannelMessage> = Vec::new();
        for msg in batch {
            if self.record(msg.id.clone()) {
                out.push(msg);
            }
        }
        out
    }

    /// Insert `id` into the ring. Returns `true` when newly seen,
    /// `false` when already present. On overflow the oldest entry is
    /// evicted from both `order` (FIFO) and `seen` (lookup) so the two
    /// stay in lockstep.
    fn record(&mut self, id: String) -> bool {
        if !self.seen.insert(id.clone()) {
            return false;
        }
        self.order.push_back(id);
        if self.order.len() > self.cap {
            if let Some(oldest) = self.order.pop_front() {
                self.seen.remove(&oldest);
            }
        }
        true
    }
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
    // Stderr keeps `--json` parseable; see [`should_warn_truncation`].
    if should_warn_truncation(&body.next_cursor) {
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
    json_out: bool,
) -> Result<(), String> {
    let canonical = canonicalize_channel_id(name);
    validate_path_param(&canonical, "channel id")?;
    // `--mention-all` resolves from the server's member list (already
    // validated at join time); only the explicit inputs need a check.
    validate_send_inputs(sender_id, explicit_mentions)?;
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
    for msg in &body.messages {
        println!(
            "{}  {}: {}",
            msg.timestamp.dimmed(),
            msg.sender_id.cyan(),
            msg.content
        );
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
    eprintln!(
        "Watching {} (poll every {}s; Ctrl-C to stop)",
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
                println!(
                    "{}  {}: {}",
                    msg.timestamp.dimmed(),
                    msg.sender_id.cyan(),
                    msg.content
                );
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
