//! `persatrix logs <execution_id>` — RFC 0018 PR 6.
//!
//! Two modes share one entry point ([`cmd_logs`]):
//!
//! - **Snapshot** (default): a single `GET /api/v1/executions/{id}/logs`
//!   request returning the in-memory ring (oldest first), filtered by the
//!   server-side `since` / `workflow` / `level` / `limit` query params.
//! - **Follow** (`--follow`): a long-lived
//!   `GET /api/v1/executions/{id}/logs/stream` Server-Sent Events
//!   subscription that prints each new entry as it lands. Reconnects with
//!   exponential backoff on connection loss and prints a single
//!   `[reconnected]` info line per successful re-connect.
//!
//! The path segment value `_` is a documented sentinel for the merged
//! cross-execution view (see [RFC 0018 § E](../../docs/rfcs/0018-structured-logging-framework.md#e-on-disk-store-layout)
//! and the server-side `crossExecutionToken` constant in
//! [`internal/server/logs_handler.go`](../../internal/server/logs_handler.go)).

use std::time::Duration;

use colored::Colorize;
use futures_util::StreamExt;
use serde::Deserialize;

use crate::types::{api_error_message, validate_path_param};

/// Bag of optional filters and rendering flags. Grouped so callers (e.g.
/// `main.rs`) can name the fields at the call site without a six-positional
/// `cmd_logs(...)` invocation that violates the project's clippy budget.
#[derive(Debug, Default, Clone)]
pub(crate) struct LogsOptions<'a> {
    pub(crate) follow: bool,
    pub(crate) verbose: bool,
    pub(crate) since: Option<&'a str>,
    pub(crate) workflow: Option<&'a str>,
    /// Uppercase wire token (`DEBUG` / `INFO` / `WARN` / `ERROR`); the
    /// server's level filter is exact-match per RFC 0018 PR 5.
    pub(crate) level: Option<&'a str>,
    /// Client-side `trace_id` filter (the server has no `trace_id` query
    /// param today; applied after fetch). See RFC 0019 § G.
    pub(crate) trace: Option<&'a str>,
    /// Back-compat alias for `attributes["agent_id"]`; matched client-side
    /// because the server's `workflow` filter is the only attribute filter
    /// exposed in PR 5.
    pub(crate) agent: Option<&'a str>,
}

/// Initial backoff for SSE reconnect. Doubled on each successive failure
/// up to [`SSE_MAX_BACKOFF`].
const SSE_INITIAL_BACKOFF: Duration = Duration::from_millis(500);
/// Cap on the SSE reconnect backoff so a long outage doesn't push the
/// retry interval into the multi-minute range.
const SSE_MAX_BACKOFF: Duration = Duration::from_secs(15);

pub(crate) async fn cmd_logs(
    client: &reqwest::Client,
    server: &str,
    execution_id: &str,
    opts: LogsOptions<'_>,
) -> Result<(), String> {
    validate_path_param(execution_id, "execution ID")?;

    if opts.follow {
        follow_logs(client, server, execution_id, &opts).await
    } else {
        snapshot_logs(client, server, execution_id, &opts).await
    }
}

// ─── Snapshot path ───────────────────────────────────────────────────────

async fn snapshot_logs(
    client: &reqwest::Client,
    server: &str,
    execution_id: &str,
    opts: &LogsOptions<'_>,
) -> Result<(), String> {
    let url = build_logs_url(server, execution_id, opts, /* stream */ false);

    let resp = client
        .get(&url)
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;

    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }

    let entries: Vec<LogEntry> = resp
        .json()
        .await
        .map_err(|e| format!("failed to decode logs response: {e}"))?;

    for entry in entries.into_iter().filter(|e| client_filter(e, opts)) {
        println!("{}", render_entry(&entry, opts.verbose));
    }
    Ok(())
}

// ─── Follow / SSE path ───────────────────────────────────────────────────

