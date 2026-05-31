//! User-identity resolution for the CLI.
//!
//! `default_user_id` derives the implicit identity (from the OS environment)
//! that `Commands::Chat` and the `channel` subcommands fall back to when no
//! explicit `--user` / `--as <id>` is given; `normalize_user_id` is the shared
//! normalisation that keeps that identity within the resource-ID contract.
//!
//! Extracted from `main.rs` so the entrypoint stays under the file-size review
//! cap once the `--epoch` operator surface (ISSUE-0085 PR 5) tipped it over —
//! a mechanical move, no behaviour change.

/// Resolve the default user identity from the OS environment for
/// channel subcommands that did not pass `--as <id>`. Mirrors the
/// fallback logic used by `Commands::Chat` so the two REPLs and the
/// channel CLI agree on the implicit identity.
pub(crate) fn default_user_id() -> String {
    let raw = std::env::var("USERNAME")
        .or_else(|_| std::env::var("USER"))
        .unwrap_or_default();
    normalize_user_id(&raw)
}

/// Normalize a raw OS username into a resource-ID-safe string.
///
/// Converts to lowercase, replaces every non-alphanumeric character with '-',
/// strips leading/trailing hyphens, and falls back to "local" when the result
/// would otherwise be empty.
pub(crate) fn normalize_user_id(raw: &str) -> String {
    let normalized: String = raw
        .to_lowercase()
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
        .collect();
    let trimmed = normalized.trim_matches('-').to_string();
    if trimmed.is_empty() {
        "local".to_string()
    } else {
        trimmed
    }
}

#[cfg(test)]
mod tests {
    use super::normalize_user_id;

    #[test]
    fn normalize_simple_lowercase() {
        assert_eq!(normalize_user_id("alice"), "alice");
    }

    #[test]
    fn normalize_uppercase_converted() {
        assert_eq!(normalize_user_id("Alice"), "alice");
        assert_eq!(normalize_user_id("MKHOMUTOV"), "mkhomutov");
    }

    #[test]
    fn normalize_alphanumeric_preserved() {
        assert_eq!(normalize_user_id("user01"), "user01");
    }

    #[test]
    fn normalize_spaces_become_hyphens() {
        assert_eq!(normalize_user_id("John Doe"), "john-doe");
    }

    #[test]
    fn normalize_dots_become_hyphens() {
        // Windows UPN style: john.doe
        assert_eq!(normalize_user_id("john.doe"), "john-doe");
    }

    #[test]
    fn normalize_domain_prefix_stripped() {
        // Windows DOMAIN\user — backslash becomes hyphen, leading hyphen trimmed
        // after the domain part, but the whole thing is lowercased and hyphens
        // replace non-alphanumeric chars; leading/trailing hyphens are stripped.
        // "CORP\\jdoe" → "corp-jdoe"
        assert_eq!(normalize_user_id("CORP\\jdoe"), "corp-jdoe");
    }

    #[test]
    fn normalize_leading_trailing_hyphens_stripped() {
        // Underscore at start: "_build" → "-build" → "build"
        assert_eq!(normalize_user_id("_build"), "build");
    }

    #[test]
    fn normalize_empty_falls_back_to_local() {
        assert_eq!(normalize_user_id(""), "local");
    }

    #[test]
    fn normalize_only_special_chars_falls_back_to_local() {
        assert_eq!(normalize_user_id("___"), "local");
        assert_eq!(normalize_user_id("..."), "local");
    }

    #[test]
    fn normalize_unicode_becomes_hyphens() {
        // Non-ASCII chars are replaced with '-'; result trimmed if needed
        assert_eq!(normalize_user_id("björn"), "bj-rn");
    }

    #[test]
    fn normalize_result_passes_resource_id_validation() {
        // The output of normalize_user_id must always satisfy validate_resource_id.
        use crate::types::validate_resource_id;
        let inputs = [
            "Alice",
            "MKHOMUTOV",
            "john.doe",
            "John Doe",
            "CORP\\jdoe",
            "user01",
            "",
            "___",
        ];
        for input in inputs {
            let result = normalize_user_id(input);
            assert!(
                validate_resource_id(&result, "user_id").is_ok(),
                "normalize_user_id({input:?}) = {result:?} failed validate_resource_id",
            );
        }
    }
}
