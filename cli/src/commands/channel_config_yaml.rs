//! `channel config export / import / diff` — RFC 0050 Phase 1 PR 5 YAML follow-up.
//!
//! PR 5 landed the dependency-free core (`get` / `set` / `unset`). These three
//! verbs were split out because they read or write the declared YAML
//! (`config/channels.yaml`), which needs the YAML dependency PR 5 deliberately
//! omitted to stay under the branching size cap. They still ride PR 4's
//! store-canonical apply path over REST — no verb writes the store directly:
//!
//! - `export <name>` — `GET …/config`, then regenerate the channel's YAML block
//!   from the store, emitting only the **explicitly-overridden** knobs
//!   (`source == "channel"`) stamped `revision: store + 1` (the export-first
//!   foot-gun mitigation, RFC §"Foot-gun mitigation"). Inherited knobs are NOT
//!   frozen into explicit overrides.
//! - `import <file>` — parse a (possibly hand-edited) YAML doc and apply each
//!   channel's declared overrides through the same optimistic-concurrency PATCH
//!   path `set`/`unset` use. This is the **live CLI writer**, not the boot YAML
//!   loader: it is If-Match guarded, not revision-gated — the file's `revision:`
//!   is what the *boot* loader consumes once the file is committed to
//!   `config/channels.yaml`, so `import` does not gate on it.
//! - `diff <name>` — compare the declared YAML block against the effective
//!   store config and surface drift (RFC mechanic 4), per knob plus revision.

use colored::Colorize;
use serde_json::{Map, Value};
use serde_yaml_ng::Value as Yaml;

use crate::commands::channel::canonicalize_channel_id;
use crate::commands::channel_config::{
    apply_config_patch, config_rows, fetch_config, knob_type, ChannelConfigView, KnobType,
};
use crate::types::validate_path_param;

// ─── Parsed YAML model ────────────────────────────────────────────────────

/// One channel block parsed out of a declared YAML doc: its canonical id, the
/// optional `revision:` it was stamped at (absent in hand-authored blocks that
/// predate RFC 0050 — treated as "no declared revision"), and the sparse
/// `{knob: value}` override patch extracted from the recognised governance keys.
/// Non-knob keys (`name`, `description`, `members`, …) are ignored — only the
/// closed [`channel_config::CONFIG_KNOBS`](super::channel_config) set is lifted.
#[derive(Debug, PartialEq)]
pub(crate) struct ParsedChannel {
    pub(crate) id: String,
    pub(crate) revision: Option<i64>,
    pub(crate) patch: Map<String, Value>,
}

// ─── Pure helpers (testable without an HTTP server or a file) ──────────────

/// Coerce one declared YAML scalar to its knob's wire-typed JSON value, applying
/// the same closed-knob-set + strict-type discipline `set`'s `key=value` parser
/// uses — so an edited `config/channels.yaml` that mistypes a knob
/// (`floor_control: maybe`, `end_vote_window: "two"`) fails locally with the knob
/// named, before any round-trip, rather than round-tripping a 400 (or, worse,
/// silently coercing).
pub(crate) fn yaml_to_knob_json(key: &str, ty: KnobType, y: &Yaml) -> Result<Value, String> {
    match ty {
        KnobType::Bool => y
            .as_bool()
            .map(Value::Bool)
            .ok_or_else(|| format!("knob '{key}' expects a boolean (true|false)")),
        KnobType::Int => y
            .as_i64()
            .map(|n| Value::Number(n.into()))
            .ok_or_else(|| format!("knob '{key}' expects an integer")),
        // serde_yaml_ng parses an unquoted YAML scalar as Bool/Number where it can,
        // so `escalation_chair_id: true` arrives as Bool, not String. Accept only
        // a genuine string — a non-string here is a typo, not a chair id.
        KnobType::Str => y
            .as_str()
            .map(|s| Value::String(s.to_string()))
            .ok_or_else(|| format!("knob '{key}' expects a string")),
    }
}

