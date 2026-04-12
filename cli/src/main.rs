use std::collections::HashMap;

use clap::{Parser, Subcommand};
use colored::Colorize;
use serde::{Deserialize, Serialize};
use tabled::{Table, Tabled};

/// Orchestr8 CLI — manage agents, workflows, and the mesh.
#[derive(Parser)]
#[command(name = "orch", version, about)]
struct Cli {
    /// Orchestrator server address
    #[arg(long, default_value = "http://localhost:8080", global = true)]
    server: String,

    #[command(subcommand)]
    command: Commands,
}

// ─── API request/response types ──────────────────────────────────────────

#[derive(Serialize)]
struct SubmitWorkflowRequest {
    workflow_id: String,
    /// Matches Go server's `map[string]string` — typed precisely to catch
    /// non-string values on the client side instead of round-tripping a 400.
    #[serde(skip_serializing_if = "Option::is_none")]
    inputs: Option<HashMap<String, String>>,
}

#[derive(Deserialize)]
struct SubmitWorkflowResponse {
    run_id: String,
    workflow_id: String,
    status: String,
}

/// Response fields intentionally use `Option` and no `#[serde(deny_unknown_fields)]`
/// so the CLI stays forward-compatible when the server adds new fields (e.g. `steps`).
#[derive(Deserialize, Tabled)]
struct WorkflowRunResponse {
    run_id: String,
    workflow_id: String,
    status: String,
    #[tabled(display_with = "fmt_option")]
    error: Option<String>,
    #[tabled(display_with = "fmt_option")]
    started_at: Option<String>,
    #[tabled(display_with = "fmt_option")]
    finished_at: Option<String>,
}

#[derive(Deserialize, Tabled)]
struct AgentResponse {
    id: String,
    address: String,
    #[tabled(display_with = "fmt_vec")]
    capabilities: Vec<String>,
    status: String,
}

#[derive(Deserialize)]
struct ApiError {
    error: String,
    #[allow(dead_code)]
    code: Option<String>,
}

fn fmt_option(val: &Option<String>) -> String {
    match val {
        Some(s) => s.clone(),
        None => "\u{2014}".to_string(),
    }
}

fn fmt_vec(val: &[String]) -> String {
    if val.is_empty() {
        "\u{2014}".to_string()
    } else {
        val.join(", ")
    }
}

// ─── HTTP helper ─────────────────────────────────────────────────────────

async fn api_error_message(resp: reqwest::Response) -> String {
    let status = resp.status();
    match resp.json::<ApiError>().await {
        Ok(e) => format!("{}: {}", status, e.error),
        Err(_) => format!("HTTP {status}"),
    }
}

