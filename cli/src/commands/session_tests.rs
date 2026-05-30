//! Unit tests for the `persatrix session` registry verbs.
//!
//! Pattern mirrors `types.rs` / `channel_tests.rs`: serde-contract round-trips
//! against the exact JSON shape `internal/server/types.go` produces, plus
//! `tabled` rendering smoke-tests — all without an HTTP server.

use super::*;

// ─── Serde contract tests ────────────────────────────────────────────────

#[test]
fn create_session_request_serializes_to_label() {
    let req = CreateSessionRequest {
        label: "run-arc-3".to_string(),
    };
    let json = serde_json::to_value(&req).unwrap();
    assert_eq!(json["label"], "run-arc-3");
}

#[test]
fn session_response_deserializes_full_shape() {
    // Matches sessionResponse JSON tags in internal/server/types.go.
    let json = serde_json::json!({
        "id": "0190abcd-0000-7000-8000-000000000001",
        "label": "arc",
        "created_at": "2026-05-30T12:00:00Z",
        "archived": false
    });
    let resp: SessionResponse = serde_json::from_value(json).unwrap();
    assert_eq!(resp.id, "0190abcd-0000-7000-8000-000000000001");
    assert_eq!(resp.label, "arc");
    assert_eq!(resp.created_at, "2026-05-30T12:00:00Z");
    assert!(!resp.archived);
}

#[test]
fn session_response_defaults_missing_label() {
    // Go `omitempty` drops the label for auto-minted rows; #[serde(default)]
    // must tolerate its absence rather than failing the parse.
    let json = serde_json::json!({
        "id": "0190abcd-0000-7000-8000-000000000002",
        "created_at": "2026-05-30T12:01:00Z",
        "archived": true
    });
    let resp: SessionResponse = serde_json::from_value(json).unwrap();
    assert!(
        resp.label.is_empty(),
        "missing label should default to empty"
    );
    assert!(resp.archived);
}

#[test]
fn list_sessions_response_deserializes_envelope() {
    let json = serde_json::json!({
        "sessions": [
            {"id": "0190a", "label": "arc", "created_at": "2026-05-30T12:00:00Z", "archived": false},
            {"id": "0190b", "created_at": "2026-05-30T12:01:00Z", "archived": true}
        ]
    });
    let body: ListSessionsResponse = serde_json::from_value(json).unwrap();
    assert_eq!(body.sessions.len(), 2);
    assert_eq!(body.sessions[0].label, "arc");
    assert!(body.sessions[1].label.is_empty());
}

#[test]
fn session_response_serializes_for_json_output() {
    // `session list --json` re-serializes the parsed rows; the round-trip must
    // preserve every field name the wire contract uses.
    let resp = SessionResponse {
        id: "0190a".to_string(),
        label: "arc".to_string(),
        created_at: "2026-05-30T12:00:00Z".to_string(),
        archived: true,
    };
    let json = serde_json::to_value(&resp).unwrap();
    assert_eq!(json["id"], "0190a");
    assert_eq!(json["label"], "arc");
    assert_eq!(json["created_at"], "2026-05-30T12:00:00Z");
    assert_eq!(json["archived"], true);
}

// ─── Rendering tests ─────────────────────────────────────────────────────

#[test]
fn render_session_table_empty_is_friendly() {
    assert_eq!(render_session_table(&[]), "No sessions.");
}

#[test]
fn render_session_table_carries_headers_and_rows() {
    let rows = vec![SessionResponse {
        id: "0190abc".to_string(),
        label: "arc".to_string(),
        created_at: "2026-05-30T12:00:00Z".to_string(),
        archived: false,
    }];
    let out = render_session_table(&rows);
    for header in ["ID", "LABEL", "CREATED", "ARCHIVED"] {
        assert!(out.contains(header), "table missing header {header}: {out}");
    }
    assert!(out.contains("0190abc"));
    assert!(out.contains("arc"));
}

// ─── clap parse tests ──────────────────────────────────────────────────────
//
// A local `Parser` wrapper exercises `SessionCommands` parsing without pulling
// the binary's private `Cli` into scope (and without adding test bulk to
// `main.rs`, which is at the file-size cap).

#[derive(clap::Parser)]
struct TestCli {
    #[command(subcommand)]
    cmd: SessionCommands,
}

#[test]
fn parses_new_with_label() {
    use clap::Parser;
    let cli = TestCli::try_parse_from(["x", "new", "--label", "arc"]).unwrap();
    match cli.cmd {
        SessionCommands::New { label, json } => {
            assert_eq!(label.as_deref(), Some("arc"));
            assert!(!json);
        }
        _ => panic!("expected New"),
    }
}

#[test]
fn parses_list_with_flags() {
    use clap::Parser;
    let cli = TestCli::try_parse_from(["x", "list", "--include-archived", "--json"]).unwrap();
    match cli.cmd {
        SessionCommands::List {
            include_archived,
            json,
        } => {
            assert!(include_archived);
            assert!(json);
        }
        _ => panic!("expected List"),
    }
}

#[test]
fn parses_archive() {
    use clap::Parser;
    let cli = TestCli::try_parse_from(["x", "archive", "arc"]).unwrap();
    match cli.cmd {
        SessionCommands::Archive { id_or_label, json } => {
            assert_eq!(id_or_label, "arc");
            assert!(!json);
        }
        _ => panic!("expected Archive"),
    }
}