/// Lift a single channel mapping into a [`ParsedChannel`]: require a string
/// `name` (canonicalized to the channel id), read an optional integer
/// `revision`, and fold every recognised governance knob into the sparse patch.
/// A YAML `null` for a knob is skipped (absent = inherit), matching the
/// sparse-merge contract — `unset`-to-inherit is an explicit verb, not a YAML
/// state.
pub(crate) fn parse_channel_block(block: &Yaml) -> Result<ParsedChannel, String> {
    let map = block
        .as_mapping()
        .ok_or_else(|| "channel entry is not a mapping".to_string())?;

    let name = map
        .get("name")
        .and_then(Yaml::as_str)
        .ok_or_else(|| "channel entry is missing a string `name`".to_string())?;
    let id = canonicalize_channel_id(name);

    // `revision:` may be absent (pre-RFC-0050 hand-authored block) → None. A
    // present-but-non-integer revision is a typo worth surfacing.
    let revision = match map.get("revision") {
        None => None,
        Some(Yaml::Null) => None,
        Some(r) => Some(
            r.as_i64()
                .ok_or_else(|| format!("channel '{name}': `revision` must be an integer"))?,
        ),
    };

    let mut patch = Map::new();
    for (k, v) in map {
        let Some(key) = k.as_str() else { continue };
        let Some(ty) = knob_type(key) else { continue };
        if v.is_null() {
            continue; // absent/null = inherit; not a sparse-patch entry
        }
        patch.insert(key.to_string(), yaml_to_knob_json(key, ty, v)?);
    }
    Ok(ParsedChannel {
        id,
        revision,
        patch,
    })
}

/// Parse a declared YAML doc (the `config/channels.yaml` shape: a top-level
/// `channels:` sequence of channel blocks) into the per-channel override set.
/// An absent / non-sequence / empty `channels:` is a usage error — there is
/// nothing to import or diff.
pub(crate) fn parse_channels_doc(text: &str) -> Result<Vec<ParsedChannel>, String> {
    let doc: Yaml = serde_yaml_ng::from_str(text).map_err(|e| format!("invalid YAML: {e}"))?;
    let channels = doc
        .as_mapping()
        .and_then(|m| m.get("channels"))
        .and_then(Yaml::as_sequence)
        .ok_or_else(|| "no `channels:` sequence found".to_string())?;
    if channels.is_empty() {
        return Err("`channels:` is empty — nothing to apply".to_string());
    }
    let parsed: Vec<ParsedChannel> = channels
        .iter()
        .map(parse_channel_block)
        .collect::<Result<_, _>>()?;
    // Reject a doc that declares the same channel twice (after canonicalization, so
    // a bare `planning` and a qualified `group:planning` collide). A channel holds
    // exactly one override set: `import` would otherwise PATCH it twice and `diff`
    // would silently compare only the first block, so a duplicate `name:` is a
    // hand-edit mistake to fail loudly on, before any write, rather than resolve
    // arbitrarily.
    let mut seen = std::collections::HashSet::with_capacity(parsed.len());
    for ch in &parsed {
        if !seen.insert(ch.id.as_str()) {
            return Err(format!("channel '{}' is declared more than once", ch.id));
        }
    }
    Ok(parsed)
}

/// Validate every parsed channel's id before `import` writes anything, so a
/// malformed `name:` in a later block aborts the whole run up front rather than
/// after the earlier blocks were already PATCHed. Pairs with the parse-time knob
/// coercion to make the "validated before the first write" contract hold for the
/// channel id too, not just the knob values.
pub(crate) fn validate_channel_ids(channels: &[ParsedChannel]) -> Result<(), String> {
    for ch in channels {
        validate_path_param(&ch.id, "channel id")?;
    }
    Ok(())
}

