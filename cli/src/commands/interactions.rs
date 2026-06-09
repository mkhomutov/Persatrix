//! `agent interactions` subcommand — the CLI half of the v0.3.8
//! interaction-summary surface (RFC 0020 §C/§D).
//!
//! When an interaction closes — by the RFC 0030 Layer 4 end-vote, by the
//! Layer 1 cost ceiling, or by going idle — the persona persists a synthesised
//! summary (the read API PR 1 landed; `internal/server/interactions_handler.go`
//! proxies the agent's `GetClosedInteractions` gRPC to JSON). This command reads
//! those closed-interaction summaries so a converged brainstorm hands back a
//! readable outcome from the terminal, not just a stop — the CLI sibling of the
//! web console's `InteractionSummary.svelte` (PR 2).
//!
//! Thin-client pattern (per `.github/instructions/rust-cli.instructions.md`):
//! the command marshals args into the `GET /api/v1/agents/{id}/interactions/closed`
//! call and prints the response. The wire shape mirrors
//! `closedInteractionsResponse` / `closedInteractionDTO`; the pure helpers
//! (query builder, trigger label, sentinel detection, render) mirror the web
//! data layer's `interactions.js` so the two surfaces label a close the same way
//! and are unit-tested without an HTTP server.

use colored::Colorize;
use serde::{Deserialize, Serialize};

use crate::types::{api_error_message, validate_resource_id};

/// Default page size when `--limit` is omitted. Mirrors
/// `defaultClosedInteractionsLimit` in `internal/server/interactions_handler.go`
/// so the CLI and server agree on the unsupplied-limit window.
pub(crate) const DEFAULT_CLOSED_INTERACTIONS_LIMIT: u32 = 20;

/// The failure sentinel the agent persists when the on-close summariser could
/// not produce a summary (`interaction_janitor.py SUMMARY_UNAVAILABLE_TEXT`).
/// The read path forwards it verbatim; this surface renders it as an explicit
/// "unavailable" line, never blanked (SS3). Mirrors `SUMMARY_UNAVAILABLE_TEXT`
/// in `web/src/lib/interactions.js`.
pub(crate) const SUMMARY_UNAVAILABLE_TEXT: &str = "[interaction summary unavailable]";

/// One closed-interaction row. Mirrors `closedInteractionDTO`
/// (`internal/server/interactions_handler.go`). `#[serde(default)]` on every
/// field keeps deserialization tolerant of an older server that omits one — the
/// CLI never sets `deny_unknown_fields`, so newer server fields are dropped.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub(crate) struct ClosedInteraction {
    #[serde(default)]
    pub(crate) interaction_id: String,
    #[serde(default)]
    pub(crate) scope: String,
    #[serde(default)]
    pub(crate) started_at: f64,
    #[serde(default)]
    pub(crate) closed_at: f64,
    #[serde(default)]
    pub(crate) turn_count: i32,
    #[serde(default)]
    pub(crate) close_reason: String,
    #[serde(default)]
    pub(crate) summary: String,
    #[serde(default)]
    pub(crate) participants: Vec<String>,
}

/// The `{interactions: [...]}` envelope. Mirrors `closedInteractionsResponse`.
#[derive(Debug, Deserialize)]
pub(crate) struct ClosedInteractionsResponse {
    #[serde(default)]
    pub(crate) interactions: Vec<ClosedInteraction>,
}

// ─── Pure helpers (testable without an HTTP server) ─────────────────────

/// Build the query-string suffix for the closed-interactions read.
///
/// Each param rides only when supplied, mirroring `getClosedInteractions` in
/// `web/src/lib/api.js`. The REST handler rejects an explicit `min_turns` < 1
/// with a 400 (an interaction always has ≥ 1 turn), so the caller passes
/// `None` to omit it rather than `Some(0)`. Returns `""` when nothing is set.
pub(crate) fn build_closed_interactions_query(
    scope: Option<&str>,
    interaction_id: Option<&str>,
    limit: u32,
    min_turns: Option<u32>,
) -> String {
    let mut params: Vec<String> = Vec::new();
    if let Some(s) = scope.filter(|s| !s.is_empty()) {
        params.push(format!("scope={}", urlencode(s)));
    }
    if let Some(id) = interaction_id.filter(|s| !s.is_empty()) {
        params.push(format!("interaction_id={}", urlencode(id)));
    }
    // Always pin the page size: the server defaults it when absent, but sending
    // it keeps the CLI's `--limit` authoritative and the request self-describing.
    params.push(format!("limit={limit}"));
    if let Some(mt) = min_turns {
        params.push(format!("min_turns={mt}"));
    }
    if params.is_empty() {
        String::new()
    } else {
        format!("?{}", params.join("&"))
    }
}

