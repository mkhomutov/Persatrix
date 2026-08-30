use std::io::{self, BufRead, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use colored::Colorize;

use crate::types::{api_error_message, validate_resource_id, ChatRequest, ChatResponse};

/// Build the default-header map carrying the stored bearer token, or
/// `None` when there is no token (or its bytes are header-invalid, which
/// can only mean a corrupted credential file — act logged-out rather than
/// fail the command).
///
/// Split out of [`cmd_chat`] so the attach is pure and unit-testable: the
/// chat REPL builds its own long-timeout client, so it does NOT inherit the
/// default headers `main.rs` installs on the shared client. Losing that
/// attach made the chat POST the one unauthenticated verb on the whole CLI
/// surface — `persatrix chat` answered 401 for every logged-in caller under
/// `auth.mode: enabled` (found live at the v0.3.14 MT-MEMORY-MULTIUSER-001
/// run, F-1). The tests below are the regression bar.
fn bearer_headers(token: Option<String>) -> Option<reqwest::header::HeaderMap> {
    let token = token?;
    let mut value = reqwest::header::HeaderValue::from_str(&format!("Bearer {token}")).ok()?;
    // Mark sensitive so debug-logging middleware never prints the credential.
    value.set_sensitive(true);
    let mut headers = reqwest::header::HeaderMap::new();
    headers.insert(reqwest::header::AUTHORIZATION, value);
    Some(headers)
}

/// Interactive REPL for chatting with a persona agent via the orchestrator
/// REST endpoint `POST /api/v1/agents/{agent_id}/chat`.
pub(crate) async fn cmd_chat(
    client: &reqwest::Client,
    server: &str,
    agent_id: &str,
    user_id: &str,
    session_flag: Option<&str>,
    epoch_flag: Option<&str>,
) -> Result<(), String> {
    validate_resource_id(agent_id, "agent ID")?;
    // Validate user_id with the same resource-ID rules as agent_id.
    // Prevents arbitrary strings (whitespace, special chars) from propagating
    // to the server and into logs. Default "local" passes this check.
    validate_resource_id(user_id, "user ID")?;

    // RFC 0031 Phase 3 `--session` override: resolve once before the REPL
    // (the session is fixed for the conversation), applying the OQ #6
    // precedence. Empty when no session is in play — the field is then omitted
    // and the orchestrator keeps its boot default / auto-binding. The provided
    // `client` (short-timeout) is fine for the registry GET; the REPL builds
    // its own long-timeout client below for the chat turns.
    let operator_session_id =
        crate::session_resolve::resolve_for_invocation(client, server, session_flag)
            .await?
            .unwrap_or_default();

    // ISSUE-0085 `--epoch` override: resolve the run/test-isolation epoch
    // (flag > PERSATRIX_EPOCH env) once for the conversation. Empty when none
    // is in play — the field is then omitted and the orchestrator keeps its
    // boot default ("live"). No registry lookup (epoch has no lifecycle).
    let operator_epoch_id = crate::epoch_resolve::resolve_epoch(epoch_flag).unwrap_or_default();

    // Build a dedicated client with a longer timeout for chat (agent LLM
    // calls can take a while). Being a fresh client, it does not inherit
    // `main.rs`'s default headers — see [`bearer_headers`].
    let mut chat_builder = reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(300));
    if let Some(headers) = bearer_headers(crate::credentials::token(server)) {
        chat_builder = chat_builder.default_headers(headers);
    }
    let chat_client = chat_builder
        .build()
        .map_err(|e| format!("failed to create HTTP client: {e}"))?;

    let url = format!("{server}/api/v1/agents/{agent_id}/chat");

    // Ctrl-C handler: set flag so the REPL exits gracefully.
    let running = Arc::new(AtomicBool::new(true));
    {
        let running = Arc::clone(&running);
        ctrlc::set_handler(move || {
            running.store(false, Ordering::SeqCst);
        })
        .map_err(|e| format!("failed to set Ctrl-C handler: {e}"))?;
    }

    println!(
        "Connected to {}. Type {} or {} to quit.",
        agent_id.cyan(),
        "exit".bold(),
        "Ctrl-C".bold(),
    );

    // Blocking stdin read inside an async fn: intentional for a CLI REPL where
    // only one operation (read → send → display) happens at a time. The tokio
    // multi-thread runtime keeps other tasks (spinner) runnable on separate
    // worker threads. For non-blocking reads, consider tokio::io::stdin().
    let stdin = io::stdin();
    let mut reader = stdin.lock();
    // Local var holds RFC 0016's chat-session token within the REPL
    // function scope. The wire boundary (`ChatRequest.chat_session_id`,
    // `ChatResponse.chat_session_id`) uses the prefixed name; the local
    // name stays unprefixed per the function-scope rule.
    let mut session_id = String::new();

    loop {
        if !running.load(Ordering::SeqCst) {
            break;
        }

        // Prompt
        print!("{} ", "You:".bold());
        io::stdout().flush().ok();

        let mut line = String::new();
        // On Unix, Ctrl-C sends SIGINT which interrupts the blocking read_line,
        // causing it to return Err(Interrupted) → caught by Err(_) below.
        // On Windows with ctrlc v3, console reads are similarly interrupted.
        // The `running` flag is a fallback checked at the top of each iteration.
        match reader.read_line(&mut line) {
            Ok(0) => break, // EOF
            Ok(_) => {}
            Err(_) => break,
        }

        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        if trimmed == "exit" {
            break;
        }

        // Send message to orchestrator
        let req = ChatRequest {
            message: trimmed.to_string(),
            user_id: user_id.to_string(),
            chat_session_id: session_id.clone(),
            participant_type: "user".to_string(),
            session_id: operator_session_id.clone(),
            epoch_id: operator_epoch_id.clone(),
        };

        // Spawn a spinner task that activates after ~2 seconds
        let spinner_active = Arc::new(AtomicBool::new(false));
        let spinner_done = Arc::new(AtomicBool::new(false));
        let spinner_text = format!("Waiting for {}...", agent_id);
        // Clear width computed dynamically from spinner text length (spinner
        // prefix "⠋ " is 2 display chars) to handle long agent IDs without
        // a hardcoded magic number. (PR 6 review fix: PR 5 finding #3.)
        // NOTE: `str::len()` returns byte count, which equals display width
        // because agent IDs are pure ASCII per the project convention
        // (`^[a-z0-9][a-z0-9-]*[a-z0-9]$`). If the ID pattern is ever
        // relaxed to allow non-ASCII, switch to a unicode-width crate.
        let clear_width = spinner_text.len() + 2;
        let spinner_handle = {
            let active = Arc::clone(&spinner_active);
            let done = Arc::clone(&spinner_done);
            let text = spinner_text.clone();
            tokio::spawn(async move {
                tokio::time::sleep(Duration::from_secs(2)).await;
                if done.load(Ordering::SeqCst) {
                    return;
                }
                active.store(true, Ordering::SeqCst);
                let frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
                let mut i = 0;
                loop {
                    if done.load(Ordering::SeqCst) {
                        // Clear spinner line
                        eprint!("\r{}\r", " ".repeat(clear_width));
                        return;
                    }
                    eprint!("\r{} {}", frames[i % frames.len()], text);
                    i += 1;
                    tokio::time::sleep(Duration::from_millis(80)).await;
                }
            })
        };

        let resp = chat_client.post(&url).json(&req).send().await;

        // Stop spinner
        spinner_done.store(true, Ordering::SeqCst);
        spinner_handle.abort();
        if spinner_active.load(Ordering::SeqCst) {
            eprint!("\r{}\r", " ".repeat(clear_width));
        }

        // Connection error: print and continue instead of propagating,
        // preserving the session and session_id state.
        // (PR 6 review fix: PR 5 finding #1.)
        let resp = match resp {
            Ok(r) => r,
            Err(e) => {
                eprintln!("{} connection failed: {e}", "error:".red().bold());
                continue;
            }
        };

        if !resp.status().is_success() {
            let msg = api_error_message(resp).await;
            eprintln!("{} {msg}", "error:".red().bold());
            continue;
        }

        // JSON deserialization error: print and continue instead of
        // propagating, preserving the session and session_id state.
        // (PR 6 review fix: PR 5 finding #2.)
        let chat_resp: ChatResponse = match resp.json().await {
            Ok(r) => r,
            Err(e) => {
                eprintln!("{} invalid response: {e}", "error:".red().bold());
                continue;
            }
        };

        // Capture chat_session_id from first response
        if session_id.is_empty() && !chat_resp.chat_session_id.is_empty() {
            session_id = chat_resp.chat_session_id.clone();
        }

        // Display name: use agent_display_name, fall back to agent_id
        let display_name = if chat_resp.agent_display_name.is_empty() {
            agent_id.to_string()
        } else {
            chat_resp.agent_display_name.clone()
        };

        if chat_resp.reply_status == "empty" {
            println!("{}", format!("{display_name} did not respond.").dimmed());
        } else {
            println!(
                "{} {}",
                format!("{display_name}:").cyan().bold(),
                chat_resp.reply
            );
        }
    }

    Ok(())
}

#[cfg(test)]
#[path = "chat_tests.rs"]
mod tests;