/// Regenerate a channel's YAML block from its effective config view, emitting
/// only the explicitly-overridden knobs (`source == "channel"`) under a
/// `channels:` list so the output is both a standalone `import` file and a
/// block that slots straight into `config/channels.yaml`. The block is stamped
/// `revision: view.revision + 1` — the export-first stamp that makes the
/// hand-edit loop (export → edit → import/commit) carry a fresh, higher revision
/// without the operator remembering to bump it. Knob order follows
/// [`config_rows`] (the registry order), keeping exports stable and reviewable.
pub(crate) fn render_export_doc(name: &str, view: &ChannelConfigView) -> Result<String, String> {
    let mut block = serde_yaml_ng::Mapping::new();
    block.insert(Yaml::from("name"), Yaml::from(name));
    block.insert(Yaml::from("revision"), Yaml::from(view.revision + 1));
    for (knob, field) in config_rows(view) {
        if field.source == "channel" {
            let y = serde_yaml_ng::to_value(&field.value)
                .map_err(|e| format!("knob '{knob}': cannot encode value as YAML: {e}"))?;
            block.insert(Yaml::from(knob), y);
        }
    }
    let mut doc = serde_yaml_ng::Mapping::new();
    doc.insert(
        Yaml::from("channels"),
        Yaml::Sequence(vec![Yaml::Mapping(block)]),
    );
    serde_yaml_ng::to_string(&Yaml::Mapping(doc)).map_err(|e| format!("cannot serialize YAML: {e}"))
}

/// Per-knob outcome of a `diff`: how the declared YAML value relates to the
/// effective store value.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum DiffStatus {
    /// Declared and matches the effective value.
    InSync,
    /// Declared but differs from the effective value — config-as-code drift.
    Drift,
    /// Not declared in the YAML — the channel inherits this knob.
    Inherited,
    /// Declared, but the effective value is null (the deferred-resolution
    /// `interaction_budget_tokens`, RFC Open item 4) so the two cannot be
    /// compared: neither a confirmed match nor confirmed drift.
    Indeterminate,
    /// Not declared in the YAML, yet the store carries an explicit per-channel
    /// override (`source == "channel"`). This IS drift: the boot reconcile
    /// (`ReconcileChannelConfig`) replaces the whole override blob with the
    /// declared set, so an override the file omits would be cleared on boot. A
    /// plain `Inherited` here would falsely reassure the operator that a live
    /// override is captured in config-as-code when committing the file reverts it.
    Undeclared,
}

impl DiffStatus {
    /// Stable machine tag for the `--json` `status` field. Decoupled from the
    /// Rust variant names so a rename cannot silently change the wire contract
    /// (`{:?}` would have). Pinned by `diff_status_machine_tags_are_stable`.
    fn tag(self) -> &'static str {
        match self {
            DiffStatus::InSync => "in_sync",
            DiffStatus::Drift => "drift",
            DiffStatus::Inherited => "inherited",
            DiffStatus::Indeterminate => "deferred",
            DiffStatus::Undeclared => "undeclared",
        }
    }
}

/// One row of a channel `diff`: the knob, its declared YAML value (if any), the
/// effective store value, and their relationship.
#[derive(Debug)]
pub(crate) struct DiffRow {
    pub(crate) knob: &'static str,
    pub(crate) declared: Option<Value>,
    pub(crate) effective: Value,
    pub(crate) status: DiffStatus,
}

/// Compare a declared override patch against an effective config view, one row
/// per knob in registry order. A knob the YAML declares is `InSync` when its
/// value equals the effective value and `Drift` when it differs; a knob the YAML
/// omits is `Inherited` only when the store has no override for it — if the store
/// carries an explicit `channel`-source override the file omits, that is
/// `Undeclared` drift (the boot reconcile would clear it), not `Inherited`. The
/// one exception is a declared knob whose effective value is null
/// (deferred-resolution budget) → `Indeterminate`, so a stale "drift" is never
/// reported for a value the server cannot resolve yet.
pub(crate) fn diff_rows(declared: &Map<String, Value>, view: &ChannelConfigView) -> Vec<DiffRow> {
    config_rows(view)
        .into_iter()
        .map(|(knob, field)| {
            let declared_val = declared.get(knob).cloned();
            let status = match &declared_val {
                None if field.source == "channel" => DiffStatus::Undeclared,
                None => DiffStatus::Inherited,
                Some(_) if field.value.is_null() => DiffStatus::Indeterminate,
                Some(d) if *d == field.value => DiffStatus::InSync,
                Some(_) => DiffStatus::Drift,
            };
            DiffRow {
                knob,
                declared: declared_val,
                effective: field.value.clone(),
                status,
            }
        })
        .collect()
}