async fn follow_logs(
    client: &reqwest::Client,
    server: &str,
    execution_id: &str,
    opts: &LogsOptions<'_>,
) -> Result<(), String> {
    let url = build_logs_url(server, execution_id, opts, /* stream */ true);

    let mut backoff = SSE_INITIAL_BACKOFF;
    let mut reconnect = false;
    loop {
        match consume_stream(client, &url, opts, reconnect).await {
            StreamOutcome::Closed => {
                // Server closed the stream cleanly (e.g. orchestrator shutdown).
                // Exit without an error — the operator's `--follow` session
                // should end the same way the underlying ring's lifetime did.
                return Ok(());
            }
            StreamOutcome::Fatal(msg) => return Err(msg),
            StreamOutcome::Retry(reason) => {
                eprintln!(
                    "{} {reason} (reconnecting in {:?})",
                    "warning:".yellow(),
                    backoff,
                );
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(SSE_MAX_BACKOFF);
                reconnect = true;
            }
        }
    }
}

enum StreamOutcome {
    /// Server closed the response body without an error — treated as EOF.
    Closed,
    /// Non-recoverable error (e.g. 4xx response from the server). Bubbles
    /// up to the CLI's top-level error handler.
    Fatal(String),
    /// Transient error; the follow loop sleeps `backoff` and reconnects.
    Retry(String),
}

async fn consume_stream(
    client: &reqwest::Client,
    url: &str,
    opts: &LogsOptions<'_>,
    is_reconnect: bool,
) -> StreamOutcome {
    let resp = match client
        .get(url)
        .header("Accept", "text/event-stream")
        // Disable per-request timeout: SSE streams are by definition
        // long-lived. The 15s heartbeat from the server keeps the
        // connection from going idle as far as proxies are concerned;
        // a real network drop surfaces as a stream read error and
        // triggers the reconnect path.
        .timeout(Duration::from_secs(60 * 60 * 24))
        .send()
        .await
    {
        Ok(r) => r,
        Err(e) => return StreamOutcome::Retry(format!("stream connect failed: {e}")),
    };

    let status = resp.status();
    if !status.is_success() {
        // 4xx are non-recoverable (bad ID, bad query, subscriber cap);
        // 5xx may be recoverable but the server-side log buffer cap
        // already throttles aggressive reconnects, so treat both as fatal
        // to avoid hiding a misconfiguration in a retry loop.
        return StreamOutcome::Fatal(api_error_message(resp).await);
    }

    if is_reconnect {
        // Single-line marker per the RFC 0018 PR 6 plan — operators
        // running `--follow` overnight should be able to grep for
        // disconnections in their terminal scrollback.
        eprintln!("{} [reconnected]", "info:".cyan());
    }

    let mut stream = resp.bytes_stream();
    let mut buf: Vec<u8> = Vec::with_capacity(4096);
    while let Some(chunk) = stream.next().await {
        let bytes = match chunk {
            Ok(b) => b,
            Err(e) => return StreamOutcome::Retry(format!("stream read failed: {e}")),
        };
        buf.extend_from_slice(&bytes);
        // SSE event delimiter is a blank line (`\n\n`); split greedily so
        // a single TCP chunk carrying multiple frames is handled in one
        // pass.
        while let Some(end) = find_event_end(&buf) {
            let frame: Vec<u8> = buf.drain(..end).collect();
            // Drain the `\n\n` delimiter we matched on.
            buf.drain(..2);
            handle_sse_frame(&frame, opts);
        }
    }
    // bytes_stream() ended without an error — server closed the response.
    StreamOutcome::Closed
}

fn find_event_end(buf: &[u8]) -> Option<usize> {
    buf.windows(2).position(|w| w == b"\n\n")
}

fn handle_sse_frame(frame: &[u8], opts: &LogsOptions<'_>) {
    // SSE frame is one or more `field: value` lines separated by `\n`.
    // The server emits exactly two shapes:
    //   `: heartbeat`            — comment line, ignore.
    //   `data: <json>`           — one entry payload.
    // Anything else is treated as `data:` for forward-compat with future
    // Persatrix-side fields (e.g. `event:`, `id:`).
    for line in frame.split(|&b| b == b'\n') {
        if line.is_empty() || line.starts_with(b":") {
            continue;
        }
        let payload = if let Some(rest) = line.strip_prefix(b"data: ") {
            rest
        } else if let Some(rest) = line.strip_prefix(b"data:") {
            rest
        } else {
            continue;
        };
        match serde_json::from_slice::<LogEntry>(payload) {
            Ok(entry) => {
                if client_filter(&entry, opts) {
                    println!("{}", render_entry(&entry, opts.verbose));
                }
            }
            Err(e) => {
                eprintln!("{} skipping malformed SSE frame: {e}", "warning:".yellow());
            }
        }
    }
}

