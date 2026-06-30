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
use crate::commands::channel_config::{cmd_config_get, cmd_config_set, cmd_config_unset};
use crate::commands::channel_config_yaml::{cmd_config_diff, cmd_config_export, cmd_config_import};
use crate::commands::channel_convene::cmd_convene;
use crate::commands::channel_manage::{cmd_channel_create, cmd_channel_info};

/// Default declared-config path the `diff` verb reads when `--file` is omitted —
/// the repo's canonical channel config-as-code (RFC 0011 / RFC 0050).
const DEFAULT_CHANNELS_YAML: &str = "config/channels.yaml";

/// `--respond` value-parser. clap renders each variant in its `snake_case`
/// form (via `rename_all`), so `--respond` is validated against that set
/// locally and a typo surfaces as a friendly `possible values` list instead
/// of round-tripping a 400 from the server.
///
/// The full set MUST cover exactly the `channels.RespondPolicy` constants in
/// `internal/channels/channels.go`: the three legacy dispositions plus the
/// RFC 0030 relevance-amendment / v0.3.8 vocabulary (`participant`/`addressed`/
/// `observer` and the `chair` facilitator). The REST add/create handlers accept
/// every one of them — casting to `RespondPolicy`, then normalizing to the
/// legacy triple at the store boundary — so an allowlist narrower than the
/// server's silently hides shipped behaviour. (Declaration order here is a
/// client concern, not the Go order; `respond_policy_covers_server_vocabulary`
/// enforces set-equality against the Go source.)
#[derive(Copy, Clone, Debug, PartialEq, Eq, ValueEnum)]
#[clap(rename_all = "snake_case")]
pub(crate) enum RespondPolicy {
    WhenMentioned,
    Always,
    Never,
    /// Open-floor participant: runs the v0.3.8 salience bid, biased to silence.
    Participant,
    /// Low-threshold facilitator (a `participant` that clears the bid more
    /// readily); cannot close interactions in v0.3.8.
    Chair,
    /// Responds only when directly addressed.
    Addressed,
    /// Never dispatched a turn, but present in the conversation.
    Observer,
}