/// Reject path parameters that could cause path-traversal or query-injection
/// when interpolated into URLs. Defense-in-depth — the server also validates
/// IDs, but failing fast here gives the user a clearer error message.
fn validate_path_param(value: &str, label: &str) -> Result<(), String> {
    if value.is_empty() {
        return Err(format!("{label} cannot be empty"));
    }
    if value.contains('/')
        || value.contains('\\')
        || value.contains("..")
        || value.contains('?')
        || value.contains('#')
    {
        return Err(format!(
            "invalid {label}: contains characters not allowed in URL path"
        ));
    }
    Ok(())
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
        /// Execution ID (omit for latest)
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

    // ─── v0.3: Mesh commands ─────────────────────────
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
    let client = reqwest::Client::builder()
        .connect_timeout(std::time::Duration::from_secs(10))
        .timeout(std::time::Duration::from_secs(60))
        .build()
        .expect("failed to create HTTP client");

    let result = match cli.command {
        Commands::Run {
            workflow,
            input,
            profile,
        } => {
            if profile != "default" {
                eprintln!(
                    "{}",
                    "warning: --profile is not yet supported by the server, ignored".yellow()
                );
            }
            cmd_run(&client, server, &workflow, input.as_deref()).await
        }

        Commands::Status { execution_id } => {
            cmd_status(&client, server, execution_id.as_deref()).await
        }

        Commands::Agent(cmd) => match cmd {
            AgentCommands::List => cmd_agent_list(&client, server).await,
            AgentCommands::Info { agent_id } => cmd_agent_info(&client, server, &agent_id).await,
            AgentCommands::Reload {
                agent_id: _,
                config: _,
            } => {
                println!("{}", "Agent reload not yet implemented".yellow());
                Ok(())
            }
        },

        Commands::Logs {
            execution_id,
            follow,
            agent,
        } => {
            if follow {
                eprintln!(
                    "{}",
                    "warning: --follow is not yet supported, ignored".yellow()
                );
            }
            if agent.is_some() {
                eprintln!(
                    "{}",
                    "warning: --agent filter is not yet supported, ignored".yellow()
                );
            }
            cmd_logs(&client, server, &execution_id).await
        }

        // Exhaustive match instead of catch-all `_ =>` so that adding a new
        // Commands variant produces a compile error until its handler is added.
        Commands::Validate { path, strict } => {
            println!(
                "{}",
                format!("Validating config: {path} (strict: {strict}) — not yet implemented")
                    .yellow()
            );
            Ok(())
        }
        Commands::Test { .. } => {
            println!("{}", "Command 'test' not yet implemented".yellow());
            Ok(())
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

// ─── Command implementations ─────────────────────────────────────────────

async fn cmd_run(
    client: &reqwest::Client,
    server: &str,
    workflow: &str,
    input: Option<&str>,
) -> Result<(), String> {
    let inputs: Option<HashMap<String, String>> = match input {
        Some(raw) => Some(serde_json::from_str(raw).map_err(|e| {
            format!("invalid --input JSON (expected {{\"key\": \"value\", ...}}): {e}")
        })?),
        None => None,
    };

    let body = SubmitWorkflowRequest {
        workflow_id: workflow.to_string(),
        inputs,
    };

    let resp = client
        .post(format!("{server}/api/v1/workflows/run"))
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;

    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }

    let data: SubmitWorkflowResponse = resp
        .json()
        .await
        .map_err(|e| format!("invalid response: {e}"))?;

    println!(
        "{} Workflow {} submitted (run_id: {})",
        "✓".green().bold(),
        data.workflow_id.bold(),
        data.run_id
    );
    println!("  Status: {}", data.status);
    Ok(())
}

async fn cmd_status(
    client: &reqwest::Client,
    server: &str,
    execution_id: Option<&str>,
) -> Result<(), String> {
    match execution_id {
        Some(id) => {
            validate_path_param(id, "execution ID")?;
            let resp = client
                .get(format!("{server}/api/v1/workflows/{id}/status"))
                .send()
                .await
                .map_err(|e| format!("connection failed: {e}"))?;

            if !resp.status().is_success() {
                return Err(api_error_message(resp).await);
            }

            let run: WorkflowRunResponse = resp
                .json()
                .await
                .map_err(|e| format!("invalid response: {e}"))?;

            println!("{:<14} {}", "Run ID:".bold(), run.run_id);
            println!("{:<14} {}", "Workflow:".bold(), run.workflow_id);
            println!("{:<14} {}", "Status:".bold(), colorize_status(&run.status));
            if let Some(ref err) = run.error {
                println!("{:<14} {}", "Error:".bold(), err.red());
            }
            if let Some(ref t) = run.started_at {
                println!("{:<14} {}", "Started:".bold(), t);
            }
            if let Some(ref t) = run.finished_at {
                println!("{:<14} {}", "Finished:".bold(), t);
            }
        }
        None => {
            let resp = client
                .get(format!("{server}/api/v1/workflows"))
                .send()
                .await
                .map_err(|e| format!("connection failed: {e}"))?;

            if !resp.status().is_success() {
                return Err(api_error_message(resp).await);
            }

            let runs: Vec<WorkflowRunResponse> = resp
                .json()
                .await
                .map_err(|e| format!("invalid response: {e}"))?;

            if runs.is_empty() {
                println!("No workflow runs found.");
            } else {
                println!("{}", Table::new(&runs));
            }
        }
    }
    Ok(())
}

async fn cmd_agent_list(client: &reqwest::Client, server: &str) -> Result<(), String> {
    let resp = client
        .get(format!("{server}/api/v1/agents"))
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;

    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }

    let agents: Vec<AgentResponse> = resp
        .json()
        .await
        .map_err(|e| format!("invalid response: {e}"))?;

    if agents.is_empty() {
        println!("No agents registered.");
    } else {
        println!("{}", Table::new(&agents));
    }
    Ok(())
}

async fn cmd_agent_info(
    client: &reqwest::Client,
    server: &str,
    agent_id: &str,
) -> Result<(), String> {
    validate_path_param(agent_id, "agent ID")?;
    let resp = client
        .get(format!("{server}/api/v1/agents/{agent_id}"))
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;

    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }

    let agent: AgentResponse = resp
        .json()
        .await
        .map_err(|e| format!("invalid response: {e}"))?;

    println!("{:<16} {}", "ID:".bold(), agent.id);
    println!("{:<16} {}", "Address:".bold(), agent.address);
    println!(
        "{:<16} {}",
        "Status:".bold(),
        colorize_status(&agent.status)
    );
    println!(
        "{:<16} {}",
        "Capabilities:".bold(),
        if agent.capabilities.is_empty() {
            "—".to_string()
        } else {
            agent.capabilities.join(", ")
        }
    );
    Ok(())
}

async fn cmd_logs(
    client: &reqwest::Client,
    server: &str,
    execution_id: &str,
) -> Result<(), String> {
    validate_path_param(execution_id, "execution ID")?;
    let resp = client
        .get(format!("{server}/api/v1/executions/{execution_id}/logs"))
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;

    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }

    let body = resp
        .text()
        .await
        .map_err(|e| format!("failed to read response: {e}"))?;

    println!("{body}");
    Ok(())
}

fn colorize_status(status: &str) -> colored::ColoredString {
    match status {
        "completed" => status.green(),
        "running" | "pending" => status.cyan(),
        "failed" => status.red(),
        "cancelled" => status.yellow(),
        "retrying" => status.yellow(),
        "healthy" => status.green(),
        "degraded" => status.yellow(),
        "offline" => status.red(),
        _ => status.normal(),
    }
}