// ─── URL construction + client-side filters + rendering ─────────────────

fn build_logs_url(
    server: &str,
    execution_id: &str,
    opts: &LogsOptions<'_>,
    stream: bool,
) -> String {
    let suffix = if stream { "/stream" } else { "" };
    let mut url = format!("{server}/api/v1/executions/{execution_id}/logs{suffix}");
    let mut sep = '?';
    let mut push = |url: &mut String, key: &str, val: &str| {
        url.push(sep);
        url.push_str(key);
        url.push('=');
        url.push_str(&urlencode(val));
        sep = '&';
    };
    if let Some(s) = opts.since {
        push(&mut url, "since", s);
    }
    if let Some(w) = opts.workflow {
        push(&mut url, "workflow", w);
    }
    if let Some(l) = opts.level {
        push(&mut url, "level", l);
    }
    url
}

/// Minimal percent-encoding for URL query values. Covers the ASCII
/// punctuation characters that may appear in a user-supplied workflow ID
/// or RFC 3339 timestamp; the alphanumerics, `-`, `_`, `.`, `~` are kept
/// verbatim per RFC 3986 unreserved set.
fn urlencode(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    for b in input.bytes() {
        let keep = b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~');
        if keep {
            out.push(b as char);
        } else {
            out.push('%');
            out.push_str(&format!("{b:02X}"));
        }
    }
    out
}

fn client_filter(entry: &LogEntry, opts: &LogsOptions<'_>) -> bool {
    if let Some(t) = opts.trace {
        if entry.trace_id.as_deref() != Some(t) {
            return false;
        }
    }
    if let Some(a) = opts.agent {
        let matches_field = entry.agent_id.as_deref() == Some(a);
        let matches_attr = entry
            .attributes
            .as_ref()
            .and_then(|m| m.get("agent_id"))
            .and_then(|v| v.as_str())
            == Some(a);
        if !matches_field && !matches_attr {
            return false;
        }
    }
    true
}

fn render_entry(entry: &LogEntry, verbose: bool) -> String {
    let level_colored = match entry.level.as_str() {
        "ERROR" => entry.level.red().bold(),
        "WARN" => entry.level.yellow().bold(),
        "INFO" => entry.level.cyan(),
        "DEBUG" => entry.level.dimmed(),
        _ => entry.level.normal(),
    };
    let agent = entry.agent_id.as_deref().unwrap_or("-");
    let line = format!(
        "{ts} {lvl:<5} [{agent}] {msg}",
        ts = entry.timestamp,
        lvl = level_colored,
        agent = agent,
        msg = entry.message,
    );
    if !verbose {
        return line;
    }
    let exec = entry.execution_id.as_deref().unwrap_or("-");
    let step = entry.step_id.as_deref().unwrap_or("-");
    let trace = entry.trace_id.as_deref().unwrap_or("-");
    let attrs = entry
        .attributes
        .as_ref()
        .map(|m| serde_json::to_string(m).unwrap_or_default())
        .unwrap_or_else(|| "{}".to_string());
    format!("{line}\n  execution_id={exec} step_id={step} trace_id={trace} attributes={attrs}")
}

// ─── Wire types ──────────────────────────────────────────────────────────
//
// Mirrors a *subset* of `internal/observability/logbuffer.Entry` (Go) — the
// fields the CLI renders or filters on. Kept narrow on purpose: forward
// compat is preserved by serde's default lenient parsing (unknown JSON
// keys are silently dropped).

