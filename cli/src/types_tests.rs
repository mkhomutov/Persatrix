//! Serde-contract and table-rendering tests for [`super`] (the
//! [`crate::types`] module). Wired in via `#[path = "types_tests.rs"]
//! mod tests;` so `types.rs` stays under the 500-line review cap — the
//! same split convention as `channel_tests.rs` / `session_tests.rs`.

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
    // Matches Go agentResponse JSON tags in internal/server/types.go: the
    // agent kind rides on the JSON key `type` (agentResponse.Type), which the
    // CLI maps onto `agent_type` via `#[serde(rename = "type")]`. A regression
    // dropping that rename would make `agent_type` deserialize to None on every
    // server, breaking `test --persona` Check 3.
    let json = serde_json::json!({
        "id": "iron-fox",
        "address": "localhost:50051",
        "capabilities": ["review", "lint"],
        "status": "healthy",
        "type": "persona"
    });
    let resp: AgentResponse = serde_json::from_value(json).unwrap();
    assert_eq!(resp.id, "iron-fox");
    assert_eq!(resp.address, "localhost:50051");
    assert_eq!(resp.capabilities, vec!["review", "lint"]);
    assert_eq!(resp.status, "healthy");
    assert_eq!(
        resp.agent_type.as_deref(),
        Some("persona"),
        "the `type` JSON key must deserialize into agent_type (serde rename)"
    );
}

