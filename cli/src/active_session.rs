//! Active-session pointer file — RFC 0031 Phase 3 §E.
//!
//! This file is the operator's source of truth for "which persona-memory
//! session am I working in." `persatrix session use` and `session new
//! --activate` write the resolved session id here; `session current` reads it
//! back; PR 4's `--session` default will read it too. The file holds a single
//! line: the active session id.
//!
//! Location resolution: the `PERSATRIX_ACTIVE_SESSION_FILE` environment
//! variable (an explicit override, used by tests and unusual deployments) wins;
//! otherwise `~/.persatrix/active-session`. The orchestrator does **not** read
//! this file — it is CLI-local. The boot session stays sourced from
//! `PERSATRIX_SESSION_ID`, so a non-co-located CLI and orchestrator never have
//! to share a filesystem (decision recorded in the Phase 3 PR plan, PR 3).

use std::fs;
use std::path::{Path, PathBuf};

/// Environment override for the pointer-file location.
const ACTIVE_SESSION_FILE_ENV: &str = "PERSATRIX_ACTIVE_SESSION_FILE";

/// Resolve the pointer path from an explicit override and a home directory.
///
/// Kept pure (no environment or filesystem access) so the precedence is
/// unit-testable without touching the real environment or the operator's home
/// directory. The override wins when set to a non-blank value; otherwise the
/// path is `<home>/.persatrix/active-session`. Returns `None` only when there
/// is neither an override nor a resolvable home directory.
fn resolve_path(override_var: Option<&str>, home: Option<&Path>) -> Option<PathBuf> {
    if let Some(raw) = override_var {
        let trimmed = raw.trim();
        if !trimmed.is_empty() {
            return Some(PathBuf::from(trimmed));
        }
    }
    home.map(|h| h.join(".persatrix").join("active-session"))
}

/// The active-session pointer path for this machine.
pub(crate) fn path() -> Option<PathBuf> {
    let override_var = std::env::var(ACTIVE_SESSION_FILE_ENV).ok();
    resolve_path(override_var.as_deref(), dirs::home_dir().as_deref())
}

/// Read the active session id from `path`, or `None` when the file is absent or
/// blank. A trailing newline (and any surrounding whitespace) is trimmed.
fn read_at(path: &Path) -> Option<String> {
    let raw = fs::read_to_string(path).ok()?;
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

/// Write `id` as the active session, creating the parent directory if needed.
fn write_at(path: &Path, id: &str) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("could not create {}: {e}", parent.display()))?;
    }
    fs::write(path, format!("{id}\n"))
        .map_err(|e| format!("could not write {}: {e}", path.display()))
}

// ─── Public wrappers (resolve the real path, then delegate) ─────────────────

/// Message shown when no pointer location can be resolved (no home directory
/// and no override). Rare, but better than a silent no-op.
const NO_PATH: &str = "could not resolve the active-session file location \
    (no home directory found; set PERSATRIX_ACTIVE_SESSION_FILE)";

/// Read the currently active session id, if a pointer is set.
pub(crate) fn read() -> Option<String> {
    path().and_then(|p| read_at(&p))
}

