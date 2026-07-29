//! Session-credential file — RFC 0039 §J.
//!
//! `persatrix login` writes the bearer token the orchestrator issued
//! here; `logout` clears it; PR 5 will have every command read it and
//! attach `Authorization: Bearer`. The file is a JSON object **keyed by
//! the orchestrator URL** so multiple orchestrators do not collide:
//!
//! ```json
//! { "http://localhost:8080": { "token": "…", "participant_id": "…" } }
//! ```
//!
//! Location resolution mirrors the active-session pointer file: the
//! `PERSATRIX_CREDENTIALS_FILE` environment variable (tests, unusual
//! deployments) wins; otherwise `~/.persatrix/credentials`. Written at
//! mode `0600` — the token is a live credential, unlike the session
//! pointer beside it.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

/// Environment override for the credential-file location.
const CREDENTIALS_FILE_ENV: &str = "PERSATRIX_CREDENTIALS_FILE";

/// One orchestrator's stored session. Only `token` is load-bearing; the
/// rest is operator-facing context echoed by `login` for `whoami`-less
/// inspection of the file.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct CredentialEntry {
    pub(crate) token: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) participant_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) role: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) expires_at: Option<String>,
}

/// Resolve the credential-file path from an explicit override and a home
/// directory. Pure (no environment or filesystem access) so precedence
/// is unit-testable; the override wins when non-blank.
fn resolve_path(override_var: Option<&str>, home: Option<&Path>) -> Option<PathBuf> {
    if let Some(raw) = override_var {
        let trimmed = raw.trim();
        if !trimmed.is_empty() {
            return Some(PathBuf::from(trimmed));
        }
    }
    home.map(|h| h.join(".persatrix").join("credentials"))
}

/// The credential-file path for this machine.
fn path() -> Option<PathBuf> {
    let override_var = std::env::var(CREDENTIALS_FILE_ENV).ok();
    resolve_path(override_var.as_deref(), dirs::home_dir().as_deref())
}

/// Canonicalize the map key: `--server` with and without a trailing
/// slash must resolve the same stored session.
fn server_key(server: &str) -> String {
    server.trim_end_matches('/').to_string()
}

/// Read the whole file. Absent → empty map; present-but-malformed →
/// error, so a corrupted file can never be silently clobbered on the
/// next write (the operator decides whether to delete it).
fn read_all_at(path: &Path) -> Result<BTreeMap<String, CredentialEntry>, String> {
    let raw = match fs::read_to_string(path) {
        Ok(raw) => raw,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(BTreeMap::new()),
        Err(e) => return Err(format!("could not read {}: {e}", path.display())),
    };
    if raw.trim().is_empty() {
        return Ok(BTreeMap::new());
    }
    serde_json::from_str(&raw)
        .map_err(|e| format!("malformed credential file {}: {e}", path.display()))
}

/// Write the map back at mode `0600`, creating the parent directory.
fn write_all_at(path: &Path, all: &BTreeMap<String, CredentialEntry>) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("could not create {}: {e}", parent.display()))?;
    }
    let body = serde_json::to_string_pretty(all).map_err(|e| e.to_string())?;
    fs::write(path, body + "\n").map_err(|e| format!("could not write {}: {e}", path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))
            .map_err(|e| format!("could not chmod {}: {e}", path.display()))?;
    }
    Ok(())
}

fn token_at(path: &Path, server: &str) -> Option<String> {
    // Read paths are tolerant: a malformed file reads as "no token" so
    // ordinary commands keep working; the strict error surfaces on the
    // next `login`/`logout` write instead.
    let all = read_all_at(path).ok()?;
    all.get(&server_key(server)).map(|e| e.token.clone())
}

fn store_at(path: &Path, server: &str, entry: CredentialEntry) -> Result<(), String> {
    let mut all = read_all_at(path)?;
    all.insert(server_key(server), entry);
    write_all_at(path, &all)
}

fn clear_at(path: &Path, server: &str) -> Result<bool, String> {
    let mut all = read_all_at(path)?;
    let removed = all.remove(&server_key(server)).is_some();
    if removed {
        write_all_at(path, &all)?;
    }
    Ok(removed)
}

// ─── Public wrappers (resolve the real path, then delegate) ─────────────────

const NO_PATH: &str = "could not resolve the credential file location \
    (no home directory found; set PERSATRIX_CREDENTIALS_FILE)";

/// The stored bearer token for `server`, if any.
pub(crate) fn token(server: &str) -> Option<String> {
    path().and_then(|p| token_at(&p, server))
}

/// Store `entry` as the session for `server` (mode `0600`).
pub(crate) fn store(server: &str, entry: CredentialEntry) -> Result<(), String> {
    let p = path().ok_or(NO_PATH)?;
    store_at(&p, server, entry)
}

