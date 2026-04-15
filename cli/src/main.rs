mod commands;
mod types;

use clap::{Parser, Subcommand};
use colored::Colorize;

use commands::agent::{cmd_agent_info, cmd_agent_list, cmd_agent_reload, cmd_test};
use commands::logs::cmd_logs;
use commands::validate::cmd_validate;
use commands::workflow::{cmd_run, cmd_status};

/// Persatrix CLI — manage agents, workflows, and the mesh.
#[derive(Parser)]
#[command(name = "orch", version, about)]
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
    Logs {
        /// Execution ID
        execution_id: String,
        /// Follow log output
        #[arg(short, long)]
        follow: bool,
        /// Filter by agent
        #[arg(long)]
        agent: Option<String>,
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
    /// State management
    #[command(subcommand)]
    State(StateCommands),
    /// Manage mesh nodes (v0.3+)
    #[command(subcommand)]
    Node(NodeCommands),
    /// Mesh status and diagnostics (v0.3+)
    #[command(subcommand)]
    Mesh(MeshCommands),
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
        },
        Commands::Logs {
            execution_id,
            follow,
            agent,
        } => cmd_logs(&client, server, &execution_id, follow, agent.as_deref()).await,
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
