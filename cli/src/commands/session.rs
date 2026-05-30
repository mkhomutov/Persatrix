//! `persatrix session` — operator session verbs (RFC 0031 §E).
//!
//! Thin-client pattern (per `.github/instructions/rust-cli.instructions.md`):
//! every subcommand marshals args into a REST call against the orchestrator
//! `/api/v1/sessions` surface (Phase 3 PR 1) and prints the response. Wire
//! shapes mirror `internal/server/types.go`.
//!
//! The registry verbs (`new` / `list` / `archive`) are pure REST. The pointer
//! verbs (`use` / `current`, and `new --activate`'s side effect) additionally
//! read/write the CLI-local `~/.persatrix/active-session` file via
//! [`crate::active_session`], resolving their id-or-label argument against the
//! registry over REST first.

use colored::Colorize;
use serde::{Deserialize, Serialize};
use tabled::{Table, Tabled};

use crate::active_session;
use crate::types::{api_error_message, validate_path_param, validate_session_label};

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
        label: String,
        /// Activate the new session by writing it to the active-session
        /// pointer file (equivalent to a follow-up `session use <new-id>`).
        #[arg(long)]
        activate: bool,
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
    /// Set the active session by id or label (writes the local pointer file)
    Use {
        /// Session id or label to activate
        id_or_label: String,
    },
    /// Show the currently active session
    Current,
}

/// Route a parsed [`SessionCommands`] to its handler.
pub(crate) async fn dispatch(
    client: &reqwest::Client,
    server: &str,
    cmd: SessionCommands,
) -> Result<(), String> {
    match cmd {
        SessionCommands::New {
            label,
            activate,
            json,
        } => cmd_session_new(client, server, label, activate, json).await,
        SessionCommands::List {
            include_archived,
            json,
        } => cmd_session_list(client, server, include_archived, json).await,
        SessionCommands::Archive { id_or_label, json } => {
            cmd_session_archive(client, server, &id_or_label, json).await
        }
        SessionCommands::Use { id_or_label } => cmd_session_use(client, server, &id_or_label).await,
        SessionCommands::Current => cmd_session_current(client, server).await,
    }
}

// ─── Pure helpers (testable without an HTTP server) ─────────────────────

/// Render the `session list` table, or a friendly line when empty.
///
/// Uses the `tabled` crate-default style (no `Style` override) to match the
/// sibling `agent list` table — the repo's only other `tabled`-based list.
pub(crate) fn render_session_table(sessions: &[SessionResponse]) -> String {
    if sessions.is_empty() {
        return "No sessions.".to_string();
    }
    Table::new(sessions).to_string()
}

// ─── Subcommand entry points ────────────────────────────────────────────

async fn cmd_session_new(
    client: &reqwest::Client,
    server: &str,
    label: String,
    activate: bool,
    json: bool,
) -> Result<(), String> {
    // Fail fast client-side: enforce the resource-id label shape (a CLI funnel
    // the server does not impose) and reject the reserved `legacy` sentinel
    // before the wire (OQ #2a). The server stays the guard of record — see
    // `validate_session_label`.
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
    // `--activate` is sugar for "create, then `session use <new-id>`". Write the
    // pointer only AFTER the created id has been reported above, so a
    // pointer-write failure surfaces as an error without hiding the freshly
    // minted id the operator needs to recover (a follow-up `session use <id>`).
    if activate {
        active_session::write(&stored.id)?;
        if !json {
            println!("Active session is now {}", stored.id.bold());
        }
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

/// Resolve an id-or-label against the registry via `GET /api/v1/sessions/{id}`.
///
/// Shared by `use` (validate before pointing) and `current` (enrich the stored
/// id with its human label). The path value is validated to keep traversal /
/// query-injection out of the URL before it is sent.
async fn resolve_session(
    client: &reqwest::Client,
    server: &str,
    id_or_label: &str,
) -> Result<SessionResponse, String> {
    validate_path_param(id_or_label, "session id")?;
    let resp = client
        .get(format!("{server}/api/v1/sessions/{id_or_label}"))
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }
    resp.json()
        .await
        .map_err(|e| format!("invalid response: {e}"))
}

/// Render the annotation that follows a session id in `use` / `current` output:
/// the label and/or an `archived` marker, parenthesised. Returns the empty
/// string (nothing to annotate) for a live, unlabeled session so callers can
/// append it unconditionally; the leading space is included only when there is
/// something to show.
///
/// Kept pure so the rendering — including the archived arm — is unit-testable
/// without a live registry. `current` relies on that arm because `GET
/// /api/v1/sessions/{id}` returns archived rows with 200 (the row is preserved;
/// RFC 0031 §B). Without the marker a pointer left on a since-archived session
/// would read as a normal active one, contradicting `use`, which refuses to
/// activate an archived session.
fn session_annotation(label: &str, archived: bool) -> String {
    match (label.is_empty(), archived) {
        (true, false) => String::new(),
        (true, true) => " (archived)".to_string(),
        (false, false) => format!(" ({label})"),
        (false, true) => format!(" ({label}, archived)"),
    }
}

async fn cmd_session_use(
    client: &reqwest::Client,
    server: &str,
    id_or_label: &str,
) -> Result<(), String> {
    // Resolve against the registry first so a typo or an archived target fails
    // *before* the pointer is written — never after, when it would silently
    // misroute the next channel (RFC §Security: misconfiguration risk).
    let sess = resolve_session(client, server, id_or_label).await?;
    if sess.archived {
        return Err(format!(
            "session {} ({}) is archived and cannot be activated (archive is one-way; RFC 0031 §B)",
            sess.id, sess.label
        ));
    }
    active_session::write(&sess.id)?;
    // Echo the active id at activation time — the documented mitigation for the
    // stale-pointer footgun (RFC §Security: misconfiguration risk). `archived`
    // is guaranteed false here by the guard above, so no marker is rendered.
    let annotation = session_annotation(&sess.label, sess.archived);
    println!(
        "Active session is now {}{}",
        sess.id.bold(),
        annotation.cyan()
    );
    Ok(())
}

async fn cmd_session_current(client: &reqwest::Client, server: &str) -> Result<(), String> {
    let Some(active_id) = active_session::read() else {
        // No pointer set: recall falls back to the built-in `legacy` namespace
        // (RFC §D carve-out), so say so rather than printing nothing.
        println!("No active session — using {}.", "legacy".bold());
        return Ok(());
    };
    // Enrich the stored id with its registry label. If the lookup fails (the
    // registry is down, or the session was archived/removed out from under the
    // pointer), still report the active id — a degraded answer beats none.
    match resolve_session(client, server, &active_id).await {
        Ok(sess) => {
            // Surface the archived marker: GET returns archived rows (200; the
            // row is preserved, RFC 0031 §B), so a pointer left on a session
            // archived after activation would otherwise read as a normal active
            // one — and `use` would refuse to re-activate it.
            let annotation = session_annotation(&sess.label, sess.archived);
            println!("Active session: {}{}", sess.id.bold(), annotation.cyan());
        }
        Err(e) => {
            println!("Active session: {}", active_id.bold());
            eprintln!("warning: could not resolve session label: {e}");
        }
    }
    Ok(())
}

#[cfg(test)]
#[path = "session_tests.rs"]
mod tests;