/// Write `id` to the active-session pointer file.
pub(crate) fn write(id: &str) -> Result<(), String> {
    let p = path().ok_or(NO_PATH)?;
    write_at(&p, id)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use tempfile::TempDir;

    // ─── resolve_path precedence (pure — no env/fs) ──────────────────────────

    #[test]
    fn resolve_path_prefers_override() {
        let home = PathBuf::from("/home/op");
        let got = resolve_path(Some("/custom/pointer"), Some(&home)).unwrap();
        assert_eq!(got, PathBuf::from("/custom/pointer"));
    }

    #[test]
    fn resolve_path_falls_back_to_home() {
        let home = PathBuf::from("/home/op");
        let got = resolve_path(None, Some(&home)).unwrap();
        assert_eq!(got, home.join(".persatrix").join("active-session"));
    }

    #[test]
    fn resolve_path_ignores_blank_override() {
        // A blank override (empty or whitespace) must not win over the home
        // default — otherwise `PERSATRIX_ACTIVE_SESSION_FILE=` would point the
        // CLI at an unusable empty path.
        let home = PathBuf::from("/home/op");
        let got = resolve_path(Some("   "), Some(&home)).unwrap();
        assert_eq!(got, home.join(".persatrix").join("active-session"));
    }

    #[test]
    fn resolve_path_none_without_home_or_override() {
        assert!(resolve_path(None, None).is_none());
    }

    // ─── read_at / write_at round-trip (TempDir — never the real homedir) ────

    #[test]
    fn write_then_read_round_trips() {
        let dir = TempDir::new().unwrap();
        let p = dir.path().join("active-session");
        write_at(&p, "0190abcd-0000-7000-8000-000000000001").unwrap();
        assert_eq!(
            read_at(&p).as_deref(),
            Some("0190abcd-0000-7000-8000-000000000001")
        );
    }

    #[test]
    fn write_creates_missing_parent_dir() {
        // The operator's first `session use` runs before `~/.persatrix/` exists;
        // write must create the directory rather than fail.
        let dir = TempDir::new().unwrap();
        let p = dir
            .path()
            .join("nested")
            .join(".persatrix")
            .join("active-session");
        write_at(&p, "sess-1").unwrap();
        assert_eq!(read_at(&p).as_deref(), Some("sess-1"));
    }

    #[test]
    fn read_missing_file_is_none() {
        let dir = TempDir::new().unwrap();
        let p = dir.path().join("does-not-exist");
        assert!(read_at(&p).is_none());
    }

    #[test]
    fn read_trims_trailing_newline() {
        // write_at appends a newline; read_at must hand back the bare id so a
        // round-trip through the file is identity.
        let dir = TempDir::new().unwrap();
        let p = dir.path().join("active-session");
        fs::write(&p, "sess-2\n").unwrap();
        assert_eq!(read_at(&p).as_deref(), Some("sess-2"));
    }

    #[test]
    fn read_blank_file_is_none() {
        // A truncated/whitespace-only pointer reads as "no active session"
        // rather than an empty-string id that would misroute downstream.
        let dir = TempDir::new().unwrap();
        let p = dir.path().join("active-session");
        fs::write(&p, "  \n").unwrap();
        assert!(read_at(&p).is_none());
    }

    #[test]
    fn write_overwrites_existing_pointer() {
        // `session use` re-points an already-active session; the new id must
        // replace the old one cleanly (no append).
        let dir = TempDir::new().unwrap();
        let p = dir.path().join("active-session");
        write_at(&p, "first").unwrap();
        write_at(&p, "second").unwrap();
        assert_eq!(read_at(&p).as_deref(), Some("second"));
    }

    // ─── public wrappers honour PERSATRIX_ACTIVE_SESSION_FILE (env seam) ──────
    // The `resolve_path` precedence above is pure; this pins the real
    // `path()` → `read()` / `write()` wiring end-to-end against a TempDir the
    // override points at, so a typo in the env-var constant or a mis-wired
    // wrapper is caught — without ever touching the operator's real
    // `~/.persatrix/`. Safe to mutate the process env here: `path()` is the only
    // reader of this var and no other test calls it.
    #[test]
    fn public_wrappers_honour_env_override() {
        let dir = TempDir::new().unwrap();
        let pointer = dir.path().join("active-session");
        let prior = std::env::var(ACTIVE_SESSION_FILE_ENV).ok();
        std::env::set_var(ACTIVE_SESSION_FILE_ENV, &pointer);

        assert_eq!(path().as_deref(), Some(pointer.as_path()));
        assert!(read().is_none(), "no pointer written yet → None");
        write("env-sess").unwrap();
        assert_eq!(read().as_deref(), Some("env-sess"));

        // Restore the ambient environment so sibling tests are unaffected.
        match prior {
            Some(v) => std::env::set_var(ACTIVE_SESSION_FILE_ENV, v),
            None => std::env::remove_var(ACTIVE_SESSION_FILE_ENV),
        }
    }
}
