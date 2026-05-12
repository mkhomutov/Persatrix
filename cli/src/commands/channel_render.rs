//! Display helpers for `channel watch` / `channel history` output.
//!
//! Split from [`super::channel`] so the parent module stays under the
//! project's 500-line review cap (same rationale as
//! [`super::channel_tests`]). Pure helpers + the best-effort agent
//! display-name fetcher live here; the subcommand entry points that
//! call into them stay in `channel.rs`.

use std::collections::HashMap;

use colored::{Color, Colorize};

use crate::commands::channel_types::ChannelMessage;
use crate::types::AgentResponse;

/// Extract the `HH:MM:SS` segment of an RFC3339 timestamp for compact
/// inline display (e.g. `[10:23:45] Iron Fox: …`).
///
/// The server emits UTC RFC3339Nano (see
/// `internal/server/channel_handlers.go::Timestamp = time.Now().UTC()`),
/// which always shapes as `YYYY-MM-DDTHH:MM:SS[.fff]Z`. Slicing 8 chars
/// after the `T` is robust to optional sub-second precision and any
/// trailing `Z`/offset. Falls back to the raw string when the input is
/// malformed (no `T`, or the post-`T` segment is shorter than 8 chars)
/// so a future format change degrades to "show the whole thing" rather
/// than panic. UTC is preserved end-to-end — adding chrono just to
/// localize a 30-minute demo timestamp is not worth the dependency
/// weight; the format is unambiguous because `cmd_channel_watch`
/// announces "(times in UTC)" in its banner.
pub(crate) fn format_short_time(iso: &str) -> String {
    if let Some(t_pos) = iso.find('T') {
        let after_t = &iso[t_pos + 1..];
        if after_t.len() >= 8 {
            return after_t[..8].to_string();
        }
    }
    iso.to_string()
}

/// Resolve a sender id to its human display name (e.g. `iron-fox` →
/// `Iron Fox`). Falls back to the raw id when unknown — covers human
/// participants like `alex` who join a channel via `channel join` but
/// have no entry in `agents.yaml`. Pure helper so the lookup-and-fallback
/// rule is testable without an HTTP server.
pub(crate) fn display_name_for(sender_id: &str, names: &HashMap<String, String>) -> String {
    names
        .get(sender_id)
        .cloned()
        .unwrap_or_else(|| sender_id.to_string())
}