#[derive(Debug, Deserialize)]
pub(crate) struct LogEntry {
    #[serde(default)]
    pub(crate) timestamp: String,
    #[serde(default)]
    pub(crate) level: String,
    #[serde(default)]
    pub(crate) message: String,
    #[serde(default)]
    pub(crate) agent_id: Option<String>,
    #[serde(default)]
    pub(crate) execution_id: Option<String>,
    #[serde(default)]
    pub(crate) step_id: Option<String>,
    #[serde(default)]
    pub(crate) trace_id: Option<String>,
    #[serde(default)]
    pub(crate) attributes: Option<serde_json::Map<String, serde_json::Value>>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn entry(level: &str, agent: Option<&str>, trace: Option<&str>) -> LogEntry {
        LogEntry {
            timestamp: "2026-04-23T00:00:00Z".to_string(),
            level: level.to_string(),
            message: "hello".to_string(),
            agent_id: agent.map(str::to_string),
            execution_id: Some("exec-1".to_string()),
            step_id: None,
            trace_id: trace.map(str::to_string),
            attributes: None,
        }
    }

    #[test]
    fn url_includes_all_filters() {
        let opts = LogsOptions {
            since: Some("5m"),
            workflow: Some("wf one"),
            level: Some("WARN"),
            ..LogsOptions::default()
        };
        let url = build_logs_url("http://h", "abc", &opts, false);
        assert!(url.contains("since=5m"), "url={url}");
        // Space in workflow id is percent-encoded.
        assert!(url.contains("workflow=wf%20one"), "url={url}");
        assert!(url.contains("level=WARN"), "url={url}");
    }

    #[test]
    fn url_uses_stream_suffix_for_follow() {
        let opts = LogsOptions::default();
        let url = build_logs_url("http://h", "abc", &opts, true);
        assert_eq!(url, "http://h/api/v1/executions/abc/logs/stream");
    }

    #[test]
    fn cross_execution_token_is_passed_through() {
        let opts = LogsOptions {
            since: Some("1h"),
            ..LogsOptions::default()
        };
        let url = build_logs_url("http://h", "_", &opts, false);
        assert!(url.starts_with("http://h/api/v1/executions/_/logs?"));
    }

    #[test]
    fn trace_filter_drops_non_matching_entries() {
        let opts = LogsOptions {
            trace: Some("abc"),
            ..LogsOptions::default()
        };
        assert!(!client_filter(&entry("INFO", None, Some("xyz")), &opts));
        assert!(client_filter(&entry("INFO", None, Some("abc")), &opts));
        assert!(!client_filter(&entry("INFO", None, None), &opts));
    }

    #[test]
    fn agent_filter_matches_field_or_attribute() {
        let opts = LogsOptions {
            agent: Some("coder"),
            ..LogsOptions::default()
        };
        assert!(client_filter(&entry("INFO", Some("coder"), None), &opts));

        let mut e = entry("INFO", None, None);
        let mut attrs = serde_json::Map::new();
        attrs.insert("agent_id".to_string(), json!("coder"));
        e.attributes = Some(attrs);
        assert!(client_filter(&e, &opts));

        assert!(!client_filter(
            &entry("INFO", Some("reviewer"), None),
            &opts
        ));
    }

    #[test]
    fn render_default_omits_correlation_ids() {
        let e = entry("INFO", Some("coder"), Some("trace-xyz"));
        let line = render_entry(&e, false);
        assert!(line.contains("hello"));
        assert!(!line.contains("trace_id"));
    }

    #[test]
    fn render_verbose_includes_correlation_ids() {
        let e = entry("INFO", Some("coder"), Some("trace-xyz"));
        let line = render_entry(&e, true);
        assert!(line.contains("trace_id=trace-xyz"));
        assert!(line.contains("execution_id=exec-1"));
    }

    #[test]
    fn urlencode_keeps_unreserved_set() {
        assert_eq!(urlencode("abc-123_~.tag"), "abc-123_~.tag");
        assert_eq!(urlencode("a b"), "a%20b");
        assert_eq!(urlencode("k=v&x"), "k%3Dv%26x");
    }

    #[test]
    fn find_event_end_locates_blank_line() {
        let buf = b"data: {}\n\nrest";
        assert_eq!(find_event_end(buf), Some(8));
    }

    #[test]
    fn handle_sse_frame_ignores_heartbeat() {
        let opts = LogsOptions::default();
        // Should not panic or print anything (we only assert it doesn't
        // crash; printed output isn't captured here).
        handle_sse_frame(b": heartbeat", &opts);
    }
}