/// Remove the stored session for `server`; returns whether one existed.
pub(crate) fn clear(server: &str) -> Result<bool, String> {
    let p = path().ok_or(NO_PATH)?;
    clear_at(&p, server)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn entry(token: &str) -> CredentialEntry {
        CredentialEntry {
            token: token.to_string(),
            participant_id: Some("op-h".to_string()),
            role: Some("operator".to_string()),
            expires_at: None,
        }
    }

    // ─── resolve_path precedence (pure — no env/fs) ─────────────────────────

    #[test]
    fn resolve_path_prefers_override() {
        let home = PathBuf::from("/home/op");
        let got = resolve_path(Some("/custom/creds"), Some(&home)).unwrap();
        assert_eq!(got, PathBuf::from("/custom/creds"));
    }

    #[test]
    fn resolve_path_falls_back_to_home() {
        let home = PathBuf::from("/home/op");
        let got = resolve_path(None, Some(&home)).unwrap();
        assert_eq!(got, home.join(".persatrix").join("credentials"));
    }

    #[test]
    fn resolve_path_ignores_blank_override() {
        let home = PathBuf::from("/home/op");
        let got = resolve_path(Some("   "), Some(&home)).unwrap();
        assert_eq!(got, home.join(".persatrix").join("credentials"));
    }

    #[test]
    fn resolve_path_none_without_home_or_override() {
        assert!(resolve_path(None, None).is_none());
    }

    // ─── store / token / clear round-trip (TempDir — never the real home) ───

    #[test]
    fn store_then_token_round_trips() {
        let dir = TempDir::new().unwrap();
        let p = dir.path().join("credentials");
        store_at(&p, "http://localhost:8080", entry("tok-1")).unwrap();
        assert_eq!(
            token_at(&p, "http://localhost:8080").as_deref(),
            Some("tok-1")
        );
    }

    #[test]
    fn keyed_by_server_no_collision() {
        // §J: keyed by the orchestrator URL so multiple orchestrators do
        // not collide — storing one must not disturb the other.
        let dir = TempDir::new().unwrap();
        let p = dir.path().join("credentials");
        store_at(&p, "http://localhost:8080", entry("local-tok")).unwrap();
        store_at(&p, "https://prod.example.com", entry("prod-tok")).unwrap();
        assert_eq!(
            token_at(&p, "http://localhost:8080").as_deref(),
            Some("local-tok")
        );
        assert_eq!(
            token_at(&p, "https://prod.example.com").as_deref(),
            Some("prod-tok")
        );
    }

    #[test]
    fn trailing_slash_resolves_same_key() {
        let dir = TempDir::new().unwrap();
        let p = dir.path().join("credentials");
        store_at(&p, "http://localhost:8080/", entry("tok")).unwrap();
        assert_eq!(
            token_at(&p, "http://localhost:8080").as_deref(),
            Some("tok")
        );
    }

    #[test]
    fn clear_removes_only_that_server() {
        let dir = TempDir::new().unwrap();
        let p = dir.path().join("credentials");
        store_at(&p, "http://a", entry("tok-a")).unwrap();
        store_at(&p, "http://b", entry("tok-b")).unwrap();
        assert!(clear_at(&p, "http://a").unwrap());
        assert!(token_at(&p, "http://a").is_none());
        assert_eq!(token_at(&p, "http://b").as_deref(), Some("tok-b"));
        // Clearing an absent entry reports false and is not an error.
        assert!(!clear_at(&p, "http://a").unwrap());
    }

    #[test]
    fn missing_file_reads_as_no_token() {
        let dir = TempDir::new().unwrap();
        assert!(token_at(&dir.path().join("nope"), "http://a").is_none());
    }

    #[test]
    fn malformed_file_errors_on_write_but_reads_as_none() {
        // A corrupted file must never be silently clobbered by the next
        // login — the strict error is on the WRITE path; reads degrade
        // to "not logged in".
        let dir = TempDir::new().unwrap();
        let p = dir.path().join("credentials");
        fs::write(&p, "{not json").unwrap();
        assert!(token_at(&p, "http://a").is_none());
        assert!(store_at(&p, "http://a", entry("tok")).is_err());
        assert!(clear_at(&p, "http://a").is_err());
    }

    #[cfg(unix)]
    #[test]
    fn credential_file_is_mode_0600() {
        use std::os::unix::fs::PermissionsExt;
        let dir = TempDir::new().unwrap();
        let p = dir.path().join("credentials");
        store_at(&p, "http://a", entry("tok")).unwrap();
        let mode = fs::metadata(&p).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600, "the token is a live credential (§J)");
    }

    #[test]
    fn write_creates_missing_parent_dir() {
        let dir = TempDir::new().unwrap();
        let p = dir
            .path()
            .join("nested")
            .join(".persatrix")
            .join("credentials");
        store_at(&p, "http://a", entry("tok")).unwrap();
        assert_eq!(token_at(&p, "http://a").as_deref(), Some("tok"));
    }
}
