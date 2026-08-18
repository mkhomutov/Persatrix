//! Tests for the chat REPL's bearer-token attach (RFC 0039 §J).
//!
//! Regression bar for the v0.3.14 F-1 finding: `cmd_chat` builds its own
//! long-timeout client and therefore does **not** inherit the default
//! headers `main.rs` installs on the shared client. When the re-attach was
//! missing, the chat POST was the only unauthenticated verb on the CLI
//! surface and `persatrix chat` answered `401 authentication required` for
//! every logged-in caller under `auth.mode: enabled` — found live at the
//! `MT-MEMORY-MULTIUSER-001` execution run, not by any test.

use super::*;
use reqwest::header::AUTHORIZATION;

#[test]
fn attaches_bearer_when_a_token_is_stored() {
    let headers = bearer_headers(Some("tok-abc123".to_string()))
        .expect("a stored token must produce default headers");
    let value = headers
        .get(AUTHORIZATION)
        .expect("the Authorization header must be present");
    assert_eq!(value.to_str().unwrap(), "Bearer tok-abc123");
}

#[test]
fn marks_the_credential_sensitive() {
    // Debug-logging middleware must never print the credential.
    let headers = bearer_headers(Some("tok-abc123".to_string())).unwrap();
    assert!(headers.get(AUTHORIZATION).unwrap().is_sensitive());
}

#[test]
fn attaches_nothing_when_logged_out() {
    // No stored token → no header → behaviour exactly as before RFC 0039,
    // and identical under `auth.mode: disabled`.
    assert!(bearer_headers(None).is_none());
}

#[test]
fn acts_logged_out_on_header_invalid_token_bytes() {
    // A token with header-invalid bytes can only be a corrupted credential
    // file. Act logged-out rather than fail the whole command.
    assert!(bearer_headers(Some("bad\nvalue".to_string())).is_none());
}
