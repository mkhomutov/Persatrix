use colored::Colorize;
use tabled::Table;

use crate::types::{api_error_message, colorize_status, validate_resource_id, AgentResponse};

pub(crate) async fn cmd_agent_list(client: &reqwest::Client, server: &str) -> Result<(), String> {
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

pub(crate) async fn cmd_agent_info(
    client: &reqwest::Client,
    server: &str,
    agent_id: &str,
) -> Result<(), String> {
    // Agent IDs follow the same cross-component contract as workflow IDs.
    validate_resource_id(agent_id, "agent ID")?;
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
            "\u{2014}".to_string()
        } else {
            agent.capabilities.join(", ")
        }
    );
    Ok(())
}

pub(crate) async fn cmd_agent_reload(agent_id: &str) -> Result<(), String> {
    // Validate early for consistency with Info and cmd_test_persona.
    // Defense-in-depth: when reload is implemented the validation
    // is already in place.
    validate_resource_id(agent_id, "agent ID")?;
    println!(
        "{}",
        format!("Agent reload for '{}' not yet implemented", agent_id).yellow()
    );
    Ok(())
}

pub(crate) async fn cmd_test(
    client: &reqwest::Client,
    server: &str,
    agent: Option<&str>,
    workflow: Option<&str>,
    persona: Option<&str>,
    record: bool,
) -> Result<(), String> {
    if let Some(id) = persona {
        if agent.is_some() || workflow.is_some() || record {
            eprintln!(
                "{}",
                "warning: --persona takes precedence; --agent/--workflow/--record ignored".yellow()
            );
        }
        cmd_test_persona(client, server, id).await
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

async fn cmd_test_persona(
    client: &reqwest::Client,
    server: &str,
    agent_id: &str,
) -> Result<(), String> {
    // Agent IDs follow the same cross-component contract as workflow IDs.
    validate_resource_id(agent_id, "agent ID")?;

    println!(
        "{} Testing persona agent: {}",
        "\u{2192}".cyan().bold(),
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
    // Dynamic check counter — adding/removing a check no longer requires
    // updating a separate hardcoded total.
    let mut total_checks: u32 = 0;

    // Check 1: Agent exists and is reachable
    total_checks += 1;
    println!(
        "  {} Agent '{}' found (status: {})",
        "\u{2713}".green(),
        agent.id,
        colorize_status(&agent.status)
    );
    checks_passed += 1;

    // Check 2: Agent status is healthy
    total_checks += 1;
    if agent.status == "healthy" {
        println!("  {} Agent is healthy", "\u{2713}".green());
        checks_passed += 1;
    } else {
        println!(
            "  {} Agent status is '{}', expected 'healthy'",
            "\u{2717}".red(),
            agent.status
        );
        warnings.push(format!("agent status is '{}', not 'healthy'", agent.status));
    }

    // Check 3: Agent type is persona
    // Handle missing agent_type (v0.1 servers don't return this field).
    total_checks += 1;
    match agent.agent_type.as_deref() {
        Some("persona") => {
            println!("  {} Agent type is 'persona'", "\u{2713}".green());
            checks_passed += 1;
        }
        Some(other) => {
            println!(
                "  {} Agent type is '{}', expected 'persona'",
                "\u{2717}".red(),
                other
            );
            warnings.push(format!("agent type is '{other}', not 'persona'"));
        }
        None => {
            println!(
                "  {} Agent type unknown (server may not support type field)",
                "?".yellow()
            );
            warnings.push(
                "agent type unknown \u{2014} server may not support the type field".to_string(),
            );
        }
    }

    // Check 4: Agent has capabilities
    total_checks += 1;
    if !agent.capabilities.is_empty() {
        println!(
            "  {} Agent has {} capability(ies): {}",
            "\u{2713}".green(),
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
            "\u{2713}".green().bold(),
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