/// Percent-encode a query-parameter value. Scopes and interaction ids can carry
/// `:` (DM scopes, `group:` ids) and the rare space, so encode the small set of
/// characters that would otherwise break the query rather than pull in a URL
/// crate for two call sites.
fn urlencode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char);
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// Turn the RFC 0020 `close_reason` into a short human label, mirroring
/// `closeTriggerLabel` in `web/src/lib/interactions.js` so the two surfaces
/// label a close identically. The reasons are the `boundary_detectors.py`
/// literals; the episode row does not distinguish a Layer 4 vote-close from a
/// plain structural close, so "ended" is the honest label rather than
/// over-claiming "by vote". An unknown / empty reason degrades to "closed".
pub(crate) fn close_trigger_label(reason: &str) -> &'static str {
    match reason {
        "cost" => "cost limit reached",
        "idle_gap" => "went idle",
        "structural" => "ended",
        _ => "closed",
    }
}

/// Whether a summary body is the failure sentinel (so the surface shows an
/// explicit "unavailable" line rather than the raw marker). Mirrors
/// `isSummaryUnavailable`. A blank summary is NOT the sentinel — the read path
/// filters blanks, so a row reaching here has a real summary or the sentinel.
pub(crate) fn is_summary_unavailable(summary: &str) -> bool {
    summary == SUMMARY_UNAVAILABLE_TEXT
}

/// Render one closed interaction as a human-readable block: a header line
/// (id + close trigger + turn count), the participants, and either the summary
/// or the honest "summary unavailable" line (SS3).
pub(crate) fn format_interaction(it: &ClosedInteraction) -> String {
    let mut lines: Vec<String> = Vec::new();
    let turns = if it.turn_count == 1 {
        "1 turn".to_string()
    } else {
        format!("{} turns", it.turn_count)
    };
    lines.push(format!(
        "{}  {} ({})",
        it.interaction_id.cyan().bold(),
        format!("Conversation {}", close_trigger_label(&it.close_reason)).bold(),
        turns
    ));
    if !it.scope.is_empty() {
        lines.push(format!("  {} {}", "scope:".dimmed(), it.scope));
    }
    if !it.participants.is_empty() {
        lines.push(format!(
            "  {} {}",
            "participants:".dimmed(),
            it.participants.join(", ")
        ));
    }
    if is_summary_unavailable(&it.summary) {
        lines.push(format!(
            "  {}",
            "Summary unavailable for this interaction."
                .italic()
                .yellow()
        ));
    } else {
        lines.push(format!("  {}", it.summary));
    }
    lines.join("\n")
}

// ─── Subcommand entry point ─────────────────────────────────────────────

/// `persatrix agent interactions <agent_id> [--scope] [--interaction-id]
/// [--limit] [--min-turns] [--json]` — print an agent's closed-interaction
/// summaries (newest-first).
#[allow(clippy::too_many_arguments)]
pub(crate) async fn cmd_agent_interactions(
    client: &reqwest::Client,
    server: &str,
    agent_id: &str,
    scope: Option<&str>,
    interaction_id: Option<&str>,
    limit: u32,
    min_turns: Option<u32>,
    json_out: bool,
) -> Result<(), String> {
    // Agent IDs follow the same cross-component contract as the other agent
    // subcommands; reject locally with a clear message rather than round-trip a
    // generic 400 from the unauthenticated REST surface.
    validate_resource_id(agent_id, "agent ID")?;

    let suffix = build_closed_interactions_query(scope, interaction_id, limit, min_turns);
    let url = format!("{server}/api/v1/agents/{agent_id}/interactions/closed{suffix}");
    let resp = client
        .get(&url)
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }
    let body: ClosedInteractionsResponse = resp
        .json()
        .await
        .map_err(|e| format!("invalid response: {e}"))?;

    if json_out {
        // Single-line (not pretty-printed): keeps `--json` consistent with the
        // other subcommands so `jq` / line-counting consumers see one shape.
        println!("{}", serde_json::to_string(&body.interactions).unwrap());
        return Ok(());
    }

    if body.interactions.is_empty() {
        // A valid query with no closed interaction yet is not an error — mirror
        // `channel history`'s empty-state message and exit 0.
        let scope_note = scope
            .filter(|s| !s.is_empty())
            .map(|s| format!(" in scope {s}"))
            .unwrap_or_default();
        println!("No closed interactions for {agent_id}{scope_note}.");
        return Ok(());
    }

    // The read API returns newest-first; render in that order (the first block
    // is the latest closed interaction).
    let blocks: Vec<String> = body.interactions.iter().map(format_interaction).collect();
    println!("{}", blocks.join("\n\n"));
    Ok(())
}

#[cfg(test)]
#[path = "interactions_tests.rs"]
mod tests;
