use std::collections::HashMap;

use clap::{Parser, Subcommand};
use colored::Colorize;
use serde::{Deserialize, Serialize};
use tabled::{Table, Tabled};
use tokio::process::Command as ProcessCommand;

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
    /// Agent type (e.g. "task", "persona"). Optional for forward-compatibility
    /// with v0.1 servers that don't return this field.
    #[serde(default)]
    #[tabled(skip)]
    agent_type: Option<String>,
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
        || value.contains('%')
    {
        return Err(format!(
            "invalid {label}: contains characters not allowed in URL path"
        ));
    }
    Ok(())
}

/// Validate that a resource ID matches the cross-component contract
/// `^[a-z0-9][a-z0-9-]*[a-z0-9]$` shared with the Go orchestrator registry.
/// Catches malformed IDs early with a clear client-side error message instead
/// of round-tripping to the server.
/// (F-1b-1: workflow ID not validated before HTTP call.)
fn validate_resource_id(value: &str, label: &str) -> Result<(), String> {
    if value.is_empty() {
        return Err(format!("{label} cannot be empty"));
    }
    // Single-character IDs are valid per updated schema
    // (^[a-z0-9]([a-z0-9-]*[a-z0-9])?$).
    let bytes = value.as_bytes();
    if !bytes[0].is_ascii_lowercase() && !bytes[0].is_ascii_digit() {
        return Err(format!(
            "invalid {label} {value:?}: must start with lowercase letter or digit"
        ));
    }
    if bytes.len() > 1 {
        let last = bytes[bytes.len() - 1];
        if !last.is_ascii_lowercase() && !last.is_ascii_digit() {
            return Err(format!(
                "invalid {label} {value:?}: must end with lowercase letter or digit"
            ));
        }
        for &b in &bytes[1..bytes.len() - 1] {
            if !b.is_ascii_lowercase() && !b.is_ascii_digit() && b != b'-' {
                return Err(format!(
                    "invalid {label} {value:?}: only lowercase letters, digits, and hyphens allowed"
                ));
            }
        }
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

    // F-1b-4: case-insensitive URL scheme check — `HTTP://localhost` was rejected.
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
            // F-1b-1: validate workflow ID format before HTTP call.
            if let Err(e) = validate_resource_id(&workflow, "workflow ID") {
                Err(e)
            } else {
                cmd_run(&client, server, &workflow, input.as_deref()).await
            }
        }

        Commands::Status { execution_id } => {
            cmd_status(&client, server, execution_id.as_deref()).await
        }

        Commands::Agent(cmd) => match cmd {
            AgentCommands::List => cmd_agent_list(&client, server).await,
            AgentCommands::Info { agent_id } => cmd_agent_info(&client, server, &agent_id).await,
            // F-1b-3: capture agent_id in reload stub message.
            AgentCommands::Reload {
                agent_id,
                config: _,
            } => {
                println!(
                    "{}",
                    format!("Agent reload for '{}' not yet implemented", agent_id).yellow()
                );
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
        Commands::Validate { path, strict } => cmd_validate(&path, strict).await,
        Commands::Test {
            agent,
            workflow,
            persona,
            record,
        } => {
            if let Some(ref id) = persona {
                // F-6b-4: Warn when extra test flags are silently ignored.
                if agent.is_some() || workflow.is_some() || record {
                    eprintln!(
                        "{}",
                        "warning: --persona takes precedence; --agent/--workflow/--record ignored"
                            .yellow()
                    );
                }
                cmd_test_persona(&client, server, id).await
            } else if agent.is_some() || workflow.is_some() || record {
                println!(
                    "{}",
                    "Only --persona is implemented. --agent, --workflow, and --record are not yet supported.".yellow()
                );
                Ok(())
            } else {
                println!(
                    "{}",
                    "No test type specified. Available: --persona <id> (more coming soon)".yellow()
                );
                Ok(())
            }
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

// ─── Validate command ────────────────────────────────────────────────────

// NOTE: `validate` is the only CLI command that runs locally (subprocess) instead
// of via the orchestrator REST API. This deviates from the thin-client pattern.
// A server-side POST /api/v1/config/validate endpoint would be architecturally
// consistent — tracked for future improvement. (F-6b-3)
async fn cmd_validate(path: &str, strict: bool) -> Result<(), String> {
    // F-6b-R1: validate empty path before passing to subprocess.
    if path.is_empty() {
        return Err("validation path cannot be empty".to_string());
    }

    // F-6b-1: Python validator does not implement --strict yet.
    if strict {
        eprintln!(
            "{}",
            "warning: --strict is not yet supported by the Python validator, ignored".yellow()
        );
    }

    let script = find_validator_script()?;
    let python = find_python_binary();
    let args = vec![script.to_string_lossy().to_string(), path.to_string()];

    // F-6b-2: Use async subprocess to avoid blocking a tokio worker thread.
    // F-6b-6: Timeout prevents indefinite hang if the Python process stalls.
    let mut cmd = ProcessCommand::new(python);
    cmd.args(&args);
    let output = tokio::time::timeout(std::time::Duration::from_secs(120), cmd.output())
        .await
        .map_err(|_| "Python validator timed out after 120 seconds".to_string())?
        .map_err(|e| {
            // F-6b-R4: OS error from cmd.output() doesn't mention Python by name.
            if e.kind() == std::io::ErrorKind::NotFound {
                python_not_found_message()
            } else {
                format!("failed to run Python validator: {e}")
            }
        })?;

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);

    if !stdout.is_empty() {
        print!("{stdout}");
    }
    if !stderr.is_empty() {
        eprint!("{stderr}");
    }

    if output.status.success() {
        Ok(())
    } else {
        Err("validation failed".to_string())
    }
}

fn find_validator_script() -> Result<std::path::PathBuf, String> {
    // Try relative to CWD first (most common: running from repo root)
    let cwd_relative = std::path::PathBuf::from("agents/validate.py");
    if cwd_relative.exists() {
        // F-6b-R5: canonicalize discovered path — removes `..` components
        // from error messages and log output.
        return std::fs::canonicalize(&cwd_relative)
            .map_err(|e| format!("failed to canonicalize {}: {e}", cwd_relative.display()));
    }

    // Try relative to the executable location (installed or bin/ layout)
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            let from_bin = parent.join("../agents/validate.py");
            if from_bin.exists() {
                // F-6b-R5: canonicalize here too.
                return std::fs::canonicalize(&from_bin)
                    .map_err(|e| format!("failed to canonicalize {}: {e}", from_bin.display()));
            }
        }
    }

    Err("cannot find agents/validate.py — run from the repository root".to_string())
}

/// Return the Python interpreter binary name for the current platform.
/// Windows: `python` (standard name via installer or py launcher).
/// Unix/macOS: `python3` is preferred — `python` may be absent or
/// Python 2 on some Linux distributions. (F-6b-7)
fn find_python_binary() -> &'static str {
    if cfg!(windows) {
        "python"
    } else {
        "python3"
    }
}

