use std::collections::HashMap;

use colored::Colorize;
use tabled::Table;

use crate::types::{
    api_error_message, colorize_status, validate_path_param, validate_resource_id,
    SubmitWorkflowRequest, SubmitWorkflowResponse, WorkflowRunResponse,
};

pub(crate) async fn cmd_run(
    client: &reqwest::Client,
    server: &str,
    workflow: &str,
    input: Option<&str>,
    profile: &str,
) -> Result<(), String> {
    if profile != "default" {
        eprintln!(
            "{}",
            "warning: --profile is not yet supported by the server, ignored".yellow()
        );
    }
    validate_resource_id(workflow, "workflow ID")?;

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

pub(crate) async fn cmd_status(
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
            // Guard against empty error strings: Go's omitempty omits the field
            // when empty, but a non-Go server could send "error": "". Without
            // this filter the CLI would print a blank "Error:" line.
            if let Some(err) = run.error.as_deref().filter(|e| !e.is_empty()) {
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
