//! Per-invocation session resolution — RFC 0031 Phase 3 PR 4 (OQ #6).
//!
//! The orchestrator honours an explicit `session_id` on the publish / chat
//! body as the highest-precedence session signal, overriding the ISSUE-0082
//! auto-binding for that one request. This module decides, CLI-side, which id
//! (if any) to send, per the RFC 0031 OQ #6 precedence chain:
//!
//! ```text
//! --session flag  >  PERSATRIX_SESSION_ID env  >  ~/.persatrix/active-session  >  none
//! ```
//!
//! A `none` result means "send no override" — the orchestrator then applies
//! its own `legacy` default (or, on the dispatch path, the per-room
//! auto-binding). The chain governs the process-lifetime / single-conversation
//! session; the per-conversation auto-binding stands underneath it unless an
//! override is present (the [`super`] dispatcher reconciliation).
//!
//! Only the explicit `--session` *flag* is resolved against the registry
//! (id-or-label → canonical id), matching `session use`. The env var and the
//! pointer file already hold resolved ids (the file is written by `session use`
//! /`new --activate`, which validate against the registry first), so they pass
//! through untouched — forcing them through the registry would regress an
//! operator who exports `PERSATRIX_SESSION_ID` to an ad-hoc string.

use crate::active_session;
use crate::commands::session::lookup_session;

/// OQ #6 precedence, kept pure (no env / filesystem / network) so the ordering
/// is unit-testable without touching the real environment. `flag` is the
/// already-registry-resolved `--session` value (label → canonical id happens
/// in [`resolve_for_invocation`] before this), so the precedence layer never
/// forwards a bare label.
///
/// Every arm — flag, env, and file — is `nonblank`-filtered so a blank /
/// whitespace-only value at any layer falls through to the next rather than
/// forwarding an empty id the orchestrator would read as "no session". The file
/// arm is filtered here too even though [`active_session::read`] already
/// trims+drops blanks: it keeps this pure function self-contained rather than
/// relying on a caller-upheld invariant, and uniform across the three sources.
fn resolve_precedence(
    flag: Option<&str>,
    env: Option<&str>,
    file: Option<String>,
) -> Option<String> {
    nonblank(flag)
        .or_else(|| nonblank(env))
        .or_else(|| nonblank(file.as_deref()))
}

/// Trim and drop a blank value, so `PERSATRIX_SESSION_ID=` (or a whitespace-only
/// flag) falls through to the next precedence layer rather than forwarding an
/// empty id the orchestrator would treat as "no session".
fn nonblank(value: Option<&str>) -> Option<String> {
    value
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
}

/// Apply the OQ #6 precedence, reading the env var and the active-session
/// pointer file behind the (already-resolved) flag. Returns the id to forward,
/// or `None` to let the orchestrator apply its default.
pub(crate) fn resolve_session(flag: Option<&str>) -> Option<String> {
    let env = std::env::var("PERSATRIX_SESSION_ID").ok();
    resolve_precedence(flag, env.as_deref(), active_session::read())
}

/// Resolve the effective session for one CLI invocation.
///
/// When `--session` is given, its id-or-label argument is resolved against the
/// registry to a canonical id (`GET /api/v1/sessions/{id}`); an archived target
/// **warns but proceeds** — the operator named it explicitly, distinct from
/// `session use`, which refuses to activate an archived session. The resolved
/// id then feeds the OQ #6 precedence ([`resolve_session`]). A blank /
/// whitespace-only flag is treated as absent — it falls through the precedence
/// chain (matching [`nonblank`]) rather than being sent to the registry, so a
/// stray `--session ""` does not fail the command on a value that was never a
/// real session. Returns the id to forward on the request body, or `None` to
/// omit the field.
pub(crate) async fn resolve_for_invocation(
    client: &reqwest::Client,
    server: &str,
    flag: Option<&str>,
) -> Result<Option<String>, String> {
    let resolved_flag = match nonblank(flag) {
        Some(raw) => {
            let sess = lookup_session(client, server, &raw).await?;
            if sess.archived {
                let label = if sess.label.is_empty() {
                    String::new()
                } else {
                    format!(" ({})", sess.label)
                };
                eprintln!(
                    "warning: session {}{label} is archived; using it anyway (named explicitly via --session)",
                    sess.id
                );
            }
            Some(sess.id)
        }
        None => None,
    };
    Ok(resolve_session(resolved_flag.as_deref()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flag_wins_over_env_and_file() {
        let got = resolve_precedence(
            Some("from-flag"),
            Some("from-env"),
            Some("from-file".to_string()),
        );
        assert_eq!(got.as_deref(), Some("from-flag"));
    }

    #[test]
    fn env_wins_over_file_when_no_flag() {
        let got = resolve_precedence(None, Some("from-env"), Some("from-file".to_string()));
        assert_eq!(got.as_deref(), Some("from-env"));
    }

    #[test]
    fn file_used_when_no_flag_or_env() {
        let got = resolve_precedence(None, None, Some("from-file".to_string()));
        assert_eq!(got.as_deref(), Some("from-file"));
    }

    #[test]
    fn none_when_nothing_set() {
        assert!(resolve_precedence(None, None, None).is_none());
    }

    #[test]
    fn blank_flag_falls_through_to_env() {
        // A whitespace-only flag must not shadow a real env value (it would
        // otherwise forward an empty id the orchestrator reads as "no session").
        let got = resolve_precedence(Some("   "), Some("from-env"), None);
        assert_eq!(got.as_deref(), Some("from-env"));
    }

    #[test]
    fn blank_env_falls_through_to_file() {
        // `PERSATRIX_SESSION_ID=` (exported empty) must not shadow the pointer.
        let got = resolve_precedence(None, Some(""), Some("from-file".to_string()));
        assert_eq!(got.as_deref(), Some("from-file"));
    }

    #[test]
    fn blank_file_yields_none() {
        // The file arm is `nonblank`-filtered like the flag/env arms, so a
        // blank / whitespace-only pointer value yields "no session" rather than
        // forwarding an empty id. `active_session::read` already trims+filters,
        // so this is defensive — it keeps `resolve_precedence` self-contained
        // (no reliance on a caller-upheld invariant) and uniform across arms.
        assert!(resolve_precedence(None, None, Some("   ".to_string())).is_none());
    }

    #[test]
    fn flag_is_trimmed() {
        let got = resolve_precedence(Some("  padded  "), None, None);
        assert_eq!(got.as_deref(), Some("padded"));
    }

    #[tokio::test]
    async fn blank_flag_does_not_trigger_a_registry_lookup() {
        // A whitespace-only `--session` must fall through the OQ #6 precedence
        // (treated as "no flag"), exactly as `resolve_precedence` already does
        // for the pure layer — it must NOT be forwarded to the registry, which
        // would fail the whole command on a value the operator never meant as a
        // session. The client points at a closed port: were the blank flag
        // erroneously sent to `lookup_session`, the GET would error and this
        // would be `Err`. After the `nonblank` gate it short-circuits before any
        // network call, so the result is `Ok` regardless of env / pointer state.
        let client = reqwest::Client::new();
        let got = resolve_for_invocation(&client, "http://127.0.0.1:1", Some("   ")).await;
        assert!(
            got.is_ok(),
            "a blank --session must fall through, not hit the registry: {got:?}"
        );
    }
}
