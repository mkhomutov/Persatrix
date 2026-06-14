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
/// field keeps deserialization tolerant of an older server that omits one. The
/// typed struct drops fields it doesn't name (no `deny_unknown_fields`), but
/// that loss is confined to the human renderer — `--json` prints the server's
/// rows verbatim via [`raw_interactions_json`], so a newer field still survives.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub(crate) struct ClosedInteraction {
    #[serde(default)]
    pub(crate) interaction_id: String,
    /// The RFC 0030 governance interaction id the episode was opened under — a
    /// different namespace from `interaction_id` (the persona's agent-side RFC
    /// 0020 episode id). Surfaced so the channel-side id carried in the
    /// end-vote close logs is cross-referenceable (ISSUE-0102); empty when the
    /// interaction carried no governance id or the server predates the field.
    #[serde(default)]
    pub(crate) governance_interaction_id: String,
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
    // Append the turn count only when it is meaningful. A non-positive count
    // only arises from a forward-compat default (the read path guarantees
    // turn_count >= 1); mirror the web surface (`{#if record.turn_count}`),
    // which hides the count when falsy, rather than print "(0 turns)".
    let turns_suffix = match it.turn_count {
        1 => " (1 turn)".to_string(),
        n if n > 1 => format!(" ({n} turns)"),
        _ => String::new(),
    };
    lines.push(format!(
        "{}  {}{}",
        it.interaction_id.cyan().bold(),
        format!("Conversation {}", close_trigger_label(&it.close_reason)).bold(),
        turns_suffix
    ));
    if !it.scope.is_empty() {
        lines.push(format!("  {} {}", "scope:".dimmed(), it.scope));
    }
    // ISSUE-0102: the header id is the persona's agent-side RFC 0020 episode
    // id; surface the RFC 0030 governance interaction id this episode was
    // opened under (the one the end-vote close logs carry) on its own labelled
    // line so the two namespaces are visibly distinct and the channel-side id
    // is cross-referenceable. Omitted when empty (DM / thread / non-channel
    // interaction, or a server that predates the field).
    if !it.governance_interaction_id.is_empty() {
        lines.push(format!(
            "  {} {}",
            "governance:".dimmed(),
            it.governance_interaction_id
        ));
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

/// Reject the two numeric filters the REST surface would bounce with a 400
/// (`parseLimit` / `parseMinTurns` in `internal/server/interactions_handler.go`):
/// a `limit` of 0 and an *explicit* `min_turns` of 0. A page of zero rows is
/// meaningless and no interaction has fewer than one turn, so catch these
/// locally with a clear message rather than round-trip a generic 400 — the same
/// fail-fast contract the `agent_id` check follows. (The web sibling gets this
/// for free from JS falsy-0: `if (limit)` / `if (minTurns)` in `api.js`.) An
/// omitted `min_turns` (`None`) is fine — it forwards the server's 0 sentinel.
pub(crate) fn validate_interactions_filters(
    limit: u32,
    min_turns: Option<u32>,
) -> Result<(), String> {
    if limit == 0 {
        return Err("--limit must be a positive integer (≥ 1)".to_string());
    }
    if min_turns == Some(0) {
        return Err("--min-turns must be a positive integer (≥ 1)".to_string());
    }
    Ok(())
}

/// Compose the trailing note for the empty-state line so it names *every*
/// active filter, not just scope — otherwise a query that came up empty because
/// of `--interaction-id` / `--min-turns` reads as though the agent simply has no
/// closed interactions at all. Returns `""` when no filter is active.
pub(crate) fn empty_state_filters(
    scope: Option<&str>,
    interaction_id: Option<&str>,
    min_turns: Option<u32>,
) -> String {
    let mut parts: Vec<String> = Vec::new();
    if let Some(s) = scope.filter(|s| !s.is_empty()) {
        parts.push(format!("scope {s}"));
    }
    if let Some(id) = interaction_id.filter(|s| !s.is_empty()) {
        parts.push(format!("interaction {id}"));
    }
    if let Some(mt) = min_turns {
        parts.push(format!("min-turns {mt}"));
    }
    if parts.is_empty() {
        String::new()
    } else {
        format!(" (filtered by {})", parts.join(", "))
    }
}

/// Extract the `interactions` array from the raw response body verbatim, so a
/// field a newer server adds to a row survives `--json` (the flag promises the
/// raw row list, not a lossy re-encode through [`ClosedInteraction`]). Returns
/// `None` when the body isn't the expected `{interactions: [...]}` shape, so the
/// caller can fall back to the typed re-serialization.
pub(crate) fn raw_interactions_json(body_text: &str) -> Option<String> {
    let v: serde_json::Value = serde_json::from_str(body_text).ok()?;
    let rows = v.get("interactions")?;
    if !rows.is_array() {
        return None;
    }
    serde_json::to_string(rows).ok()
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
    // generic 400 from the unauthenticated REST surface. The numeric filters get
    // the same treatment (the server 400s a 0 limit / explicit 0 min_turns).
    validate_resource_id(agent_id, "agent ID")?;
    validate_interactions_filters(limit, min_turns)?;

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
    // Keep the raw body so `--json` can forward rows verbatim (lossless); the
    // human path deserializes the same text into the typed envelope.
    let body_text = resp
        .text()
        .await
        .map_err(|e| format!("invalid response: {e}"))?;
    let body: ClosedInteractionsResponse =
        serde_json::from_str(&body_text).map_err(|e| format!("invalid response: {e}"))?;

    if json_out {
        // Print the server's `interactions` array verbatim so a field a newer
        // server adds survives (the flag promises the raw row list); fall back to
        // the typed re-serialization only if the body isn't the envelope shape.
        // Single-line, like the other subcommands so `jq` / line-counting agree.
        let rows = raw_interactions_json(&body_text)
            .unwrap_or_else(|| serde_json::to_string(&body.interactions).unwrap());
        println!("{rows}");
        return Ok(());
    }

    if body.interactions.is_empty() {
        // A valid query with no closed interaction yet is not an error — mirror
        // `channel history`'s empty-state message and exit 0. The note names
        // every active filter so an empty result isn't mistaken for "none exist".
        let note = empty_state_filters(scope, interaction_id, min_turns);
        println!("No closed interactions for {agent_id}{note}.");
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
