mod active_session;
mod commands;
mod epoch_resolve;
mod session_resolve;
mod types;
mod user_id;
mod validation;

use clap::{Parser, Subcommand, ValueEnum};
use colored::Colorize;

use commands::agent::{cmd_agent_info, cmd_agent_list, cmd_agent_reload, cmd_test};
use commands::channel_dispatch::{dispatch as dispatch_channel, ChannelCommands};
use commands::chat::cmd_chat;
use commands::interactions::cmd_agent_interactions;
use commands::logs::{cmd_logs, LogsOptions};
use commands::session::{dispatch as dispatch_session, SessionCommands};
use commands::validate::cmd_validate;
use commands::workflow::{cmd_run, cmd_status};
use user_id::default_user_id;

/// Persatrix CLI — manage agents, workflows, and the mesh.
#[derive(Parser)]
#[command(name = "persatrix", version, about)]
struct Cli {
    /// Orchestrator server address
    #[arg(long, default_value = "http://localhost:8080", global = true)]
    server: String,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Execute a workflow
    Run {
        /// Workflow file or ID
        workflow: String,
        /// Input payload
        #[arg(short, long)]
        input: Option<String>,
        /// Optimization profile (cost, speed, quality, simulation)
        #[arg(long, default_value = "default")]
        profile: String,
    },
    /// Validate YAML configuration files
    Validate {
        /// Config directory or file path
        #[arg(default_value = "config/")]
        path: String,
        /// Treat warnings as errors
        #[arg(long)]
        strict: bool,
    },
    /// Run tests
    Test {
        /// Test a specific agent
        #[arg(long)]
        agent: Option<String>,
        /// Test a specific workflow
        #[arg(long)]
        workflow: Option<String>,
        /// Test persona consistency
        #[arg(long)]
        persona: Option<String>,
        /// Record LLM responses for replay
        #[arg(long)]
        record: bool,
    },
    /// Manage agents
    #[command(subcommand)]
    Agent(AgentCommands),
    /// View execution status and logs
    Status {
        /// Execution ID (omit to list all runs)
        execution_id: Option<String>,
    },
    /// View execution logs
    ///
    /// Use the literal execution ID `_` for a chronological cross-execution
    /// merge view (e.g. `persatrix logs _ --since 5m --level WARN`).
    Logs {
        /// Execution ID (use `_` for the cross-execution merged view)
        execution_id: String,
        /// Follow log output (Server-Sent Events stream)
        #[arg(short, long)]
        follow: bool,
        /// Show full structured payload (execution/step/trace IDs and attributes)
        #[arg(short, long)]
        verbose: bool,
        /// Lower bound on entry timestamp. Accepts a Go duration ("5m",
        /// "1h30m") or an RFC 3339 timestamp.
        #[arg(long)]
        since: Option<String>,
        /// Filter by workflow ID (matches `attributes["workflow"]`)
        #[arg(long)]
        workflow: Option<String>,
        /// Filter by log level
        #[arg(long, value_enum)]
        level: Option<LogLevel>,
        /// Filter to entries whose `trace_id` matches (client-side filter for
        /// log↔trace correlation; see [RFC 0019 § G](docs/rfcs/0019-opentelemetry-completion.md#g-logtrace-correlation)).
        #[arg(long)]
        trace: Option<String>,
        /// Filter by agent (kept for back-compat; matches `attributes["agent_id"]`)
        #[arg(long)]
        agent: Option<String>,
    },
    /// Chat with a persona agent
    Chat {
        /// Agent ID to chat with
        agent_id: String,
        /// User identity for the conversation (defaults to OS username, normalized to
        /// lowercase alphanumeric + hyphens to satisfy the resource-ID contract).
        #[arg(long)]
        user: Option<String>,
        /// Persona-memory session id or label to converse under (RFC 0031 §E
        /// `--session` override). Resolves above `PERSATRIX_SESSION_ID` and the
        /// active-session file; an archived target warns but proceeds.
        #[arg(long)]
        session: Option<String>,
        /// Run/test-isolation epoch to converse under (ISSUE-0085 `--epoch`
        /// override). Resolves above `PERSATRIX_EPOCH`; strict-equality
        /// isolation (no `legacy` carve-out, no `*` wildcard) — a fresh epoch
        /// inherits none of a prior run's memory.
        #[arg(long)]
        epoch: Option<String>,
    },
    /// Manage blueprints
    Init {
        /// Blueprint name (software-team, social-experiment)
        #[arg(long)]
        blueprint: String,
        /// Output directory
        #[arg(default_value = ".")]
        output: String,
    },
    /// Session replay
    Replay {
        /// Session ID
        session_id: String,
        /// Filter to one agent's perspective
        #[arg(long)]
        agent: Option<String>,
        /// Start from a specific step
        #[arg(long)]
        from: Option<String>,
        /// Export format (json, agentops)
        #[arg(long)]
        export: Option<String>,
    },
    /// Cost reports
    Cost {
        /// Time period (today, week, month)
        #[arg(default_value = "today")]
        period: String,
        /// Group by (agent, workflow, model)
        #[arg(long, default_value = "agent")]
        group_by: String,
    },
    /// Manage persona-memory sessions (RFC 0031 §E)
    #[command(subcommand)]
    Session(SessionCommands),
    /// State management
    #[command(subcommand)]
    State(StateCommands),
    /// Manage internal channels (RFC 0011 §F)
    #[command(subcommand)]
    Channel(ChannelCommands),
    /// Manage mesh nodes (v0.3+)
    #[command(subcommand)]
    Node(NodeCommands),
    /// Mesh status and diagnostics (v0.3+)
    #[command(subcommand)]
    Mesh(MeshCommands),
}

