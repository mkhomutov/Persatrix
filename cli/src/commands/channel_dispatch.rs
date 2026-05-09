//! Clap surface + dispatch for the `channel` subcommand group.
//!
//! Split from [`super::channel`] so that file stays under the 500-line
//! review cap. The clap-generated parser surface and the matching
//! dispatch `match` arm are mutually constraining (adding a variant
//! here forces an arm in dispatch and vice versa) so they live together.

use clap::ValueEnum;

use crate::commands::channel::{
    cmd_channel_history, cmd_channel_join, cmd_channel_list, cmd_channel_send, cmd_channel_watch,
    validate_message_id, DEFAULT_HISTORY_LIMIT, DEFAULT_WATCH_INTERVAL_SECS,
};

/// `--respond` value-parser. Uppercases to clap's snake_case wire form
/// so the CLI rejects typos locally with a friendly `possible values`
/// list instead of round-tripping a 400 from the server.
#[derive(Copy, Clone, Debug, PartialEq, Eq, ValueEnum)]
#[clap(rename_all = "snake_case")]
pub(crate) enum RespondPolicy {
    WhenMentioned,
    Always,
    Never,
}

impl RespondPolicy {
    /// Wire token — must match `channels.RespondPolicy` constants in
    /// `internal/channels/channels.go`.
    pub(crate) fn as_wire_str(self) -> &'static str {
        match self {
            RespondPolicy::WhenMentioned => "when_mentioned",
            RespondPolicy::Always => "always",
            RespondPolicy::Never => "never",
        }
    }
}

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
        /// Response policy: `when_mentioned` (default), `always`, or `never`.
        /// Validated client-side via the [`RespondPolicy`] enum so typos
        /// surface as a clap error before the server round-trip.
        #[arg(long, value_enum, default_value_t = RespondPolicy::WhenMentioned)]
        respond: RespondPolicy,
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
        /// Mention a participant (`--mention alice --mention bob` is repeatable).
        /// Self-mentions are dropped — the channel gate would only fan the
        /// message back to the sender (PR #302 deep-review finding 6).
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
            cmd_channel_join(client, server, &name, &user_id, respond.as_wire_str(), json).await
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
            // Reject empty `message_id` before constructing the request:
            // serde's `skip_serializing_if = "String::is_empty"` would
            // otherwise drop the `thread_id` field, silently degrading
            // a `reply` into a top-level `send`. PR #302 finding #1.
            validate_message_id(&message_id)?;
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
