//! Unit tests for the pure halves of the auth commands: username
//! resolution and the wire DTO shapes. The interactive prompt and the
//! network paths follow the repo convention of not being unit-mocked.

use super::*;

#[test]
fn resolve_username_flag_wins_and_trims() {
    assert_eq!(resolve_username(Some("  alice ")).unwrap(), "alice");
}

#[test]
fn resolve_username_rejects_blank_flag() {
    assert!(resolve_username(Some("   ")).is_err());
}

#[test]
fn login_request_serializes_bearer_transport() {
    // The CLI is the canonical bearer caller (§A1): the transport is
    // explicit on the wire, never sniffed server-side.
    let body = serde_json::to_value(LoginRequest {
        username: "alice",
        password: "s3cret",
        session_transport: "bearer",
    })
    .unwrap();
    assert_eq!(body["username"], "alice");
    assert_eq!(body["session_transport"], "bearer");
}

#[test]
fn login_response_tolerates_missing_optionals() {
    // A cookie-transport response (or an older server) may omit fields;
    // only `token` is load-bearing for the CLI and its absence is a
    // handled error, not a deserialization failure.
    let resp: LoginResponse =
        serde_json::from_str(r#"{"expires_at":"2026-07-30T00:00:00Z"}"#).unwrap();
    assert!(resp.token.is_none());
    assert_eq!(resp.expires_at.as_deref(), Some("2026-07-30T00:00:00Z"));

    let resp: LoginResponse =
        serde_json::from_str(r#"{"token":"t","participant_id":"p","role":"operator"}"#).unwrap();
    assert_eq!(resp.token.as_deref(), Some("t"));
    assert_eq!(resp.role.as_deref(), Some("operator"));
}

#[test]
fn whoami_response_matches_both_identities() {
    // The §H anonymous `local` identity (whoami under disabled mode)…
    let anon: WhoamiResponse =
        serde_json::from_str(r#"{"authenticated":false,"participant_id":"local"}"#).unwrap();
    assert!(!anon.authenticated);
    assert_eq!(anon.participant_id, "local");
    assert!(anon.username.is_none());

    // …and a resolved account.
    let ident: WhoamiResponse = serde_json::from_str(
        r#"{"authenticated":true,"participant_id":"alice-h","role":"operator","username":"alice","account_id":"u-1"}"#,
    )
    .unwrap();
    assert!(ident.authenticated);
    assert_eq!(ident.role.as_deref(), Some("operator"));
}