#[derive(Copy, Clone, Debug, PartialEq, Eq, ValueEnum)]
#[clap(rename_all = "UPPER")]
pub(crate) enum LogLevel {
    Debug,
    Info,
    Warn,
    Error,
}

impl LogLevel {
    /// Wire value sent to the orchestrator REST/SSE endpoints, which expect
    /// the uppercase severity tokens enumerated by [`logsRequest`].
    pub(crate) fn as_wire_str(self) -> &'static str {
        match self {
            LogLevel::Debug => "DEBUG",
            LogLevel::Info => "INFO",
            LogLevel::Warn => "WARN",
            LogLevel::Error => "ERROR",
        }
    }
}

#[derive(Subcommand)]
enum AgentCommands {
    /// List all registered agents
    List,
    /// Show agent details
    Info { agent_id: String },
    /// Reload agent config without restart
    Reload {
        agent_id: String,
        #[arg(long)]
        config: Option<String>,
    },
    /// Show an agent's closed-interaction summaries (newest-first).
    ///
    /// The CLI half of the v0.3.8 interaction-summary surface (RFC 0020
    /// §C/§D): when a brainstorm interaction closes — by end-vote, by the
    /// RFC 0030 cost ceiling, or by going idle — the persona persists a
    /// synthesised summary; this prints it so a converged conversation hands
    /// back a readable outcome from the terminal.
    Interactions {
        /// Agent whose closed interactions to read
        agent_id: String,
        /// Restrict to one conversation scope (e.g. `group:planning`, a DM id)
        #[arg(long)]
        scope: Option<String>,
        /// Filter by id — the agent-side episode id, or the channel governance
        /// interaction id (e.g. from an end-vote close log; may match several)
        #[arg(long = "interaction-id")]
        interaction_id: Option<String>,
        /// Max interactions to fetch (default: 20; newest-first)
        #[arg(long, default_value_t = commands::interactions::DEFAULT_CLOSED_INTERACTIONS_LIMIT)]
        limit: u32,
        /// Exclude degenerate rows below this turn count (server floor; must be ≥ 1)
        #[arg(long)]
        min_turns: Option<u32>,
        /// Emit JSON instead of human-readable blocks
        #[arg(long)]
        json: bool,
    },
}

#[derive(Subcommand)]
enum StateCommands {
    /// Export full state
    Export {
        #[arg(long)]
        output: String,
        #[arg(long)]
        anonymize: bool,
    },
    /// Restore from export
    Restore { path: String },
    /// Create a named checkpoint
    Checkpoint {
        #[arg(long)]
        name: Option<String>,
    },
    /// List available checkpoints
    Checkpoints,
}

#[derive(Subcommand)]
enum NodeCommands {
    /// Register a new node
    Register {
        #[arg(long)]
        config: String,
    },
    /// List all nodes
    List,
    /// Show node status
    Status { node_id: String },
    /// Drain agents from a node
    Drain { node_id: String },
}

