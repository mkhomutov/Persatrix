//! Human-render helpers for the `channel config` surface — extracted from
//! [`channel_config`](super::channel_config) when RFC 0052 PR 2 pushed that file
//! to the 500-line cap (the "must split" the PR plan called for). Pure presentation:
//! the effective-config block and the per-knob value formatter, driven off the
//! `pub(crate)` registry ([`config_rows`](super::channel_config::config_rows)) the
//! parent still owns. The `--json` passthrough does not come through here.

use colored::Colorize;
use serde_json::Value;

use super::channel_config::{config_rows, ChannelConfigView};

/// Render one knob's effective value for the human view. A JSON `null` reads as
/// `—` so it is not mistaken for the literal string "null"; a JSON string drops
/// its quotes.
///
/// An empty string renders as `(none)` rather than a blank cell. The one string
/// knob is `escalation_chair_id`, whose empty value means "no chair" — either an
/// explicit `[channel]` disable (the empty-string sentinel) or an inherited
/// `[default]` with no chair configured. A blank cell would read as a render
/// glitch / missing field; `(none)` names the state, and the provenance tag still
/// distinguishes the two cases. Kept distinct from `—` (a JSON null) so the two
/// are not conflated.
///
/// A list knob (`autonomous.agenda`) joins its items into a human row rather than
/// raw JSON (`["a","b"]`); an empty list reads `(none)` like the empty string.
pub(crate) fn render_value(value: &Value) -> String {
    match value {
        Value::Null => "\u{2014}".to_string(),
        Value::String(s) if s.is_empty() => "(none)".to_string(),
        Value::String(s) => s.clone(),
        Value::Array(a) if a.is_empty() => "(none)".to_string(),
        Value::Array(a) => a.iter().map(render_value).collect::<Vec<_>>().join(", "),
        other => other.to_string(),
    }
}

/// The one-line RFC 0052 §E convening-count / aggregate-bound readout for the
/// human `get` render, or `None` when the channel is not armed (a non-autonomous
/// channel has no convening story — suppress it rather than print a misleading
/// "0 used"). Drives off the runtime block's `convenings_remaining`: `Some` ⇒ a
/// positive `max_convenings` bound (report the count and what remains against it),
/// `None` ⇒ unbounded (report the count alone). The server owns the
/// null/clamp derivation; this only renders it.
pub(crate) fn autonomous_runtime_summary(view: &ChannelConfigView) -> Option<String> {
    if view.autonomous.enabled.value != Value::Bool(true) {
        return None;
    }
    let count = view.autonomous_runtime.convening_count;
    Some(match view.autonomous_runtime.convenings_remaining {
        Some(remaining) => format!("{count} used, {remaining} remaining"),
        None => format!("{count} used (no aggregate bound)"),
    })
}

/// Render the effective-config block: a header carrying the channel id and
/// current revision, then one aligned row per knob with its value and a
/// `[channel]` / `[default]` provenance tag. For an armed autonomous channel a
/// trailing `convenings` row carries the RFC 0052 §E runtime readout — tagged
/// `(runtime)` rather than `[channel]`/`[default]` so it never reads as a config
/// knob missing its provenance.
pub(crate) fn render_config_view(id: &str, view: &ChannelConfigView) {
    println!(
        "{}  {}",
        format!("#{id}").cyan(),
        format!("revision {}", view.revision).dimmed()
    );
    let rows = config_rows(view);
    let width = rows.iter().map(|(k, _)| k.len()).max().unwrap_or(0);
    for (key, field) in rows {
        println!(
            "  {key:<width$}  {}  {}",
            render_value(&field.value),
            format!("[{}]", field.source).dimmed(),
        );
    }
    if let Some(summary) = autonomous_runtime_summary(view) {
        println!(
            "  {:<width$}  {summary}  {}",
            "convenings",
            "(runtime)".dimmed(),
        );
    }
}

#[cfg(test)]
#[path = "channel_config_render_tests.rs"]
mod tests;
