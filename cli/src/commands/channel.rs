//! Channel subcommand group — RFC 0011 §F.
//!
//! Thin-client pattern (per `.github/instructions/rust-cli.instructions.md`):
//! every subcommand marshals args into a REST call and prints the response.
//! Wire shapes mirror `internal/server/channel_types.go` so the `--json`
//! output is a byte-for-byte passthrough.

use std::collections::HashSet;
use std::time::Duration;

use colored::Colorize;
use serde_json::json;

use crate::commands::channel_types::{
    AddMemberRequest, ChannelMember, ChannelMessage, ChannelView, HistoryResponse,
    ListChannelsResponse, PublishMessageRequest,
};
use crate::types::{api_error_message, validate_path_param};

/// 5 s poll cadence for `channel watch` (RFC 0011 OQ #4 default).
pub(crate) const DEFAULT_WATCH_INTERVAL_SECS: u64 = 5;

/// Mirrors `channelDefaultHistoryLimit` so CLI and server agree.
pub(crate) const DEFAULT_HISTORY_LIMIT: u32 = 50;

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

/// Tracks the high-watermark for `persatrix channel watch`.
///
/// Each poll returns the latest N messages newest-first; we filter by
/// message id (not timestamp — SQLite's ms resolution can repeat) and
/// only print ids we have not seen yet.
#[derive(Debug, Default)]
pub(crate) struct WatchState {
    seen: HashSet<String>,
}

impl WatchState {
    pub(crate) fn new() -> Self {
        Self::default()
    }

    /// Filter `batch` to unseen messages and reverse to oldest-first
    /// (the server returns newest-first; humans want chronological).
    pub(crate) fn apply_batch(&mut self, mut batch: Vec<ChannelMessage>) -> Vec<ChannelMessage> {
        batch.reverse();
        let mut out: Vec<ChannelMessage> = Vec::new();
        for msg in batch {
            if self.seen.insert(msg.id.clone()) {
                out.push(msg);
            }
        }
        out
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
    if json_out {
        println!("{}", serde_json::to_string_pretty(&body.channels).unwrap());
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
    let mentions = if mention_all {
        let members = fetch_channel_members(client, server, &canonical).await?;
        expand_mentions(explicit_mentions, true, &members, sender_id)
    } else {
        expand_mentions(explicit_mentions, false, &[], sender_id)
    };
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
        println!("{}", serde_json::to_string_pretty(&body.messages).unwrap());
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
    let mut state = WatchState::new();
    let interval = Duration::from_secs(interval_secs.max(1));
    eprintln!(
        "Watching {} (poll every {}s; Ctrl-C to stop)",
        format!("#{canonical}").cyan(),
        interval_secs
    );
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
        for msg in state.apply_batch(body.messages) {
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

// ─── Clap subcommand surface + dispatch ─────────────────────────────────

/// `persatrix channel <subcommand>` parser. Co-located with `dispatch`
/// so a new variant compile-errors here, not in `main.rs`.
#[derive(clap::Subcommand)]
pub(crate) enum ChannelCommands {
    /// List channels visible to the orchestrator
    List {
        /// Emit JSON instead of human-readable rows
        #[arg(long)]
        json: bool,
    },
    /// Add a participant to a channel's membership
    Join {
        /// Channel name (`planning`) or fully-qualified id (`group:planning`, `dm:a:b`)
        name: String,
        /// User identity to add (defaults to OS username, normalized)
        #[arg(long)]
        r#as: Option<String>,
        /// Response policy: `when_mentioned` (default), `always`, or `never`
        #[arg(long, default_value = "when_mentioned")]
        respond: String,
        #[arg(long)]
        json: bool,
    },
    /// Publish a top-level message to a channel
    Send {
        name: String,
        message: String,
        /// Sender identity (defaults to OS username, normalized)
        #[arg(long)]
        r#as: Option<String>,
        /// Mention a participant (`--mention alice --mention bob` is repeatable)
        #[arg(long = "mention")]
        mention: Vec<String>,
        /// Mention every channel member (resolved client-side via GET /channels/{id})
        #[arg(long)]
        mention_all: bool,
        #[arg(long)]
        json: bool,
    },
    /// Reply to an existing channel message in its thread
    Reply {
        name: String,
        message_id: String,
        message: String,
        #[arg(long)]
        r#as: Option<String>,
        #[arg(long = "mention")]
        mention: Vec<String>,
        #[arg(long)]
        mention_all: bool,
        #[arg(long)]
        json: bool,
    },
    /// Print the recent history of a channel (newest-first)
    History {
        name: String,
        /// Number of messages to fetch (default: 50, server cap: 1000)
        #[arg(long, default_value_t = DEFAULT_HISTORY_LIMIT)]
        limit: u32,
        #[arg(long)]
        json: bool,
    },
    /// Poll a channel for new messages (5 s default; Ctrl-C to stop)
    Watch {
        name: String,
        /// Poll interval in seconds (default: 5)
        #[arg(long, default_value_t = DEFAULT_WATCH_INTERVAL_SECS)]
        interval: u64,
        /// Per-poll page size (default: 50)
        #[arg(long, default_value_t = DEFAULT_HISTORY_LIMIT)]
        limit: u32,
        /// Emit JSON Lines instead of human rows
        #[arg(long)]
        json: bool,
    },
}

pub(crate) async fn dispatch(
    client: &reqwest::Client,
    server: &str,
    cmd: ChannelCommands,
    default_user: impl FnOnce() -> String,
) -> Result<(), String> {
    match cmd {
        ChannelCommands::List { json } => cmd_channel_list(client, server, json).await,
        ChannelCommands::Join {
            name,
            r#as,
            respond,
            json,
        } => {
            let user_id = r#as.unwrap_or_else(default_user);
            cmd_channel_join(client, server, &name, &user_id, &respond, json).await
        }
        ChannelCommands::Send {
            name,
            message,
            r#as,
            mention,
            mention_all,
            json,
        } => {
            let sender_id = r#as.unwrap_or_else(default_user);
            cmd_channel_send(
                client,
                server,
                &name,
                &message,
                &sender_id,
                &mention,
                mention_all,
                "",
                json,
            )
            .await
        }
        ChannelCommands::Reply {
            name,
            message_id,
            message,
            r#as,
            mention,
            mention_all,
            json,
        } => {
            let sender_id = r#as.unwrap_or_else(default_user);
            cmd_channel_send(
                client,
                server,
                &name,
                &message,
                &sender_id,
                &mention,
                mention_all,
                &message_id,
                json,
            )
            .await
        }
        ChannelCommands::History { name, limit, json } => {
            cmd_channel_history(client, server, &name, limit, json).await
        }
        ChannelCommands::Watch {
            name,
            interval,
            limit,
            json,
        } => cmd_channel_watch(client, server, &name, interval, limit, json).await,
    }
}

#[cfg(test)]
#[path = "channel_tests.rs"]
mod tests;
