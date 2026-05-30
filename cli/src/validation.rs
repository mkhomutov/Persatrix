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

/// Session labels that collide with the RFC 0031 §D `legacy` carve-out and
/// must never be minted as an operator session. Mirrors the server-
/// authoritative guard (`channels.ErrReservedSessionID`); the CLI fails fast
/// with a friendlier message, but the server stays the guard of record.
pub(crate) const RESERVED_SESSION_LABELS: [&str; 1] = ["legacy"];

/// Validate an operator-supplied `session new --label`.
///
/// Enforces the cross-component resource-id shape (so labels are usable as
/// id-or-label path values downstream) and rejects the reserved `legacy`
/// sentinel before it can reach the wire (OQ #2a).
pub(crate) fn validate_session_label(value: &str) -> Result<(), String> {
    validate_resource_id(value, "session label")?;
    if RESERVED_SESSION_LABELS.contains(&value) {
        return Err(format!(
            "session label {value:?} is reserved (the always-visible `legacy` carve-out); choose another name"
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

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
        assert!(validate_resource_id("-agent", "id").to_owned().is_err());
        assert!(validate_resource_id("agent-", "id").is_err());
    }

    // ─── validate_session_label tests ────────────────────────────────────

    #[test]
    fn validate_session_label_accepts_valid_labels() {
        assert!(validate_session_label("arc").is_ok());
        assert!(validate_session_label("run-arc-3").is_ok());
        assert!(validate_session_label("dementia-test").is_ok());
    }

    #[test]
    fn validate_session_label_rejects_reserved_legacy() {
        // OQ #2a: `legacy` passes the resource-id shape but collides with the
        // §D carve-out, so it must be rejected with the reserved message.
        let err = validate_session_label("legacy").unwrap_err();
        assert!(
            err.contains("reserved"),
            "expected the reserved-label message, got: {err}"
        );
    }

    #[test]
    fn validate_session_label_rejects_malformed() {
        // Shape violations are caught before the reserved check.
        assert!(validate_session_label("My Arc").is_err());
        assert!(validate_session_label("Legacy").is_err());
        assert!(validate_session_label(" legacy ").is_err());
        assert!(validate_session_label("").is_err());
    }
}
