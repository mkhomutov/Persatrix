use std::io::{self, BufRead, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use colored::Colorize;

use crate::types::{api_error_message, validate_resource_id, ChatRequest, ChatResponse};

/// Interactive REPL for chatting with a persona agent via the orchestrator
/// REST endpoint `POST /api/v1/agents/{agent_id}/chat`.
pub(crate) async fn cmd_chat(
    _client: &reqwest::Client,
    server: &str,
    agent_id: &str,
    user_id: &str,
) -> Result<(), String> {
    validate_resource_id(agent_id, "agent ID")?;

    // Build a dedicated client with a longer timeout for chat (agent LLM
    // calls can take a while).
    let chat_client = reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(300))
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

    let stdin = io::stdin();
    let mut reader = stdin.lock();
    let mut session_id = String::new();

    loop {
        if !running.load(Ordering::SeqCst) {
            break;
        }

        // Prompt
        print!("{} ", "You:".bold());
        io::stdout().flush().ok();

        let mut line = String::new();
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
            session_id: session_id.clone(),
            participant_type: "user".to_string(),
        };

        // Spawn a spinner task that activates after ~2 seconds
        let spinner_active = Arc::new(AtomicBool::new(false));
        let spinner_done = Arc::new(AtomicBool::new(false));
        let spinner_handle = {
            let active = Arc::clone(&spinner_active);
            let done = Arc::clone(&spinner_done);
            let aid = agent_id.to_string();
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
                        eprint!("\r{}\r", " ".repeat(40));
                        return;
                    }
                    eprint!("\r{} Waiting for {}...", frames[i % frames.len()], aid);
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
            eprint!("\r{}\r", " ".repeat(40));
        }

        let resp = resp.map_err(|e| format!("connection failed: {e}"))?;

        if !resp.status().is_success() {
            let msg = api_error_message(resp).await;
            eprintln!("{} {msg}", "error:".red().bold());
            continue;
        }

        let chat_resp: ChatResponse = resp
            .json()
            .await
            .map_err(|e| format!("invalid response: {e}"))?;

        // Capture session_id from first response
        if session_id.is_empty() && !chat_resp.session_id.is_empty() {
            session_id = chat_resp.session_id.clone();
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
