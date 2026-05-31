//! Per-invocation epoch resolution — ISSUE-0085 PR 5 (operator surface).
//!
//! The orchestrator resolves the per-process epoch once at boot from
//! `PERSATRIX_EPOCH` (default `live`) and emits it on every dispatch. This
//! module decides, CLI-side, whether to send a per-request `--epoch` override
//! that takes precedence *above* that boot env for the one invocation:
//!
//! ```text
//! --epoch flag  >  PERSATRIX_EPOCH env  >  none (orchestrator boot default)
//! ```
//!
//! A `none` result means "send no override" — the orchestrator then keeps its
//! boot epoch (`live` in production, a per-job id in CI).
//!
//! Unlike the session axis ([`crate::session_resolve`]) there is no registry
//! lookup (epoch has no `new`/`use` lifecycle) and no active-session pointer
//! file: epoch is a bare flag-or-env knob, so resolution is a pure two-layer
//! precedence with no I/O beyond reading the env var. Strict-equality isolation
//! (no `legacy` carve-out, no `*` wildcard) is enforced orchestrator-/persona-
//! side; the CLI only forwards the chosen id.

/// The env var carrying the per-process epoch, read in one place.
const EPOCH_ENV_VAR: &str = "PERSATRIX_EPOCH";

/// Two-layer precedence, kept pure (no env read) so the ordering is
/// unit-testable without touching the real environment. Both arms are
/// `nonblank`-filtered so a blank / whitespace-only value at either layer falls
/// through rather than forwarding an empty id the orchestrator would read as
/// "no epoch".
fn resolve_precedence(flag: Option<&str>, env: Option<&str>) -> Option<String> {
    nonblank(flag).or_else(|| nonblank(env))
}

/// Trim and drop a blank value, so `PERSATRIX_EPOCH=` (or a whitespace-only
/// flag) falls through to the next precedence layer rather than forwarding an
/// empty id.
fn nonblank(value: Option<&str>) -> Option<String> {
    value
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
}

/// Resolve the effective epoch for one CLI invocation: the `--epoch` flag, else
/// `PERSATRIX_EPOCH`, else `None` (let the orchestrator keep its boot default).
pub(crate) fn resolve_epoch(flag: Option<&str>) -> Option<String> {
    let env = std::env::var(EPOCH_ENV_VAR).ok();
    resolve_precedence(flag, env.as_deref())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flag_wins_over_env() {
        let got = resolve_precedence(Some("ci-run-5"), Some("from-env"));
        assert_eq!(got.as_deref(), Some("ci-run-5"));
    }

    #[test]
    fn env_used_when_no_flag() {
        let got = resolve_precedence(None, Some("from-env"));
        assert_eq!(got.as_deref(), Some("from-env"));
    }

    #[test]
    fn none_when_nothing_set() {
        assert!(resolve_precedence(None, None).is_none());
    }

    #[test]
    fn blank_flag_falls_through_to_env() {
        // A whitespace-only flag must not shadow a real env value (it would
        // otherwise forward an empty id the orchestrator reads as "no epoch").
        let got = resolve_precedence(Some("   "), Some("from-env"));
        assert_eq!(got.as_deref(), Some("from-env"));
    }

    #[test]
    fn blank_env_yields_none() {
        // `PERSATRIX_EPOCH=` (exported empty) must fall through to none, not
        // forward an empty id.
        assert!(resolve_precedence(None, Some("")).is_none());
    }

    #[test]
    fn flag_is_trimmed() {
        let got = resolve_precedence(Some("  run-7  "), None);
        assert_eq!(got.as_deref(), Some("run-7"));
    }
}