/// F-6b-R4: diagnostic error message when Python is not found.
fn python_not_found_message() -> String {
    let binary = find_python_binary();
    format!("Python not found. Install Python 3.11+ and ensure '{binary}' is on PATH.")
}

// ─── Test persona command ────────────────────────────────────────────────

async fn cmd_test_persona(
    client: &reqwest::Client,
    server: &str,
    agent_id: &str,
) -> Result<(), String> {
    validate_path_param(agent_id, "agent ID")?;

    println!(
        "{} Testing persona agent: {}",
        "→".cyan().bold(),
        agent_id.bold()
    );

    // Fetch agent info from the orchestrator
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

    let mut warnings: Vec<String> = Vec::new();
    let mut checks_passed: u32 = 0;
    // F-6b-R2: dynamic check counter — adding/removing a check no longer
    // requires updating a separate hardcoded total.
    let mut total_checks: u32 = 0;

    // Check 1: Agent exists and is reachable
    total_checks += 1;
    println!(
        "  {} Agent '{}' found (status: {})",
        "✓".green(),
        agent.id,
        colorize_status(&agent.status)
    );
    checks_passed += 1;

    // Check 2: Agent status is healthy
    total_checks += 1;
    if agent.status == "healthy" {
        println!("  {} Agent is healthy", "✓".green());
        checks_passed += 1;
    } else {
        println!(
            "  {} Agent status is '{}', expected 'healthy'",
            "✗".red(),
            agent.status
        );
        warnings.push(format!("agent status is '{}', not 'healthy'", agent.status));
    }

    // Check 3: Agent type is persona
    // F-6b-R3: handle missing agent_type (v0.1 servers) gracefully.
    total_checks += 1;
    match agent.agent_type.as_deref() {
        Some("persona") => {
            println!("  {} Agent type is 'persona'", "✓".green());
            checks_passed += 1;
        }
        Some(other) => {
            println!(
                "  {} Agent type is '{}', expected 'persona'",
                "✗".red(),
                other
            );
            warnings.push(format!("agent type is '{other}', not 'persona'"));
        }
        None => {
            println!(
                "  {} Agent type unknown (server may not support type field)",
                "?".yellow()
            );
            warnings.push("agent type unknown — server may not support the type field".to_string());
        }
    }

    // Check 4: Agent has capabilities
    total_checks += 1;
    if !agent.capabilities.is_empty() {
        println!(
            "  {} Agent has {} capability(ies): {}",
            "✓".green(),
            agent.capabilities.len(),
            agent.capabilities.join(", ")
        );
        checks_passed += 1;
    } else {
        println!("  {} Agent has no capabilities", "!".yellow());
        warnings.push("agent has no capabilities".to_string());
    }

    // Summary
    println!();
    if warnings.is_empty() {
        println!(
            "{} All {total_checks} checks passed for '{}'",
            "✓".green().bold(),
            agent_id.bold()
        );
        Ok(())
    } else {
        println!(
            "{} {checks_passed}/{total_checks} checks passed for '{}' ({} warning(s))",
            "!".yellow().bold(),
            agent_id.bold(),
            warnings.len()
        );
        for w in &warnings {
            println!("  {} {w}", "warning:".yellow());
        }
        Ok(())
    }
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validate_path_param_rejects_empty() {
        assert!(validate_path_param("", "test").is_err());
    }

    #[test]
    fn validate_path_param_rejects_traversal() {
        assert!(validate_path_param("../etc/passwd", "test").is_err());
        assert!(validate_path_param("foo/bar", "test").is_err());
        assert!(validate_path_param("foo\\bar", "test").is_err());
    }

    #[test]
    fn validate_path_param_rejects_query_fragment_injection() {
        assert!(validate_path_param("id?admin=true", "test").is_err());
        assert!(validate_path_param("id#fragment", "test").is_err());
    }

    #[test]
    fn validate_path_param_rejects_percent_encoding() {
        assert!(validate_path_param("id%2Ftraversal", "test").is_err());
        assert!(validate_path_param("%00null", "test").is_err());
    }

    #[test]
    fn validate_path_param_accepts_valid_ids() {
        assert!(validate_path_param("my-agent-01", "test").is_ok());
        assert!(validate_path_param("abc", "test").is_ok());
        assert!(validate_path_param("550e8400-e29b-41d4-a716-446655440000", "test").is_ok());
    }

    // ─── F-1b-1: validate_resource_id tests ──────────────────────────────

    #[test]
    fn validate_resource_id_accepts_valid_ids() {
        assert!(validate_resource_id("a", "id").is_ok());
        assert!(validate_resource_id("a1", "id").is_ok());
        assert!(validate_resource_id("my-agent-01", "id").is_ok());
        assert!(validate_resource_id("abc", "id").is_ok());
        assert!(validate_resource_id("code-reviewer", "id").is_ok());
    }

    #[test]
    fn validate_resource_id_rejects_empty() {
        assert!(validate_resource_id("", "id").is_err());
    }

    #[test]
    fn validate_resource_id_rejects_uppercase() {
        assert!(validate_resource_id("MyAgent", "id").is_err());
        assert!(validate_resource_id("AGENT", "id").is_err());
    }

    #[test]
    fn validate_resource_id_rejects_special_chars() {
        assert!(validate_resource_id("my_agent", "id").is_err());
        assert!(validate_resource_id("my agent", "id").is_err());
        assert!(validate_resource_id("agent.1", "id").is_err());
    }

    #[test]
    fn validate_resource_id_rejects_leading_trailing_hyphen() {
        assert!(validate_resource_id("-agent", "id").is_err());
        assert!(validate_resource_id("agent-", "id").is_err());
    }

    // ─── F-1b-2: serde contract test ─────────────────────────────────────

    #[test]
    fn submit_workflow_request_serializes_correctly() {
        let req = SubmitWorkflowRequest {
            workflow_id: "my-workflow".to_string(),
            inputs: Some(HashMap::from([("key1".to_string(), "value1".to_string())])),
        };
        let json = serde_json::to_value(&req).unwrap();
        assert_eq!(json["workflow_id"], "my-workflow");
        assert_eq!(json["inputs"]["key1"], "value1");
        // Verify exact field names match the Go server's API contract
        assert!(json.get("workflow_id").is_some());
        assert!(json.get("inputs").is_some());
    }

    #[test]
    fn submit_workflow_request_omits_none_inputs() {
        let req = SubmitWorkflowRequest {
            workflow_id: "test".to_string(),
            inputs: None,
        };
        let json = serde_json::to_value(&req).unwrap();
        assert_eq!(json["workflow_id"], "test");
        // skip_serializing_if = "Option::is_none" should omit inputs
        assert!(json.get("inputs").is_none());
    }

    // ─── F-6b-R6: find_python_binary tests ───────────────────────────────

    #[test]
    fn find_python_binary_returns_platform_appropriate() {
        let binary = find_python_binary();
        if cfg!(windows) {
            assert_eq!(binary, "python");
        } else {
            assert_eq!(binary, "python3");
        }
    }

    #[test]
    fn python_not_found_message_contains_binary_name() {
        let msg = python_not_found_message();
        assert!(msg.contains("Python not found"));
        assert!(msg.contains(find_python_binary()));
        assert!(msg.contains("3.11+"));
    }

    // ─── F-6b-R6: find_validator_script tests ────────────────────────────

    #[test]
    fn find_validator_script_in_temp_dir() {
        let tmp = std::env::temp_dir().join("orch_test_validator");
        let agents_dir = tmp.join("agents");
        std::fs::create_dir_all(&agents_dir).unwrap();
        let script = agents_dir.join("validate.py");
        std::fs::write(&script, "# test").unwrap();

        // Change CWD to the temp dir so CWD-relative lookup finds it
        let original_dir = std::env::current_dir().unwrap();
        std::env::set_current_dir(&tmp).unwrap();
        let result = find_validator_script();
        std::env::set_current_dir(original_dir).unwrap();

        // Cleanup
        std::fs::remove_dir_all(&tmp).ok();

        assert!(result.is_ok(), "expected Ok, got: {result:?}");
        // F-6b-R5: result should be canonicalized (absolute path)
        let path = result.unwrap();
        assert!(path.is_absolute(), "expected absolute path, got: {path:?}");
    }
}
