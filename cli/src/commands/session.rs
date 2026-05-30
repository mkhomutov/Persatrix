//! `persatrix session` — operator session registry verbs (RFC 0031 §E).
//!
//! Thin-client pattern (per `.github/instructions/rust-cli.instructions.md`):
//! every subcommand marshals args into a REST call against the orchestrator
//! `/api/v1/sessions` surface (Phase 3 PR 1) and prints the response. Wire
//! shapes mirror `internal/server/types.go`. These three verbs are pure REST
//! — nothing touches SQLite or `~/.persatrix/`; the active-session pointer
//! file and `use` / `current` land in a later PR.

use colored::Colorize;
use serde::{Deserialize, Serialize};
use tabled::{settings::Style, Table, Tabled};

use crate::types::{api_error_message, validate_path_param};
use crate::validation::validate_session_label;

// ─── Session registry DTOs (mirror `internal/server/types.go`) ──────────────

/// `POST /api/v1/sessions` request body. Matches `createSessionRequest` — a
/// required, human-readable `label`.
#[derive(Serialize)]
pub(crate) struct CreateSessionRequest {
    pub(crate) label: String,
}

/// A session registry row. Mirrors `sessionResponse`.
///
/// `label` is omitted from the wire for auto-minted, not-yet-named rows
/// (Go `omitempty`), so `#[serde(default)]` lets it default to the empty
/// string. `Serialize` is derived so `session list` / `session new` `--json`
/// can echo the parsed rows.
#[derive(Deserialize, Serialize, Tabled)]
pub(crate) struct SessionResponse {
    #[tabled(rename = "ID")]
    pub(crate) id: String,
    #[serde(default)]
    #[tabled(rename = "LABEL")]
    pub(crate) label: String,
    #[tabled(rename = "CREATED")]
    pub(crate) created_at: String,
    #[tabled(rename = "ARCHIVED")]
    pub(crate) archived: bool,
}

/// Envelope for `GET /api/v1/sessions`.
#[derive(Deserialize)]
pub(crate) struct ListSessionsResponse {
    pub(crate) sessions: Vec<SessionResponse>,
}

/// `persatrix session …` subcommands (RFC 0031 §E registry verbs).
#[derive(clap::Subcommand)]
pub(crate) enum SessionCommands {
    /// Create and register a new named session
    New {
        /// Human-readable label for the session (required)
        #[arg(long)]
        label: Option<String>,
        /// Emit raw JSON (single line) instead of the human confirmation.
        #[arg(long)]
        json: bool,
    },
    /// List sessions
    List {
        /// Include archived sessions
        #[arg(long)]
        include_archived: bool,
        /// Emit raw JSON (single line) instead of the human table.
        #[arg(long)]
        json: bool,
    },
    /// Archive a session by id or label (one-way; RFC 0031 §B)
    Archive {
        /// Session id or label
        id_or_label: String,
        /// Emit raw JSON (single line) instead of the human confirmation.
        #[arg(long)]
        json: bool,
    },
}

/// Route a parsed [`SessionCommands`] to its handler.
pub(crate) async fn dispatch(
    client: &reqwest::Client,
    server: &str,
    cmd: SessionCommands,
) -> Result<(), String> {
    match cmd {
        SessionCommands::New { label, json } => cmd_session_new(client, server, label, json).await,
        SessionCommands::List {
            include_archived,
            json,
        } => cmd_session_list(client, server, include_archived, json).await,
        SessionCommands::Archive { id_or_label, json } => {
            cmd_session_archive(client, server, &id_or_label, json).await
        }
    }
}

// ─── Pure helpers (testable without an HTTP server) ─────────────────────

/// Render the `session list` table, or a friendly line when empty.
pub(crate) fn render_session_table(sessions: &[SessionResponse]) -> String {
    if sessions.is_empty() {
        return "No sessions.".to_string();
    }
    let mut table = Table::new(sessions);
    table.with(Style::rounded());
    table.to_string()
}

// ─── Subcommand entry points ────────────────────────────────────────────

async fn cmd_session_new(
    client: &reqwest::Client,
    server: &str,
    label: Option<String>,
    json: bool,
) -> Result<(), String> {
    let label = label.ok_or("session new requires --label <name>")?;
    // Client-side fail-fast mirror of the server's reserved-id guard (OQ #2a);
    // the server stays authoritative.
    validate_session_label(&label)?;
    let req = CreateSessionRequest { label };
    let resp = client
        .post(format!("{server}/api/v1/sessions"))
        .json(&req)
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }
    let stored: SessionResponse = resp
        .json()
        .await
        .map_err(|e| format!("invalid response: {e}"))?;
    if json {
        println!("{}", serde_json::to_string(&stored).unwrap());
    } else {
        println!(
            "Created session {} {}",
            stored.id.bold(),
            format!("({})", stored.label).cyan()
        );
    }
    Ok(())
}

async fn cmd_session_list(
    client: &reqwest::Client,
    server: &str,
    include_archived: bool,
    json: bool,
) -> Result<(), String> {
    let mut url = format!("{server}/api/v1/sessions");
    if include_archived {
        url.push_str("?include_archived=true");
    }
    let resp = client
        .get(&url)
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }
    let body: ListSessionsResponse = resp
        .json()
        .await
        .map_err(|e| format!("invalid response: {e}"))?;
    if json {
        // Single-line output, matching the `channel list --json` convention so
        // downstream `jq`/line-counting tools see a consistent shape.
        println!("{}", serde_json::to_string(&body.sessions).unwrap());
        return Ok(());
    }
    println!("{}", render_session_table(&body.sessions));
    Ok(())
}

async fn cmd_session_archive(
    client: &reqwest::Client,
    server: &str,
    id_or_label: &str,
    json: bool,
) -> Result<(), String> {
    validate_path_param(id_or_label, "session id")?;
    let resp = client
        .post(format!("{server}/api/v1/sessions/{id_or_label}/archive"))
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }
    if json {
        let payload = serde_json::json!({ "id_or_label": id_or_label, "archived": true });
        println!("{}", serde_json::to_string(&payload).unwrap());
    } else {
        println!("Archived session {}", id_or_label.bold());
    }
    Ok(())
}

#[cfg(test)]
#[path = "session_tests.rs"]
mod tests;
