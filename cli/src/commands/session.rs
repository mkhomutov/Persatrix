//! `persatrix session` — persona-memory session inspection (read verbs).

use crate::types::{api_error_message, SessionListResponse, SessionRow};
use tabled::{settings::Style, Table};

/// Renders a list of sessions as a table (or pretty JSON when `json`).
pub(crate) fn render_list(sessions: &[SessionRow], json: bool) -> String {
    if json {
        return serde_json::to_string_pretty(sessions).unwrap_or_default();
    }
    if sessions.is_empty() {
        return "No sessions.".to_string();
    }
    let mut table = Table::new(sessions);
    table.with(Style::rounded());
    table.to_string()
}

/// Renders a single session as a one-row table (or pretty JSON when `json`).
pub(crate) fn render_one(session: &SessionRow, json: bool) -> String {
    if json {
        return serde_json::to_string_pretty(session).unwrap_or_default();
    }
    let mut table = Table::new(std::slice::from_ref(session));
    table.with(Style::rounded());
    table.to_string()
}

pub(crate) async fn list(
    client: &reqwest::Client,
    server: &str,
    include_archived: bool,
    json: bool,
) -> Result<(), String> {
    let mut req = client.get(format!("{server}/api/v1/sessions"));
    if include_archived {
        req = req.query(&[("include_archived", "true")]);
    }

    let resp = req
        .send()
        .await
        .map_err(|e| format!("request failed: {e}"))?;

    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }

    let data: SessionListResponse = resp
        .json()
        .await
        .map_err(|e| format!("invalid response body: {e}"))?;

    println!("{}", render_list(&data.sessions, json));
    Ok(())
}

pub(crate) async fn show(
    client: &reqwest::Client,
    server: &str,
    id_or_label: &str,
    json: bool,
) -> Result<(), String> {
    let resp = client
        .get(format!("{server}/api/v1/sessions/{id_or_label}"))
        .send()
        .await
        .map_err(|e| format!("request failed: {e}"))?;

    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }

    let data: SessionRow = resp
        .json()
        .await
        .map_err(|e| format!("invalid response body: {e}"))?;

    println!("{}", render_one(&data, json));
    Ok(())
}

#[cfg(test)]
#[path = "session_tests.rs"]
mod session_tests;
