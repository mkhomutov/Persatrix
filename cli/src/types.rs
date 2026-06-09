use std::collections::HashMap;

use colored::Colorize;
use serde::{Deserialize, Serialize};
use tabled::Tabled;

// ─── API request/response types ──────────────────────────────────────────

#[derive(Serialize)]
pub(crate) struct SubmitWorkflowRequest {
    pub(crate) workflow_id: String,
    /// Matches Go server's `map[string]string` — typed precisely to catch
    /// non-string values on the client side instead of round-tripping a 400.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) inputs: Option<HashMap<String, String>>,
}

#[derive(Deserialize)]
pub(crate) struct SubmitWorkflowResponse {
    pub(crate) run_id: String,
    pub(crate) workflow_id: String,
    pub(crate) status: String,
}

/// Response fields intentionally use `Option` and no `#[serde(deny_unknown_fields)]`
/// so the CLI stays forward-compatible when the server adds new fields (e.g. `steps`).
#[derive(Deserialize, Tabled)]
pub(crate) struct WorkflowRunResponse {
    pub(crate) run_id: String,
    pub(crate) workflow_id: String,
    pub(crate) status: String,
    #[tabled(display("fmt_option"))]
    pub(crate) error: Option<String>,
    #[tabled(display("fmt_option"))]
    pub(crate) started_at: Option<String>,
    #[tabled(display("fmt_option"))]
    pub(crate) finished_at: Option<String>,
}

#[derive(Deserialize, Tabled)]
pub(crate) struct AgentResponse {
    pub(crate) id: String,
    pub(crate) address: String,
    #[tabled(display("fmt_vec"))]
    pub(crate) capabilities: Vec<String>,
    pub(crate) status: String,
    /// Agent type (e.g. "task", "persona"). The Go server serializes this
    /// under the JSON key `type` (`agentResponse.Type`, internal/server/types.go),
    /// so the field MUST carry `#[serde(rename = "type")]` — without it serde
    /// looks for a non-existent `agent_type` key and silently yields `None` on
    /// every server. `#[serde(default)]` keeps deserialization tolerant of v0.1
    /// servers that omit the key entirely.
    #[serde(default, rename = "type")]
    #[tabled(skip)]
    pub(crate) agent_type: Option<String>,
    /// Human-readable display name from `agents.yaml` (e.g. "Iron Fox").
    /// `#[serde(default)]` keeps deserialization tolerant of older servers
    /// that omit the field; `#[tabled(skip)]` keeps the `agent list` table
    /// layout unchanged. Consumers fall back to `id` when missing/empty.
    #[serde(default)]
    #[tabled(skip)]
    pub(crate) name: Option<String>,
}

#[derive(Deserialize)]
pub(crate) struct ApiError {
    pub(crate) error: String,
    #[allow(dead_code)]
    pub(crate) code: Option<String>,
}

// ─── Chat types ──────────────────────────────────────────────────────────

/// `chat_session_id` (RFC 0016 chat-conversation token) was renamed from
/// `session_id` in v0.3.1 to disambiguate from RFC 0031's operator-
/// namespace `session_id`. Binary-proto consumers are unaffected (field
/// numbers preserved); JSON consumers — including this REST client —
/// must use the new key. See CHANGELOG `[0.3.1]` Upgrade Notes.
#[derive(Serialize)]
pub(crate) struct ChatRequest {
    pub(crate) message: String,
    pub(crate) user_id: String,
    pub(crate) chat_session_id: String,
    pub(crate) participant_type: String,
    /// RFC 0031 Phase 3 operator-namespace session override (the `--session`
    /// flag), distinct from the RFC 0016 `chat_session_id` above. Omitted when
    /// empty (Go `session_id,omitempty` parity).
    #[serde(skip_serializing_if = "String::is_empty")]
    pub(crate) session_id: String,
    /// ISSUE-0085 PR 5 run/test-isolation epoch override (the `--epoch` flag),
    /// orthogonal to `session_id` (room-continuity vs. run-isolation). Omitted
    /// when empty (Go `epoch_id,omitempty` parity) so the orchestrator keeps
    /// its boot default ("live").
    #[serde(skip_serializing_if = "String::is_empty")]
    pub(crate) epoch_id: String,
}

#[derive(Deserialize)]
pub(crate) struct ChatResponse {
    pub(crate) reply: String,
    pub(crate) chat_session_id: String,
    #[allow(dead_code)]
    pub(crate) agent_id: String,
    /// Forward-compatible: defaults to "" if server omits the field (e.g. older
    /// server version). The REPL falls back to agent_id when empty.
    #[serde(default)]
    pub(crate) agent_display_name: String,
    /// Forward-compatible: defaults to "" if server omits the field.
    /// The REPL treats empty the same as a non-"empty" status (shows reply).
    #[serde(default)]
    pub(crate) reply_status: String,
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

pub(crate) fn colorize_status(status: &str) -> colored::ColoredString {
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

// ─── Shared helpers ─────────────────────────────────────────────────────

pub(crate) async fn api_error_message(resp: reqwest::Response) -> String {
    let status = resp.status();
    match resp.json::<ApiError>().await {
        Ok(e) => format!("{}: {}", status, e.error),
        Err(_) => format!("HTTP {status}"),
    }
}

pub(crate) use crate::validation::{
    validate_path_param, validate_resource_id, validate_session_label,
};
#[cfg(test)]
#[path = "types_tests.rs"]
mod tests;