impl RespondPolicy {
    /// Wire token — must match `channels.RespondPolicy` constants in
    /// `internal/channels/channels.go`.
    pub(crate) fn as_wire_str(self) -> &'static str {
        match self {
            RespondPolicy::WhenMentioned => "when_mentioned",
            RespondPolicy::Always => "always",
            RespondPolicy::Never => "never",
            RespondPolicy::Participant => "participant",
            RespondPolicy::Chair => "chair",
            RespondPolicy::Addressed => "addressed",
            RespondPolicy::Observer => "observer",
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
    /// Create a new group channel (the server derives the `group:<name>` id)
    Create {
        /// Bare channel name (e.g. `planning`); the server derives `group:<name>`
        name: String,
        /// Optional channel description
        #[arg(long)]
        description: Option<String>,
        /// Add a member as `<id>` or `<id>:<disposition>` (repeatable; at least
        /// one required). Disposition ∈ when_mentioned (default) | always |
        /// never | participant | chair | addressed | observer.
        #[arg(long = "member")]
        member: Vec<String>,
        #[arg(long)]
        json: bool,
    },
    /// Show a channel's metadata, members, and their dispositions
    Info {
        /// Channel name (`planning`) or fully-qualified id (`group:planning`, `dm:a:b`)
        name: String,
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
        /// Response policy: `when_mentioned` (default), `always`, `never`, or
        /// the v0.3.8 conversation vocabulary `participant` / `chair` /
        /// `addressed` / `observer`. Validated client-side via the
        /// [`RespondPolicy`] enum so typos surface as a clap error before the
        /// server round-trip.
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
        /// Session id or label to publish under (RFC 0031 §E `--session`
        /// override). Resolves above `PERSATRIX_SESSION_ID` and the
        /// active-session file; an archived target warns but proceeds.
        #[arg(long)]
        session: Option<String>,
        /// Run/test-isolation epoch to publish under (ISSUE-0085 `--epoch`
        /// override). Resolves above `PERSATRIX_EPOCH`; strict-equality.
        #[arg(long)]
        epoch: Option<String>,
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
        /// Session id or label to publish under (RFC 0031 §E `--session`
        /// override). See `channel send --session`.
        #[arg(long)]
        session: Option<String>,
        /// Run/test-isolation epoch to publish under (ISSUE-0085 `--epoch`
        /// override). See `channel send --epoch`.
        #[arg(long)]
        epoch: Option<String>,
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
    /// Read or edit a channel's governance config (RFC 0050 Phase 1).
    ///
    /// Gated server-side behind the `config_edit_enabled` operator toggle
    /// (config/ui.yaml) — a `403` means the surface is off. Writes are
    /// optimistic-concurrency guarded: `set`/`unset` read the current revision
    /// and a concurrent edit surfaces as a conflict.
    Config {
        #[command(subcommand)]
        action: ConfigAction,
    },
    /// Convene an autonomous channel (RFC 0052 §B) — open a human-free
    /// discussion by dispatching the convene forced turn to the channel's
    /// configured `autonomous.convener`. Gated server-side behind the same
    /// `config_edit_enabled` toggle as `config` (`403` = off); `409` = the
    /// channel is not `autonomous.enabled`.
    Convene {
        /// Channel id (a bare name is canonicalized to `group:<name>`)
        name: String,
        /// Emit JSON instead of a human line
        #[arg(long)]
        json: bool,
    },
}

/// `persatrix channel config <action>` — the operator surface over PR 4's
/// `GET`/`PATCH /api/v1/channels/{id}/config`. A new variant compile-errors in
/// `dispatch` until its arm is added, keeping the parser and dispatch in lockstep.
#[derive(clap::Subcommand)]
pub(crate) enum ConfigAction {
    /// Show a channel's effective governance values, provenance, and revision
    Get {
        /// Channel name (`planning`) or fully-qualified id (`group:planning`)
        name: String,
        #[arg(long)]
        json: bool,
    },
    /// Set one or more knobs (`key=value`, space-separated, e.g.
    /// `floor_control=true end_vote_window=5`). Knobs ∈ floor_control,
    /// salience_max_channel_members, max_replies_per_participant_per_interaction,
    /// end_vote_threshold, end_vote_window, escalation_chair_id,
    /// interaction_idle_timeout_seconds, interaction_budget_tokens.
    Set {
        name: String,
        /// One or more `key=value` assignments (at least one required)
        #[arg(required = true)]
        assignments: Vec<String>,
        #[arg(long)]
        json: bool,
    },
    /// Clear one or more knobs back to inherit (space-separated knob names)
    Unset {
        name: String,
        /// One or more knob names to unset (at least one required)
        #[arg(required = true)]
        keys: Vec<String>,
        #[arg(long)]
        json: bool,
    },
    /// Regenerate a channel's YAML override block from the store, stamped
    /// `revision: store + 1` (the export-first foot-gun mitigation, RFC 0050).
    /// Emits only the explicitly-overridden knobs — inherited knobs are not
    /// frozen. Writes to `--out` if given, else stdout.
    Export {
        /// Channel name (`planning`) or fully-qualified id (`group:planning`)
        name: String,
        /// Write the YAML to this file instead of stdout
        #[arg(long)]
        out: Option<String>,
    },
    /// Apply each declared channel block in a YAML file through the
    /// optimistic-concurrency PATCH path (the live CLI writer — If-Match
    /// guarded, not the revision-gated boot loader). Validates the whole file
    /// before the first write.
    Import {
        /// Path to a YAML file (the `config/channels.yaml` shape)
        file: String,
        #[arg(long)]
        json: bool,
    },
    /// Compare a channel's declared YAML block against its effective store
    /// config and surface per-knob drift plus a revision comparison.
    Diff {
        /// Channel name (`planning`) or fully-qualified id (`group:planning`)
        name: String,
        /// Declared-config file to compare against (default: config/channels.yaml)
        #[arg(long)]
        file: Option<String>,
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
        ChannelCommands::Create {
            name,
            description,
            member,
            json,
        } => {
            cmd_channel_create(
                client,
                server,
                &name,
                description.as_deref().unwrap_or(""),
                &member,
                json,
            )
            .await
        }
        ChannelCommands::Info { name, json } => cmd_channel_info(client, server, &name, json).await,
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
            session,
            epoch,
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
                session.as_deref(),
                epoch.as_deref(),
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
            session,
            epoch,
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
                session.as_deref(),
                epoch.as_deref(),
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
        ChannelCommands::Config { action } => match action {
            ConfigAction::Get { name, json } => cmd_config_get(client, server, &name, json).await,
            ConfigAction::Set {
                name,
                assignments,
                json,
            } => cmd_config_set(client, server, &name, &assignments, json).await,
            ConfigAction::Unset { name, keys, json } => {
                cmd_config_unset(client, server, &name, &keys, json).await
            }
            ConfigAction::Export { name, out } => {
                cmd_config_export(client, server, &name, out.as_deref()).await
            }
            ConfigAction::Import { file, json } => {
                cmd_config_import(client, server, &file, json).await
            }
            ConfigAction::Diff { name, file, json } => {
                let file = file.as_deref().unwrap_or(DEFAULT_CHANNELS_YAML);
                cmd_config_diff(client, server, &name, file, json).await
            }
        },
        ChannelCommands::Convene { name, json } => cmd_convene(client, server, &name, json).await,
    }
}

#[cfg(test)]
mod tests {
    use super::RespondPolicy;
    use clap::ValueEnum;
    use std::collections::BTreeSet;

    // Internal-consistency check: every variant maps to its exact wire token,
    // and the value set is the same size as the mapping table — so a variant
    // added without a wire token (or vice versa) trips here. This pins the
    // variant↔token mapping; on its own it does NOT detect the *server* growing
    // a disposition (a hardcoded table stays green when channels.go gains one).
    // That drift is caught by `respond_policy_covers_server_vocabulary` below.
    #[test]
    fn respond_policy_wire_strings_are_stable() {
        let cases = [
            (RespondPolicy::WhenMentioned, "when_mentioned"),
            (RespondPolicy::Always, "always"),
            (RespondPolicy::Never, "never"),
            (RespondPolicy::Participant, "participant"),
            (RespondPolicy::Chair, "chair"),
            (RespondPolicy::Addressed, "addressed"),
            (RespondPolicy::Observer, "observer"),
        ];
        for (policy, wire) in cases {
            assert_eq!(policy.as_wire_str(), wire);
        }
        assert_eq!(RespondPolicy::value_variants().len(), cases.len());
    }

    // TRUE lockstep guard. The regression this surface exists to prevent is
    // "the client allowlist is narrower than the server's RespondPolicy
    // vocabulary". A hardcoded expected list cannot catch that — it stays green
    // when the server grows a disposition the client never learns about. So we
    // parse the disposition tokens straight out of the server's source of truth
    // (internal/channels/channels.go) and assert the CLI covers exactly that
    // set. Add a `chair`-like constant server-side and forget the CLI, and this
    // fails with the missing token named.
    #[test]
    fn respond_policy_covers_server_vocabulary() {
        // Resolved relative to the crate root so the path holds regardless of
        // the test's working directory.
        let go_src = std::fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../internal/channels/channels.go"
        ))
        .expect(
            "channels.go must be readable for the disposition lockstep guard; \
             update the path if internal/channels/channels.go moved",
        );

        // The constants are declared one per line inside the `const (...)`
        // block as `RespondWhenMentioned RespondPolicy = "when_mentioned"`.
        // `//`-comment lines that mention the type (e.g. `[RespondPolicy.
        // Normalize]`) never carry the literal `RespondPolicy = "<token>"`, and
        // `type RespondPolicy string` starts with `type` — so a trimmed line
        // that starts with `Respond` and bears a quoted token after
        // `RespondPolicy =` selects exactly the const declarations.
        let server: BTreeSet<String> = go_src
            .lines()
            .map(str::trim_start)
            .filter(|l| {
                l.starts_with("Respond") && l.contains("RespondPolicy") && l.contains("= \"")
            })
            .filter_map(|l| {
                let start = l.find('"')? + 1;
                let end = l[start..].find('"')? + start;
                Some(l[start..end].to_string())
            })
            .collect();

        assert!(
            !server.is_empty(),
            "parsed no RespondPolicy constants from channels.go — the const \
             declaration format changed; update this guard's parser",
        );

        let client: BTreeSet<String> = RespondPolicy::value_variants()
            .iter()
            .map(|p| p.as_wire_str().to_string())
            .collect();

        assert_eq!(
            client,
            server,
            "CLI --respond vocabulary drifted from channels.RespondPolicy: \
             missing from CLI = {:?}, extra in CLI = {:?}",
            server.difference(&client).collect::<Vec<_>>(),
            client.difference(&server).collect::<Vec<_>>(),
        );
    }
}
