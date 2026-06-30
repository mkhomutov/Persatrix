//! `persatrix channel convene <id>` — RFC 0052 §B self-convening (v0.3.11 PR 3).
//!
//! Thin wrapper over `POST /api/v1/channels/{id}/convene`: the operator action
//! that opens an autonomous agent-only channel with no human message. The
//! orchestrator dispatches a convene forced turn to the channel's configured
//! `autonomous.convener`, which authors the opening turn; from there the
//! discussion sustains itself. The endpoint is gated server-side behind the
//! `config_edit_enabled` toggle (a `403` means the operator surface is off);
//! it `404`s an unknown channel and `409`s one that is not `autonomous.enabled`,
//! already has a live interaction, or has no floor-capable audience besides the
//! convener. The server's wording is surfaced verbatim via `api_error_message`,
//! so the verb needs no per-status handling.
//!
//! Extracted to its own module (mirroring `channel_convene_handlers.go` and the
//! `channel_config_autonomous.rs` split) so `channel.rs` / `channel_dispatch.rs`
//! stay under the 500-line review cap.

use colored::Colorize;
use serde::Deserialize;
use serde_json::json;

use crate::commands::channel::canonicalize_channel_id;
use crate::types::{api_error_message, validate_path_param};

/// The 202 body the convene endpoint returns — the channel and the convener the
/// opening turn was dispatched to (see `conveneResponse` in
/// `internal/server/channel_convene_handlers.go`).
#[derive(Deserialize)]
struct ConveneResponse {
    #[serde(default)]
    convener: String,
}

/// `persatrix channel convene <id>` → `POST /api/v1/channels/{id}/convene`.
pub(crate) async fn cmd_convene(
    client: &reqwest::Client,
    server: &str,
    name: &str,
    json_out: bool,
) -> Result<(), String> {
    let canonical = canonicalize_channel_id(name);
    validate_path_param(&canonical, "channel id")?;
    let resp = client
        .post(format!("{server}/api/v1/channels/{canonical}/convene"))
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }
    // The convener id rides the ack so the operator sees who is opening the
    // discussion; tolerate an empty body (a future server that returns 202 with
    // no payload) rather than failing a successful convene on a decode error.
    let convener = resp
        .json::<ConveneResponse>()
        .await
        .map(|r| r.convener)
        .unwrap_or_default();
    if json_out {
        let payload = json!({
            "channel_id": canonical,
            "convener": convener,
            "status": "convening",
        });
        println!("{}", serde_json::to_string(&payload).unwrap());
    } else if convener.is_empty() {
        println!("Convening {}", format!("#{canonical}").cyan());
    } else {
        println!(
            "Convening {} (convener: {})",
            format!("#{canonical}").cyan(),
            convener.bold()
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    // The convene verb canonicalizes a bare name to `group:<name>` (the convene
    // path is a group concept) before validating + sending — same contract as
    // every other channel verb.
    #[test]
    fn canonicalizes_bare_name_to_group() {
        assert_eq!(canonicalize_channel_id("planning"), "group:planning");
        assert_eq!(canonicalize_channel_id("group:planning"), "group:planning");
    }

    // An invalid channel id is rejected locally, before any HTTP round-trip, by
    // the shared `validate_path_param` guard the command runs first.
    #[tokio::test]
    async fn rejects_invalid_channel_id_before_request() {
        let client = reqwest::Client::new();
        // A path-traversal id never reaches the network: validate_path_param
        // rejects it, so the unreachable server address is never dialed.
        let err = cmd_convene(&client, "http://127.0.0.1:0", "../etc", false)
            .await
            .expect_err("an invalid channel id must be rejected locally");
        assert!(
            !err.contains("connection failed"),
            "validation must fail before the HTTP request, got: {err}"
        );
    }

    // Clap-parse coverage for the verb lives here (not in channel_dispatch.rs,
    // which is at the file-size cap): a local Parser wrapper exercises the
    // `ChannelCommands::Convene` variant without pulling the binary's private
    // `Cli` into scope (the session_tests.rs precedent).
    #[derive(clap::Parser)]
    struct TestCli {
        #[command(subcommand)]
        cmd: crate::commands::channel_dispatch::ChannelCommands,
    }

    #[test]
    fn parses_convene_verb() {
        use crate::commands::channel_dispatch::ChannelCommands;
        use clap::Parser;
        let cli = TestCli::try_parse_from(["x", "convene", "group:planning"]).unwrap();
        match cli.cmd {
            ChannelCommands::Convene { name, json } => {
                assert_eq!(name, "group:planning");
                assert!(!json, "--json must default off");
            }
            _ => panic!("expected Convene"),
        }
    }

    #[test]
    fn parses_convene_verb_with_json() {
        use crate::commands::channel_dispatch::ChannelCommands;
        use clap::Parser;
        let cli = TestCli::try_parse_from(["x", "convene", "planning", "--json"]).unwrap();
        match cli.cmd {
            ChannelCommands::Convene { json, .. } => assert!(json),
            _ => panic!("expected Convene"),
        }
    }

    #[test]
    fn convene_requires_a_channel_id() {
        use clap::Parser;
        assert!(TestCli::try_parse_from(["x", "convene"]).is_err());
    }
}
