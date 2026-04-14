use colored::Colorize;

use crate::types::{api_error_message, validate_path_param};

pub(crate) async fn cmd_logs(
    client: &reqwest::Client,
    server: &str,
    execution_id: &str,
    follow: bool,
    agent: Option<&str>,
) -> Result<(), String> {
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
