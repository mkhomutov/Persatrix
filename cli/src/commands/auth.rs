//! `persatrix login` / `logout` / `whoami` — RFC 0039 §J (Phase 1).
//!
//! Login prompts for the password (read without echo — never in argv,
//! the §J discipline), POSTs `/api/v1/auth/login` under the `bearer`
//! transport, and stores the returned token in the credential file
//! (`credentials.rs`, mode 0600, keyed by orchestrator URL). Logout
//! revokes server-side FIRST and clears the local token only after the
//! orchestrator answered — a token the server still honours must not be
//! forgotten locally. Under `auth.mode: disabled` these commands work
//! too: Phase 1 ships the mechanism inert, and `whoami` honestly
//! reports the anonymous `local` identity.

use std::io::Write;

use colored::Colorize;
use serde::{Deserialize, Serialize};

use crate::credentials::{self, CredentialEntry};
use crate::types::api_error_message;

#[derive(Serialize)]
struct LoginRequest<'a> {
    username: &'a str,
    password: &'a str,
    /// Explicit — the CLI is the canonical bearer caller (§A1).
    session_transport: &'a str,
}

#[derive(Deserialize)]
struct LoginResponse {
    token: Option<String>,
    #[serde(default)]
    expires_at: Option<String>,
    #[serde(default)]
    participant_id: Option<String>,
    #[serde(default)]
    role: Option<String>,
}

#[derive(Deserialize)]
struct WhoamiResponse {
    authenticated: bool,
    participant_id: String,
    #[serde(default)]
    role: Option<String>,
    #[serde(default)]
    username: Option<String>,
}

/// Read the login name: the `--username` flag, or an interactive prompt.
/// The prompt goes to stderr so stdout stays parseable.
fn resolve_username(flag: Option<&str>) -> Result<String, String> {
    if let Some(name) = flag {
        let trimmed = name.trim();
        if trimmed.is_empty() {
            return Err("--username must be non-empty".to_string());
        }
        return Ok(trimmed.to_string());
    }
    eprint!("Username: ");
    std::io::stderr().flush().ok();
    let mut line = String::new();
    std::io::stdin()
        .read_line(&mut line)
        .map_err(|e| format!("could not read username: {e}"))?;
    let name = line.trim().to_string();
    if name.is_empty() {
        return Err("username must be non-empty".to_string());
    }
    Ok(name)
}

/// Read the password without echo from the terminal (§J — never argv).
/// When stdin is not a terminal (a provisioning pipe), read one line
/// from stdin instead — the same fallback the Go `account bootstrap`
/// subcommand implements.
fn read_password() -> Result<String, String> {
    use std::io::IsTerminal;
    if std::io::stdin().is_terminal() {
        return rpassword::prompt_password("Password: ")
            .map_err(|e| format!("could not read password: {e}"));
    }
    eprint!("Password: ");
    std::io::stderr().flush().ok();
    let mut line = String::new();
    std::io::stdin()
        .read_line(&mut line)
        .map_err(|e| format!("could not read password: {e}"))?;
    Ok(line.trim_end_matches(['\r', '\n']).to_string())
}

pub(crate) async fn cmd_login(
    client: &reqwest::Client,
    server: &str,
    username: Option<&str>,
) -> Result<(), String> {
    let username = resolve_username(username)?;
    let password = read_password()?;

    let resp = client
        .post(format!("{server}/api/v1/auth/login"))
        .json(&LoginRequest {
            username: &username,
            password: &password,
            session_transport: "bearer",
        })
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;

    match resp.status().as_u16() {
        401 => return Err("invalid credentials".to_string()),
        429 => {
            let retry = resp
                .headers()
                .get("Retry-After")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("a few")
                .to_string();
            return Err(format!(
                "too many login attempts — retry in {retry} seconds"
            ));
        }
        _ if !resp.status().is_success() => return Err(api_error_message(resp).await),
        _ => {}
    }

    let body: LoginResponse = resp
        .json()
        .await
        .map_err(|e| format!("invalid response: {e}"))?;
    let token = body
        .token
        .ok_or("login succeeded but no bearer token was returned")?;

    credentials::store(
        server,
        CredentialEntry {
            token,
            participant_id: body.participant_id.clone(),
            role: body.role.clone(),
            expires_at: body.expires_at.clone(),
        },
    )?;

    println!(
        "{} logged in as {} (participant {}, role {})",
        "✓".green().bold(),
        username.bold(),
        body.participant_id.as_deref().unwrap_or("—"),
        body.role.as_deref().unwrap_or("—"),
    );
    if let Some(expires) = body.expires_at.as_deref() {
        println!("  session expires {expires}");
    }
    Ok(())
}

pub(crate) async fn cmd_logout(client: &reqwest::Client, server: &str) -> Result<(), String> {
    let Some(token) = credentials::token(server) else {
        println!("not logged in to {server}");
        return Ok(());
    };

    let resp = client
        .post(format!("{server}/api/v1/auth/logout"))
        .bearer_auth(&token)
        .send()
        .await
        .map_err(|e| {
            format!("connection failed: {e} — the stored token was NOT cleared; retry when the orchestrator is reachable")
        })?;

    // 204: revoked now. 401: the session is already dead server-side
    // (expired, pruned, or revoked elsewhere) — either way the token is
    // useless, so the local copy goes. Anything else keeps the token:
    // server-side revocation is the logout (§D), the local clear is
    // hygiene on top.
    match resp.status().as_u16() {
        204 => {
            credentials::clear(server)?;
            println!("{} logged out (session revoked)", "✓".green().bold());
            Ok(())
        }
        401 => {
            credentials::clear(server)?;
            println!("session was already expired or revoked; cleared the stored token");
            Ok(())
        }
        _ => Err(api_error_message(resp).await),
    }
}

pub(crate) async fn cmd_whoami(client: &reqwest::Client, server: &str) -> Result<(), String> {
    let mut req = client.get(format!("{server}/api/v1/auth/whoami"));
    if let Some(token) = credentials::token(server) {
        req = req.bearer_auth(token);
    }
    let resp = req
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }
    let body: WhoamiResponse = resp
        .json()
        .await
        .map_err(|e| format!("invalid response: {e}"))?;

    if body.authenticated {
        println!(
            "{} (participant {}, role {})",
            body.username.as_deref().unwrap_or("—").bold(),
            body.participant_id,
            body.role.as_deref().unwrap_or("—"),
        );
    } else {
        println!(
            "anonymous (participant {}) — not authenticated; run 'persatrix login' once auth.mode is enabled",
            body.participant_id
        );
    }
    Ok(())
}

#[cfg(test)]
#[path = "auth_tests.rs"]
mod tests;