/// Whether any row in a diff is drift — a declared knob whose value diverges
/// (`Drift`) or a store override the file omits (`Undeclared`, which the boot
/// reconcile would clear). Drives the non-zero "config-as-code diverges from the
/// store" render and a future exit-code hook.
pub(crate) fn has_drift(rows: &[DiffRow]) -> bool {
    rows.iter()
        .any(|r| matches!(r.status, DiffStatus::Drift | DiffStatus::Undeclared))
}

// ─── Rendering ─────────────────────────────────────────────────────────────

/// Render a JSON value for a diff cell: a JSON `null` (the deferred budget) reads
/// as `—`, a string drops its quotes, an absent declared value reads as `·`.
fn cell(v: Option<&Value>) -> String {
    match v {
        None => "·".to_string(),
        Some(Value::Null) => "\u{2014}".to_string(),
        Some(Value::String(s)) if s.is_empty() => "(none)".to_string(),
        Some(Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
    }
}

/// Render the diff table: declared vs effective per knob, with a status tag, plus
/// a revision line comparing the file's stamp to the store's current revision.
fn render_diff(id: &str, declared_rev: Option<i64>, view: &ChannelConfigView, rows: &[DiffRow]) {
    let file_rev = declared_rev
        .map(|r| r.to_string())
        .unwrap_or_else(|| "—".to_string());
    println!(
        "{}  {}",
        format!("#{id}").cyan(),
        format!(
            "file revision {file_rev} → store revision {}",
            view.revision
        )
        .dimmed()
    );
    let width = rows.iter().map(|r| r.knob.len()).max().unwrap_or(0);
    for row in rows {
        let painted = match row.status {
            DiffStatus::InSync => "in sync".green(),
            DiffStatus::Drift => "DRIFT".red().bold(),
            DiffStatus::Inherited => "inherited".dimmed(),
            DiffStatus::Indeterminate => "deferred".yellow(),
            DiffStatus::Undeclared => "DRIFT (store-only)".red().bold(),
        };
        println!(
            "  {knob:<width$}  declared {:<14} effective {:<14} {}",
            cell(row.declared.as_ref()),
            cell(Some(&row.effective)),
            painted,
            knob = row.knob,
        );
    }
}

// ─── Subcommand entry points ────────────────────────────────────────────────

/// `channel config export <name> [--out file]` — regenerate the channel's YAML
/// override block from the store, stamped `revision: store + 1`. Writes to
/// `--out` when given, else stdout (so `> patch.yaml` composes).
pub(crate) async fn cmd_config_export(
    client: &reqwest::Client,
    server: &str,
    name: &str,
    out: Option<&str>,
) -> Result<(), String> {
    let id = canonicalize_channel_id(name);
    validate_path_param(&id, "channel id")?;
    let resp = fetch_config(client, server, &id).await?;
    let doc = render_export_doc(name, &resp.view)?;
    match out {
        Some(path) => {
            std::fs::write(path, &doc).map_err(|e| format!("cannot write {path}: {e}"))?;
            println!(
                "{} {} ({})",
                "exported".green(),
                format!("#{id}").cyan(),
                format!("revision {} → {path}", resp.view.revision + 1).dimmed()
            );
        }
        None => print!("{doc}"),
    }
    Ok(())
}

/// `channel config import <file>` — apply each declared channel block's overrides
/// through the optimistic-concurrency PATCH path. Per channel: read the current
/// revision, then PATCH the declared knobs under its `If-Match`. A block with no
/// overridden knobs is a no-op (sparse PATCH) and is skipped with a note rather
/// than silently bumping the revision. The whole file is parsed and validated
/// before the first write, so a typo'd knob aborts before any channel is touched.
pub(crate) async fn cmd_config_import(
    client: &reqwest::Client,
    server: &str,
    file: &str,
    json_out: bool,
) -> Result<(), String> {
    let text = std::fs::read_to_string(file).map_err(|e| format!("cannot read {file}: {e}"))?;
    let channels = parse_channels_doc(&text)?;
    // Validate every channel id before the first write — the same before-any-write
    // discipline the knob coercion already gets at parse time, so a malformed
    // `name:` in a later block cannot leave earlier blocks half-applied.
    validate_channel_ids(&channels)?;
    let mut raws: Vec<String> = Vec::new();
    // Track the channels already PATCHed so a mid-run failure can tell the operator
    // exactly which writes landed. `import` is sparse and per-channel — there is no
    // cross-channel transaction over REST — so it is best-effort, not atomic: a 409
    // or wire error on a later block leaves the earlier blocks applied.
    let mut applied: Vec<String> = Vec::new();
    for ch in &channels {
        if ch.patch.is_empty() {
            if !json_out {
                println!(
                    "{} {} (no overrides declared)",
                    "skipped".dimmed(),
                    format!("#{}", ch.id).cyan()
                );
            }
            continue;
        }
        // Read the current revision for the If-Match guard, then apply. A 409
        // (a concurrent edit moved the revision) aborts the import with the
        // already-applied channels left in place — the channel id is named, and the
        // already-applied set is surfaced, so the operator can re-run the remainder
        // after re-reading rather than guessing what landed.
        let current = fetch_config(client, server, &ch.id).await?;
        let resp = apply_config_patch(client, server, &ch.id, &ch.patch, current.view.revision)
            .await
            .map_err(|e| {
                let mut msg = format!("{}: {e}", ch.id);
                if !applied.is_empty() {
                    msg.push_str(&format!(
                        "\n  note: {} channel(s) already applied and NOT rolled back: {}",
                        applied.len(),
                        applied.join(", ")
                    ));
                }
                msg
            })?;
        applied.push(ch.id.clone());
        if json_out {
            raws.push(resp.raw.trim_end().to_string());
        } else {
            println!(
                "{} {} ({})",
                "applied".green(),
                format!("#{}", ch.id).cyan(),
                format!(
                    "{} knob(s) → revision {}",
                    ch.patch.len(),
                    resp.view.revision
                )
                .dimmed()
            );
        }
    }
    if json_out {
        println!("[{}]", raws.join(","));
    }
    Ok(())
}

/// `channel config diff <name> [--file path]` — compare the channel's declared
/// YAML block (default `config/channels.yaml`) against its effective store
/// config and surface per-knob drift plus a revision comparison.
pub(crate) async fn cmd_config_diff(
    client: &reqwest::Client,
    server: &str,
    name: &str,
    file: &str,
    json_out: bool,
) -> Result<(), String> {
    let id = canonicalize_channel_id(name);
    validate_path_param(&id, "channel id")?;
    let text = std::fs::read_to_string(file).map_err(|e| format!("cannot read {file}: {e}"))?;
    let declared = parse_channels_doc(&text)?
        .into_iter()
        .find(|c| c.id == id)
        .ok_or_else(|| format!("channel '{id}' is not declared in {file}"))?;
    let resp = fetch_config(client, server, &id).await?;
    let rows = diff_rows(&declared.patch, &resp.view);
    if json_out {
        let cells: Vec<Value> = rows
            .iter()
            .map(|r| {
                serde_json::json!({
                    "knob": r.knob,
                    "declared": r.declared,
                    "effective": r.effective,
                    "status": r.status.tag(),
                })
            })
            .collect();
        let body = serde_json::json!({
            "channel_id": id,
            "file_revision": declared.revision,
            "store_revision": resp.view.revision,
            "drift": has_drift(&rows),
            "knobs": cells,
        });
        println!("{body}");
    } else {
        render_diff(&id, declared.revision, &resp.view, &rows);
    }
    Ok(())
}

#[cfg(test)]
#[path = "channel_config_yaml_tests.rs"]
mod tests;