#[derive(Subcommand)]
enum MeshCommands {
    /// Show mesh topology and health
    Status,
    /// Ping a node
    Ping { node_id: String },
    /// Trace routing path between two agents
    Trace {
        from_agent: String,
        to_agent: String,
    },
}

#[tokio::main]
async fn main() {
    let cli = Cli::parse();
    let server = cli.server.trim_end_matches('/');

    let server_lower = server.to_lowercase();
    if !server_lower.starts_with("http://") && !server_lower.starts_with("https://") {
        eprintln!(
            "{} --server must start with http:// or https://",
            "error:".red().bold()
        );
        std::process::exit(1);
    }

    let client = reqwest::Client::builder()
        .connect_timeout(std::time::Duration::from_secs(10))
        .timeout(std::time::Duration::from_secs(60))
        .build()
        .expect("failed to create HTTP client");

    // Exhaustive match — adding a Commands variant produces a compile error
    // until its handler is added.
    let result = match cli.command {
        Commands::Run {
            workflow,
            input,
            profile,
        } => cmd_run(&client, server, &workflow, input.as_deref(), &profile).await,
        Commands::Status { execution_id } => {
            cmd_status(&client, server, execution_id.as_deref()).await
        }
        Commands::Agent(cmd) => match cmd {
            AgentCommands::List => cmd_agent_list(&client, server).await,
            AgentCommands::Info { agent_id } => cmd_agent_info(&client, server, &agent_id).await,
            AgentCommands::Reload {
                agent_id,
                config: _,
            } => cmd_agent_reload(&agent_id).await,
            AgentCommands::Interactions {
                agent_id,
                scope,
                interaction_id,
                limit,
                min_turns,
                json,
            } => {
                cmd_agent_interactions(
                    &client,
                    server,
                    &agent_id,
                    scope.as_deref(),
                    interaction_id.as_deref(),
                    limit,
                    min_turns,
                    json,
                )
                .await
            }
        },
        Commands::Logs {
            execution_id,
            follow,
            verbose,
            since,
            workflow,
            level,
            trace,
            agent,
        } => {
            cmd_logs(
                &client,
                server,
                &execution_id,
                LogsOptions {
                    follow,
                    verbose,
                    since: since.as_deref(),
                    workflow: workflow.as_deref(),
                    level: level.map(LogLevel::as_wire_str),
                    trace: trace.as_deref(),
                    agent: agent.as_deref(),
                },
            )
            .await
        }
        Commands::Chat {
            agent_id,
            user,
            session,
            epoch,
        } => {
            // Resolve user identity: explicit --user flag first, otherwise
            // fall back to the shared OS-username derivation (see
            // [`default_user_id`]).
            let user_id = user.unwrap_or_else(default_user_id);
            cmd_chat(
                &client,
                server,
                &agent_id,
                &user_id,
                session.as_deref(),
                epoch.as_deref(),
            )
            .await
        }
        Commands::Validate { path, strict } => cmd_validate(&path, strict).await,
        Commands::Test {
            agent,
            workflow,
            persona,
            record,
        } => {
            cmd_test(
                &client,
                server,
                agent.as_deref(),
                workflow.as_deref(),
                persona.as_deref(),
                record,
            )
            .await
        }
        Commands::Init { .. } => {
            println!("{}", "Command 'init' not yet implemented".yellow());
            Ok(())
        }
        Commands::Replay { .. } => {
            println!("{}", "Command 'replay' not yet implemented".yellow());
            Ok(())
        }
        Commands::Cost { .. } => {
            println!("{}", "Command 'cost' not yet implemented".yellow());
            Ok(())
        }
        Commands::State(_) => {
            println!("{}", "Command 'state' not yet implemented".yellow());
            Ok(())
        }
        Commands::Channel(cmd) => dispatch_channel(&client, server, cmd, default_user_id).await,
        Commands::Session(cmd) => dispatch_session(&client, server, cmd).await,
        Commands::Node(_) => {
            println!("{}", "Command 'node' not yet implemented".yellow());
            Ok(())
        }
        Commands::Mesh(_) => {
            println!("{}", "Command 'mesh' not yet implemented".yellow());
            Ok(())
        }
    };

    if let Err(e) = result {
        eprintln!("{} {e}", "error:".red().bold());
        std::process::exit(1);
    }
}
