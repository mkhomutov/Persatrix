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
    #[tabled(display_with = "fmt_option")]
    pub(crate) error: Option<String>,
    #[tabled(display_with = "fmt_option")]
    pub(crate) started_at: Option<String>,
    #[tabled(display_with = "fmt_option")]
    pub(crate) finished_at: Option<String>,
}

#[derive(Deserialize, Tabled)]
pub(crate) struct AgentResponse {
    pub(crate) id: String,
    pub(crate) address: String,
    #[tabled(display_with = "fmt_vec")]
    pub(crate) capabilities: Vec<String>,
    pub(crate) status: String,
    /// Agent type (e.g. "task", "persona"). Optional for forward-compatibility
    /// with v0.1 servers that don't return this field.
    #[serde(default)]
    #[tabled(skip)]
    pub(crate) agent_type: Option<String>,
}

#[derive(Deserialize)]
pub(crate) struct ApiError {
    pub(crate) error: String,
    #[allow(dead_code)]
    pub(crate) code: Option<String>,
}

// ─── Chat types ──────────────────────────────────────────────────────────

#[derive(Serialize)]
pub(crate) struct ChatRequest {
    pub(crate) message: String,
    pub(crate) user_id: String,
    pub(crate) session_id: String,
    pub(crate) participant_type: String,
}

#[derive(Deserialize)]
pub(crate) struct ChatResponse {
    pub(crate) reply: String,
    pub(crate) session_id: String,
    #[allow(dead_code)]
    pub(crate) agent_id: String,
    pub(crate) agent_display_name: String,
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

/// Reject path parameters that could cause path-traversal or query-injection.
pub(crate) fn validate_path_param(value: &str, label: &str) -> Result<(), String> {
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

/// Validate that a resource ID matches `^[a-z0-9][a-z0-9-]*[a-z0-9]$`.
pub(crate) fn validate_resource_id(value: &str, label: &str) -> Result<(), String> {
    if value.is_empty() {
        return Err(format!("{label} cannot be empty"));
    }
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

#[cfg(test)]
mod tests {
    use super::*;

    // ─── Serde contract tests ────────────────────────────────────────────

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

    // ─── Response deserialization contract tests ─────────────────────────
    // Verify the CLI can parse the exact JSON shape the Go server produces
    // (see internal/server/types.go). A server-side field rename would
    // silently produce None/default values due to serde's lenient defaults;
    // these tests catch that.

    #[test]
    fn workflow_run_response_deserializes_correctly() {
        // Matches Go workflowRunResponse JSON tags in internal/server/types.go.
        // Go's omitempty omits the error field when empty, so the canonical
        // success shape has no "error" key at all.
        let json = serde_json::json!({
            "run_id": "run-001",
            "workflow_id": "feature-builder",
            "status": "completed",
            "started_at": "2026-04-14T10:00:00Z",
            "finished_at": "2026-04-14T10:05:00Z",
            "steps": {}
        });
        let resp: WorkflowRunResponse = serde_json::from_value(json).unwrap();
        assert_eq!(resp.run_id, "run-001");
        assert_eq!(resp.workflow_id, "feature-builder");
        assert_eq!(resp.status, "completed");
        assert!(resp.error.is_none());
        assert_eq!(resp.started_at.as_deref(), Some("2026-04-14T10:00:00Z"));
        assert_eq!(resp.finished_at.as_deref(), Some("2026-04-14T10:05:00Z"));
    }

    #[test]
    fn workflow_run_response_deserializes_error_field() {
        // Go's omitempty sends a non-empty error string on failure.
        let json = serde_json::json!({
            "run_id": "run-003",
            "workflow_id": "feature-builder",
            "status": "failed",
            "error": "task timed out",
            "started_at": "2026-04-14T10:00:00Z",
            "finished_at": null,
            "steps": {}
        });
        let resp: WorkflowRunResponse = serde_json::from_value(json).unwrap();
        assert_eq!(resp.status, "failed");
        assert_eq!(resp.error.as_deref(), Some("task timed out"));
    }

    #[test]
    fn workflow_run_response_handles_null_timestamps() {
        // Go server sends null for zero-valued *time.Time pointers
        let json = serde_json::json!({
            "run_id": "run-002",
            "workflow_id": "test-wf",
            "status": "pending",
            "started_at": null,
            "finished_at": null,
            "steps": {}
        });
        let resp: WorkflowRunResponse = serde_json::from_value(json).unwrap();
        assert_eq!(resp.status, "pending");
        assert!(resp.started_at.is_none());
        assert!(resp.finished_at.is_none());
        assert!(resp.error.is_none());
    }

    #[test]
    fn agent_response_deserializes_correctly() {
        // Matches Go agentResponse JSON tags in internal/server/types.go.
        // Note: Go server does NOT include agent_type — CLI's #[serde(default)]
        // correctly handles its absence.
        let json = serde_json::json!({
            "id": "code-reviewer",
            "address": "localhost:50051",
            "capabilities": ["review", "lint"],
            "status": "healthy"
        });
        let resp: AgentResponse = serde_json::from_value(json).unwrap();
        assert_eq!(resp.id, "code-reviewer");
        assert_eq!(resp.address, "localhost:50051");
        assert_eq!(resp.capabilities, vec!["review", "lint"]);
        assert_eq!(resp.status, "healthy");
        assert!(resp.agent_type.is_none(), "Go server omits agent_type");
    }

    #[test]
    fn agent_response_with_empty_capabilities() {
        // Go server normalizes capabilities to empty slice, not null
        let json = serde_json::json!({
            "id": "new-agent",
            "address": "localhost:50052",
            "capabilities": [],
            "status": "offline"
        });
        let resp: AgentResponse = serde_json::from_value(json).unwrap();
        assert!(resp.capabilities.is_empty());
        assert_eq!(resp.status, "offline");
    }

    // ─── validate_path_param tests ──────────────────────────────────────

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

    // ─── validate_resource_id tests ──────────────────────────────────────

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

    // ─── Chat serde contract tests ──────────────────────────────────────

    #[test]
    fn chat_request_serializes_correctly() {
        let req = ChatRequest {
            message: "Hello".to_string(),
            user_id: "local".to_string(),
            session_id: "".to_string(),
            participant_type: "user".to_string(),
        };
        let json = serde_json::to_value(&req).unwrap();
        assert_eq!(json["message"], "Hello");
        assert_eq!(json["user_id"], "local");
        assert_eq!(json["session_id"], "");
        assert_eq!(json["participant_type"], "user");
    }

    #[test]
    fn chat_response_deserializes_correctly() {
        let json = serde_json::json!({
            "reply": "I'm nexus-7.",
            "session_id": "abc-123",
            "agent_id": "nexus-7",
            "timestamp": 1713600000,
            "agent_display_name": "Nexus Seven",
            "reply_status": "ok"
        });
        let resp: ChatResponse = serde_json::from_value(json).unwrap();
        assert_eq!(resp.reply, "I'm nexus-7.");
        assert_eq!(resp.session_id, "abc-123");
        assert_eq!(resp.agent_display_name, "Nexus Seven");
        assert_eq!(resp.reply_status, "ok");
    }

    #[test]
    fn chat_response_deserializes_empty_reply() {
        let json = serde_json::json!({
            "reply": "",
            "session_id": "abc-123",
            "agent_id": "nexus-7",
            "timestamp": 1713600000,
            "agent_display_name": "Nexus Seven",
            "reply_status": "empty"
        });
        let resp: ChatResponse = serde_json::from_value(json).unwrap();
        assert_eq!(resp.reply_status, "empty");
        assert!(resp.reply.is_empty());
    }
}
