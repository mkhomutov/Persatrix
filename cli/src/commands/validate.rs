use colored::Colorize;
use tokio::process::Command as ProcessCommand;

// NOTE: `validate` is the only CLI command that runs locally (subprocess) instead
// of via the orchestrator REST API. This deviates from the thin-client pattern.
// A server-side POST /api/v1/config/validate endpoint would be architecturally
// consistent — tracked for future improvement.
pub(crate) async fn cmd_validate(path: &str, strict: bool) -> Result<(), String> {
    // Whitespace-only paths pass through to Python and produce confusing errors.
    if path.trim().is_empty() {
        return Err("validation path cannot be empty".to_string());
    }

    // Python validator does not implement --strict yet.
    if strict {
        eprintln!(
            "{}",
            "warning: --strict is not yet supported by the Python validator, ignored".yellow()
        );
    }

    let script = find_validator_script()?;
    let python = find_python_binary();
    let args = vec![script.to_string_lossy().to_string(), path.to_string()];

    // Async subprocess avoids blocking a tokio worker thread.
    // Timeout prevents indefinite hang if the Python process stalls.
    let mut cmd = ProcessCommand::new(python);
    cmd.args(&args);
    let output = tokio::time::timeout(std::time::Duration::from_secs(120), cmd.output())
        .await
        .map_err(|_| "Python validator timed out after 120 seconds".to_string())?
        .map_err(|e| {
            if e.kind() == std::io::ErrorKind::NotFound {
                python_not_found_message()
            } else {
                format!("failed to run Python validator: {e}")
            }
        })?;

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);

    if !stdout.is_empty() {
        print!("{stdout}");
    }
    if !stderr.is_empty() {
        eprint!("{stderr}");
    }

    if output.status.success() {
        Ok(())
    } else {
        Err("validation failed".to_string())
    }
}

pub(crate) fn find_validator_script() -> Result<std::path::PathBuf, String> {
    // Try relative to CWD first (most common: running from repo root)
    let cwd_relative = std::path::PathBuf::from("agents/validate.py");
    if cwd_relative.exists() {
        // Canonicalize to produce clean absolute paths in error messages.
        return std::fs::canonicalize(&cwd_relative)
            .map_err(|e| format!("failed to canonicalize {}: {e}", cwd_relative.display()));
    }

    // Try relative to the executable location (installed or bin/ layout)
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            let from_bin = parent.join("../agents/validate.py");
            if from_bin.exists() {
                // Canonicalize here too.
                return std::fs::canonicalize(&from_bin)
                    .map_err(|e| format!("failed to canonicalize {}: {e}", from_bin.display()));
            }
        }
    }

    Err("cannot find agents/validate.py \u{2014} run from the repository root".to_string())
}

/// Return the Python interpreter binary name for the current platform.
/// Windows: `python` (standard name via installer or py launcher).
/// Unix/macOS: `python3` is preferred — `python` may be absent or
/// Python 2 on some Linux distributions.
pub(crate) fn find_python_binary() -> &'static str {
    if cfg!(windows) {
        "python"
    } else {
        "python3"
    }
}

/// Diagnostic error message when Python is not found on PATH.
fn python_not_found_message() -> String {
    let binary = find_python_binary();
    format!("Python not found. Install Python 3.11+ and ensure '{binary}' is on PATH.")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn find_python_binary_returns_platform_appropriate() {
        let binary = find_python_binary();
        if cfg!(windows) {
            assert_eq!(binary, "python");
        } else {
            assert_eq!(binary, "python3");
        }
    }

    #[test]
    fn python_not_found_message_contains_binary_name() {
        let msg = python_not_found_message();
        assert!(msg.contains("Python not found"));
        assert!(msg.contains(find_python_binary()));
        assert!(msg.contains("3.11+"));
    }

    // WARNING: This test mutates process-global CWD via set_current_dir(),
    // which is not thread-safe. `cargo test` runs tests in parallel. If another
    // test also depends on CWD, results become nondeterministic. Safe today
    // because no other test touches CWD, but should be addressed via
    // `serial_test` crate or by refactoring find_validator_script() to accept
    // an explicit base directory.
    #[test]
    fn find_validator_script_in_temp_dir() {
        let tmp = std::env::temp_dir().join("orch_test_validator");
        let agents_dir = tmp.join("agents");
        std::fs::create_dir_all(&agents_dir).unwrap();
        let script = agents_dir.join("validate.py");
        std::fs::write(&script, "# test").unwrap();

        let original_dir = std::env::current_dir().unwrap();
        std::env::set_current_dir(&tmp).unwrap();
        let result = find_validator_script();
        std::env::set_current_dir(original_dir).unwrap();

        // Cleanup
        std::fs::remove_dir_all(&tmp).ok();

        assert!(result.is_ok(), "expected Ok, got: {result:?}");
        // Result should be canonicalized (absolute path)
        let path = result.unwrap();
        assert!(path.is_absolute(), "expected absolute path, got: {path:?}");
    }
}