/// Pick a stable, distinct ANSI color for `sender_id` so each speaker
/// reads as a visual chip across `channel watch` / `channel history`.
///
/// Determinism matters: the same id must hash to the same color across
/// every line in a session (and across sessions, so a returning user
/// recognises "Iron Fox is bright cyan"). FNV-1a is a 14-line pure
/// function with good small-input distribution and zero deps — overkill
/// alternatives like SipHash via `DefaultHasher` carry process-randomized
/// state in some std versions, which would make the same id pick
/// different colors on each run.
///
/// Palette excludes `Black`, `White`, and `BrightBlack` (collide with
/// default terminal foregrounds and the dimmed-time slot), and both
/// `Blue` shades — they have low contrast on the near-black backgrounds
/// PowerShell / Windows Terminal / iTerm default to, and an empirical
/// pass against the v0.3.0 demo set landed two of four speakers on
/// adjacent blue shades. Five base + five bright hues give each speaker
/// a clearly distinct chip without that hue collision.
pub(crate) fn color_for(sender_id: &str) -> Color {
    const PALETTE: &[Color] = &[
        Color::Cyan,
        Color::Magenta,
        Color::Yellow,
        Color::Green,
        Color::Red,
        Color::BrightCyan,
        Color::BrightMagenta,
        Color::BrightYellow,
        Color::BrightGreen,
        Color::BrightRed,
    ];
    // FNV-1a 64-bit. Constants per the canonical reference; both fit in
    // a u64 and the wrapping_mul keeps the loop alloc-free.
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in sender_id.bytes() {
        h ^= u64::from(b);
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    PALETTE[(h as usize) % PALETTE.len()]
}

/// Render one channel message as a single human-readable line for
/// `channel watch` / `channel history` (parity with the `cmd_chat`
/// REPL). Format: `[HH:MM:SS] Display Name: <content>` with the whole
/// `[time] Name:` prefix in the speaker's deterministic color, name
/// also bold. Returning `String` (not `ColoredString`) lets the caller
/// pass it straight to `println!` without an extra format step; the
/// `colored` crate strips ANSI when stdout is piped, so plain-text
/// pipelines still receive plain text.
pub(crate) fn format_message_line(msg: &ChannelMessage, names: &HashMap<String, String>) -> String {
    let color = color_for(&msg.sender_id);
    let time = format!("[{}]", format_short_time(&msg.timestamp)).color(color);
    let name = format!("{}:", display_name_for(&msg.sender_id, names))
        .color(color)
        .bold();
    format!("{time} {name} {}", msg.content)
}

/// Best-effort fetch of `id → display_name` for every registered agent
/// via `GET /api/v1/agents`. Used by `channel history` / `channel watch`
/// to render `Iron Fox:` instead of `iron-fox:` (parity with `cmd_chat`).
///
/// Failures (connection error, non-2xx, malformed JSON, missing `name`
/// fields on a v0.1 server) silently degrade to an empty map — the
/// caller then falls back to the raw `sender_id`, preserving the
/// previous output rather than aborting a watch loop on a transient
/// hiccup. This is purely a display-cosmetic enrichment; correctness
/// of message delivery does not depend on it.
pub(crate) async fn fetch_agent_display_names(
    client: &reqwest::Client,
    server: &str,
) -> HashMap<String, String> {
    let resp = match client.get(format!("{server}/api/v1/agents")).send().await {
        Ok(r) if r.status().is_success() => r,
        _ => return HashMap::new(),
    };
    let agents: Vec<AgentResponse> = match resp.json().await {
        Ok(a) => a,
        Err(_) => return HashMap::new(),
    };
    agents
        .into_iter()
        .filter_map(|a| a.name.filter(|n| !n.is_empty()).map(|name| (a.id, name)))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    // ─── format_short_time ─────────────────────────────────────────

    #[test]
    fn format_short_time_extracts_hms_from_rfc3339() {
        // Server emits RFC3339Nano UTC; we render only HH:MM:SS for the
        // inline `[10:23:45] Iron Fox: …` watch/history layout.
        assert_eq!(format_short_time("2026-05-09T10:23:45Z"), "10:23:45");
    }

    #[test]
    fn format_short_time_handles_subsecond_precision() {
        // Go's default time.Time JSON encoding includes nanoseconds when
        // non-zero. The slice must stop at second precision regardless.
        assert_eq!(
            format_short_time("2026-05-09T10:23:45.123456789Z"),
            "10:23:45"
        );
    }

    #[test]
    fn format_short_time_falls_back_on_malformed_input() {
        // Defensive: a future server format change must not panic the
        // watch loop. Empty / no-T inputs round-trip unchanged.
        assert_eq!(format_short_time(""), "");
        assert_eq!(format_short_time("not-a-timestamp"), "not-a-timestamp");
        assert_eq!(format_short_time("2026-05-09T10:23"), "2026-05-09T10:23");
    }

    // ─── display_name_for ──────────────────────────────────────────

    #[test]
    fn display_name_for_returns_mapped_name() {
        let mut names = HashMap::new();
        names.insert("iron-fox".to_string(), "Iron Fox".to_string());
        assert_eq!(display_name_for("iron-fox", &names), "Iron Fox");
    }

    #[test]
    fn display_name_for_falls_back_to_id_when_unknown() {
        // Human participants like `alex` join via `channel join` and
        // have no entry in `agents.yaml` — the raw id is the right
        // fallback.
        let names = HashMap::new();
        assert_eq!(display_name_for("alex", &names), "alex");
    }

    // ─── color_for ─────────────────────────────────────────────────

    #[test]
    fn color_for_is_deterministic_per_id() {
        // Stable mapping is the contract: a returning user must see
        // "Iron Fox is bright cyan" across sessions, so the hash +
        // palette index pair must be process-independent. Same id →
        // same color, every call.
        assert_eq!(color_for("iron-fox"), color_for("iron-fox"));
        assert_eq!(color_for("nova-sparrow"), color_for("nova-sparrow"));
        assert_eq!(color_for("alex"), color_for("alex"));
    }

    #[test]
    fn color_for_distinguishes_demo_personas() {
        // Soft contract for the v0.3.0 demo set: the four typical
        // speakers (three personas + one human) should not all collapse
        // onto the same palette slot. With a 10-color palette and
        // FNV-1a, a 4-element collision is statistically unlikely; this
        // test guards against an accidental palette shrink that
        // defeats the per-speaker visual chip.
        let colors = [
            color_for("iron-fox"),
            color_for("nova-sparrow"),
            color_for("ember-owl"),
            color_for("alex"),
        ];
        // `colored::Color` doesn't implement Hash, so dedupe via Debug
        // repr — sufficient because Color's variants debug-print
        // uniquely.
        let unique: std::collections::HashSet<String> =
            colors.iter().map(|c| format!("{c:?}")).collect();
        assert!(
            unique.len() >= 3,
            "expected at least 3 distinct colors across the demo speakers, got {colors:?}"
        );
    }

    #[test]
    fn color_for_handles_empty_id() {
        // Defensive: an empty sender id (server contract violation)
        // must not panic the watch loop. FNV-1a's offset-basis seed
        // yields a valid palette index even for the zero-byte input.
        let _ = color_for("");
    }

    // ─── format_message_line ───────────────────────────────────────

    #[test]
    fn format_message_line_includes_time_name_and_content() {
        // Smoke-test the full render path: time slice, name resolution,
        // and verbatim content composition. ANSI codes from `colored`
        // are stripped via `colored::control::set_override(false)` so
        // the assertion stays tied to the visible text rather than the
        // current escape-sequence layout.
        colored::control::set_override(false);
        let mut names = HashMap::new();
        names.insert("iron-fox".to_string(), "Iron Fox".to_string());
        let msg = ChannelMessage {
            id: "msg-1".into(),
            channel_id: "group:planning".into(),
            sender_id: "iron-fox".into(),
            content: "production is on fire".into(),
            timestamp: "2026-05-09T10:23:45Z".into(),
            thread_id: String::new(),
            mentions: Vec::new(),
            metadata: None,
        };
        let line = format_message_line(&msg, &names);
        colored::control::unset_override();
        assert_eq!(line, "[10:23:45] Iron Fox: production is on fire");
    }
}