#[test]
fn agent_response_tolerates_absent_type_field() {
    // Forward-compat: a v0.1 server that omits the `type` key entirely must
    // still deserialize (the field is `#[serde(default)]`), leaving None.
    let json = serde_json::json!({
        "id": "legacy-agent",
        "address": "localhost:50051",
        "capabilities": [],
        "status": "healthy"
    });
    let resp: AgentResponse = serde_json::from_value(json).unwrap();
    assert!(
        resp.agent_type.is_none(),
        "an absent `type` key falls back to None via #[serde(default)]"
    );
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

// ─── Chat serde contract tests ──────────────────────────────────────

#[test]
fn chat_request_serializes_correctly() {
    let req = ChatRequest {
        message: "Hello".to_string(),
        user_id: "local".to_string(),
        chat_session_id: "".to_string(),
        participant_type: "user".to_string(),
        session_id: String::new(),
        epoch_id: String::new(),
    };
    let json = serde_json::to_value(&req).unwrap();
    assert_eq!(json["message"], "Hello");
    assert_eq!(json["user_id"], "local");
    assert_eq!(json["chat_session_id"], "");
    assert_eq!(json["participant_type"], "user");
    // An empty operator `session_id` is omitted (Go `omitempty` parity).
    // RFC 0016's chat token rides on `chat_session_id`, not this key
    // (RFC 0031 OQ #8) — so an absent override leaves the wire untouched.
    assert!(
        json.get("session_id").is_none(),
        "empty operator `session_id` must be omitted (Go omitempty parity)"
    );
    // ISSUE-0085 PR 5: an empty `--epoch` override is likewise omitted so
    // the orchestrator keeps its boot epoch ("live") — Go `epoch_id,omitempty`
    // parity, matching the session-id posture above.
    assert!(
        json.get("epoch_id").is_none(),
        "empty operator `epoch_id` must be omitted (Go omitempty parity)"
    );
}

#[test]
fn chat_request_includes_epoch_id_when_set() {
    // ISSUE-0085 PR 5: a resolved `--epoch` id rides on `epoch_id`,
    // orthogonal to the `--session` override on `session_id`. Mirrors the
    // publish-side `publish_message_request_includes_epoch_id_when_set`.
    let req = ChatRequest {
        message: "Hello".to_string(),
        user_id: "local".to_string(),
        chat_session_id: String::new(),
        participant_type: "user".to_string(),
        session_id: String::new(),
        epoch_id: "ci-run-5".to_string(),
    };
    let json = serde_json::to_value(&req).unwrap();
    assert_eq!(json["epoch_id"], "ci-run-5");
    assert!(
        json.get("session_id").is_none(),
        "empty operator `session_id` must stay omitted while epoch_id is set"
    );
}

#[test]
fn chat_response_deserializes_correctly() {
    let json = serde_json::json!({
        "reply": "I'm nexus-7.",
        "chat_session_id": "abc-123",
        "agent_id": "nexus-7",
        "timestamp": 1713600000,
        "agent_display_name": "Nexus Seven",
        "reply_status": "ok"
    });
    let resp: ChatResponse = serde_json::from_value(json).unwrap();
    assert_eq!(resp.reply, "I'm nexus-7.");
    assert_eq!(resp.chat_session_id, "abc-123");
    assert_eq!(resp.agent_display_name, "Nexus Seven");
    assert_eq!(resp.reply_status, "ok");
}

#[test]
fn chat_response_deserializes_empty_reply() {
    let json = serde_json::json!({
        "reply": "",
        "chat_session_id": "abc-123",
        "agent_id": "nexus-7",
        "timestamp": 1713600000,
        "agent_display_name": "Nexus Seven",
        "reply_status": "empty"
    });
    let resp: ChatResponse = serde_json::from_value(json).unwrap();
    assert_eq!(resp.reply_status, "empty");
    assert!(resp.reply.is_empty());
}

#[test]
fn chat_response_defaults_missing_optional_fields() {
    // Forward-compatibility: older server versions may omit
    // agent_display_name and reply_status. #[serde(default)] ensures
    // deserialization succeeds with empty-string defaults.
    let json = serde_json::json!({
        "reply": "Hello there",
        "chat_session_id": "abc-123",
        "agent_id": "nexus-7"
    });
    let resp: ChatResponse = serde_json::from_value(json).unwrap();
    assert_eq!(resp.reply, "Hello there");
    assert!(
        resp.agent_display_name.is_empty(),
        "should default to empty string"
    );
    assert!(
        resp.reply_status.is_empty(),
        "should default to empty string"
    );
}

#[test]
fn chat_response_rejects_legacy_session_id_key() {
    // Regression: a server still emitting the pre-v0.3.1 `session_id`
    // key (e.g. an out-of-tree fork) deserialises into a `serde_json`
    // Error rather than silently producing a `ChatResponse` with the
    // legacy value mapped onto `chat_session_id`. The REPL's
    // ``resp.json()`` call in `commands::chat::cmd_chat` surfaces
    // this as `eprintln!("error: invalid response: …")` and
    // continues the loop — the user sees a clear failure rather
    // than a silently-wrong session token. The break is deliberate
    // and documented in the v0.3.1 CHANGELOG.
    let json = serde_json::json!({
        "reply": "Hi",
        "session_id": "legacy-sess",
        "agent_id": "nexus-7",
    });
    let result: Result<ChatResponse, _> = serde_json::from_value(json);
    match result {
        Ok(_) => panic!(
            "expected the legacy `session_id` key to fail deserialisation against \
             the renamed `chat_session_id` field"
        ),
        Err(err) => {
            let msg = err.to_string();
            assert!(
                msg.contains("chat_session_id"),
                "expected the parse error to name the missing `chat_session_id` field, got: {msg}"
            );
        }
    }
}

// ─── PR 6 review follow-up: edge-case serde tests ──────────────────

#[test]
fn chat_request_serializes_long_message() {
    // Verify serde round-trip with very long messages.
    // (PR 6 review fix: PR 5 test gap #11.)
    let long_msg = "x".repeat(50_000);
    let req = ChatRequest {
        message: long_msg.clone(),
        user_id: "local".to_string(),
        chat_session_id: "sess-123".to_string(),
        participant_type: "user".to_string(),
        // RFC 0031 Phase 3: a populated operator session rides on the
        // `session_id` key, distinct from the RFC 0016 `chat_session_id`.
        session_id: "run-arc-3".to_string(),
        epoch_id: String::new(),
    };
    let json = serde_json::to_string(&req).unwrap();
    let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
    assert_eq!(parsed["message"].as_str().unwrap().len(), 50_000);
    assert_eq!(parsed["session_id"], "run-arc-3");
    assert_eq!(parsed["chat_session_id"], "sess-123");
}

#[test]
fn chat_request_serializes_unicode_content() {
    // Verify serde round-trip with multi-byte unicode content.
    // (PR 6 review fix: PR 5 test gap #11.)
    let unicode_msg = "こんにちは世界 🌍🚀 привет мир";
    let req = ChatRequest {
        message: unicode_msg.to_string(),
        user_id: "local".to_string(),
        chat_session_id: "".to_string(),
        participant_type: "user".to_string(),
        session_id: String::new(),
        epoch_id: String::new(),
    };
    let json = serde_json::to_value(&req).unwrap();
    assert_eq!(json["message"], unicode_msg);
}

#[test]
fn chat_response_deserializes_unicode_reply() {
    // Verify deserialization of unicode reply content.
    // (PR 6 review fix: PR 5 test gap #11.)
    let json = serde_json::json!({
        "reply": "你好！我是 nexus-7 🤖",
        "chat_session_id": "abc-123",
        "agent_id": "nexus-7",
        "agent_display_name": "Нексус Семь",
        "reply_status": "ok"
    });
    let resp: ChatResponse = serde_json::from_value(json).unwrap();
    assert_eq!(resp.reply, "你好！我是 nexus-7 🤖");
    assert_eq!(resp.agent_display_name, "Нексус Семь");
}

// ─── Tabled rendering tests (PR #162 review follow-up) ──────────────────
// Guard against future tabled attribute API renames. The `display_with = "fn"`
// syntax already became `display("fn")` in tabled 0.18 (this PR's migration).
// If tabled renames the convention again, the attribute silently becomes a
// no-op and the rendered output diverges — these tests catch that regression
// where the serde tests cannot.

#[test]
fn workflow_run_response_tabled_renders_none_as_dash() {
    // Smoke-test #[tabled(display("fmt_option"))] on all three Option<String>
    // fields (error, started_at, finished_at). All are None → each cell should
    // render as em-dash (\u2014) via fmt_option.
    use tabled::Table;
    let row = WorkflowRunResponse {
        run_id: "r1".into(),
        workflow_id: "wf".into(),
        status: "running".into(),
        error: None,
        started_at: None,
        finished_at: None,
    };
    let output = Table::new(vec![row]).to_string();
    assert!(
        output.contains('\u{2014}'),
        "expected em-dash (\\u2014) for None Option fields; got:\n{output}"
    );
}

#[test]
fn agent_response_tabled_renders_empty_vec_as_dash() {
    // Smoke-test #[tabled(display("fmt_vec"))] on capabilities. An empty Vec
    // should render as em-dash (\u2014) via fmt_vec. Mirrors the intent of the
    // workflow_run_response_tabled_renders_none_as_dash test above.
    use tabled::Table;
    let row = AgentResponse {
        id: "a1".into(),
        address: "localhost:50051".into(),
        capabilities: vec![],
        status: "healthy".into(),
        agent_type: None,
        name: None,
    };
    let output = Table::new(vec![row]).to_string();
    assert!(
        output.contains('\u{2014}'),
        "expected em-dash (\\u2014) for empty capabilities; got:\n{output}"
    );
}
