//! `channel config` verb group — RFC 0050 Phase 1 PR 5 (operator surface).
//!
//! Thin-client pattern (per `.github/instructions/rust-cli.instructions.md`):
//! every subcommand marshals args into the store-canonical apply path exposed by
//! PR 4 over REST (`GET`/`PATCH /api/v1/channels/{id}/config`) and prints the
//! response. Wire shapes mirror `internal/server/channel_types.go`.
//!
//! This PR lands the dependency-free core — `get` / `set` / `unset` — which ride
//! directly on the PR-4 endpoints. The remaining plan verbs (`export` / `import`
//! / `diff`) all need to read the declared YAML (`config/channels.yaml`), which
//! requires re-adding a YAML dependency; they are split into the YAML follow-up
//! so this PR stays under the branching size cap with no new dependency.
//!
//! Both writers (`set`, `unset`) are read-then-write: a `GET` reads the current
//! revision, the `PATCH` carries it back as the `If-Match` guard. The server merges
//! the sparse patch onto the channel's overrides (`null` unsets a knob) and returns
//! the bumped revision + new effective config in one round-trip. The nested
//! `reasoning.*` surface lives in `channel_config_reasoning`.

use reqwest::StatusCode;
use serde::Deserialize;
use serde_json::Value;

use crate::commands::channel::canonicalize_channel_id;
use crate::commands::channel_config_autonomous::{
    self as autonomous, AutonomousConfigView, AutonomousRuntimeView,
};
use crate::commands::channel_config_reasoning::{self as reasoning, ReasoningConfigView};
use crate::commands::channel_config_render::render_config_view;
use crate::types::{api_error_message, validate_path_param};

// ─── Wire DTOs (mirror internal/server/channel_types.go) ─────────────────

/// One governance knob's resolved cell in the RFC 0050 `GET/PATCH …/config`
/// payload, mirroring the Go `configFieldResponse`: the effective `value` plus
/// its `source` provenance (`"channel"` = explicit per-channel override,
/// `"default"` = inherited fleet/group default).
///
/// `value` is `serde_json::Value` rather than a typed field because the knob set
/// is heterogeneous (bool / int / string), and keeping it dynamic also lets the
/// CLI tolerate a JSON `null` defensively — a single dynamic cell renders every
/// knob uniformly without a per-knob struct.
/// `Default` backs the `#[serde(default)]` cells (knobs a pre-v0.3.13
/// orchestrator's payload omits): a null value renders as `(none)` through the
/// same defensive path an explicit JSON `null` already takes.
#[derive(Deserialize, Default)]
pub(crate) struct ConfigField {
    pub(crate) value: Value,
    pub(crate) source: String,
}

/// JSON shape returned by `GET` and `PATCH /api/v1/channels/{id}/config`,
/// mirroring the Go `channelConfigResponse`: the channel's optimistic-concurrency
/// `revision` plus each governed knob's effective value + provenance. Field names
/// match the Go JSON tags exactly so each knob deserializes into the right cell
/// for the human render; the `revision` is what a follow-up `set`/`unset` echoes
/// back in the `If-Match` header. (`--json` does not serialize this struct — it
/// passes the server body through verbatim, see [`passthrough_json`].)
#[derive(Deserialize)]
pub(crate) struct ChannelConfigView {
    pub(crate) revision: i64,
    pub(crate) floor_control: ConfigField,
    pub(crate) salience_max_channel_members: ConfigField,
    /// ISSUE-0114 (v0.3.13): the per-channel Layer 0 cascade-depth cap. Absent
    /// from a pre-v0.3.13 orchestrator's payload, so it defaults rather than
    /// failing the decode against an older server.
    #[serde(default)]
    pub(crate) max_cascade_depth: ConfigField,
    pub(crate) max_replies_per_participant_per_interaction: ConfigField,
    pub(crate) end_vote_threshold: ConfigField,
    pub(crate) end_vote_window: ConfigField,
    pub(crate) escalation_chair_id: ConfigField,
    pub(crate) interaction_idle_timeout_seconds: ConfigField,
    pub(crate) interaction_budget_tokens: ConfigField,
    /// RFC 0051 (v0.3.10): the first NESTED knob — a `reasoning` block whose four
    /// sub-knobs each carry their own `{value, source}` cell (so an override of
    /// `mode` alone reports model/depth/revise as inherited). Mirrors the Go
    /// `reasoningConfigResponse`; rendered as dotted `reasoning.<sub>` rows. The
    /// type + its sub-knob accessor live in [`channel_config_reasoning`](reasoning).
    pub(crate) reasoning: ReasoningConfigView,
    /// RFC 0052 (v0.3.11): the SECOND nested knob — an `autonomous` block (six
    /// sub-knobs, dotted `autonomous.<sub>` rows) carrying the first LIST-valued
    /// sub-knob (`agenda`). Type + accessor in [`channel_config_autonomous`](autonomous).
    pub(crate) autonomous: AutonomousConfigView,
    /// RFC 0052 §E (v0.3.11 PR 7b): the LIVE convening-count / aggregate-bound
    /// readout — runtime counters (not config `{value, source}` cells), rendered
    /// after the knob rows for an armed channel. `#[serde(default)]` so a payload
    /// without the block (older server, stub fixture) still decodes.
    #[serde(default)]
    pub(crate) autonomous_runtime: AutonomousRuntimeView,
}

// ─── Knob registry ──────────────────────────────────────────────────────

/// The JSON type a governance knob carries on the wire. The knob set is
/// heterogeneous, so `set`'s `key=value` parser coerces the value half per type
/// (and rejects a wrong-typed value locally rather than round-tripping a 400).
/// `Enum` (the RFC 0051 reasoning knobs) is a string knob restricted to a closed
/// value set; it rides the wire as a plain JSON string, so the lockstep type guard
/// treats it as the `string` wire class.
#[derive(Copy, Clone, PartialEq, Eq, Debug)]
pub(crate) enum KnobType {
    Bool,
    Int,
    Str,
    /// A string knob restricted to a closed value set (see [`reasoning`]).
    Enum(&'static [&'static str]),
    /// A string LIST knob (RFC 0052 `autonomous.agenda`) — rides the wire as a JSON
    /// array; the CLI splits a comma-delimited value (see [`autonomous::coerce_list`]).
    List,
}

/// The closed set of editable FLAT governance knobs, paired with their wire type.
/// The full editable set is this ∪ the nested [`reasoning::KNOBS`] — see
/// [`editable_knobs`]. Declaration order is also the render order for `config get`.
pub(crate) const CONFIG_KNOBS: &[(&str, KnobType)] = &[
    ("floor_control", KnobType::Bool),
    ("salience_max_channel_members", KnobType::Int),
    ("max_cascade_depth", KnobType::Int),
    ("max_replies_per_participant_per_interaction", KnobType::Int),
    ("end_vote_threshold", KnobType::Int),
    ("end_vote_window", KnobType::Int),
    ("escalation_chair_id", KnobType::Str),
    ("interaction_idle_timeout_seconds", KnobType::Int),
    ("interaction_budget_tokens", KnobType::Int),
];

/// Every editable knob in render order: the flat [`CONFIG_KNOBS`] followed by the
/// nested dotted [`reasoning::KNOBS`] then [`autonomous::KNOBS`]. This MUST match the
/// server's editable-knob set exactly — the flat `mergeConfigPatch` cases ∪ the
/// nested `mergeReasoningPatch` ∪ `mergeAutonomousPatch` sub-knobs — a drift either
/// way is pinned by `cli_knob_set_matches_server_merge_switch`.
pub(crate) fn editable_knobs() -> impl Iterator<Item = &'static (&'static str, KnobType)> {
    CONFIG_KNOBS
        .iter()
        .chain(reasoning::KNOBS.iter())
        .chain(autonomous::KNOBS.iter())
}

/// The wire type for a knob, or `None` if `key` is not an editable knob.
/// `pub(crate)` so the YAML follow-up verbs (`channel_config_yaml.rs`) coerce a
/// declared `config/channels.yaml` value against the same closed knob set.
pub(crate) fn knob_type(key: &str) -> Option<KnobType> {
    editable_knobs().find(|(k, _)| *k == key).map(|(_, t)| *t)
}

/// A comma-joined list of every editable knob, for the "unknown knob" diagnosis.
fn knob_vocabulary() -> String {
    editable_knobs()
        .map(|(k, _)| *k)
        .collect::<Vec<_>>()
        .join(", ")
}

// ─── Pure helpers (testable without an HTTP server) ─────────────────────

/// Parse one `key=value` assignment into a `(knob, json-value)` pair, coercing
/// the value to the knob's wire type. An unknown knob, a missing `=`, or a
/// wrong-typed value (`floor_control=maybe`, `end_vote_window=two`) is rejected
/// here with a message that names the knob — the server's closed-knob-set and
/// strict-type checks mirrored client-side so a typo fails before the round-trip.
///
/// An empty value is rejected for the numeric/boolean knobs (there is no empty
/// int or bool, so `end_vote_window=` is a typo — steered to `channel config
/// unset`). For the string knob it is KEPT: `escalation_chair_id=` is the
/// explicit empty-string override, which the server treats as "escalation
/// disabled" — a state DISTINCT from `unset` (clear back to inherit, which may
/// resolve to a non-empty inherited chair). See `ChannelConfigOverrides`
/// (internal/channels/channel_config_store.go): "An explicit empty string
/// disables escalation; nil inherits."
pub(crate) fn parse_set_assignment(spec: &str) -> Result<(String, Value), String> {
    let (key, raw) = spec.split_once('=').ok_or_else(|| {
        format!("invalid assignment '{spec}'; expected key=value (e.g. floor_control=true)")
    })?;
    let key = key.trim();
    let raw = raw.trim();
    let ty = knob_type(key).ok_or_else(|| {
        format!(
            "unknown config knob '{key}'; expected one of: {}",
            knob_vocabulary()
        )
    })?;
    // A string knob's empty value is a legitimate override (the empty-string
    // "disable" sentinel — see the doc above), so only the numeric/boolean knobs
    // reject empty and steer to `unset`. An empty enum value falls through to the
    // value-set check below, which names the accepted values.
    if raw.is_empty() && matches!(ty, KnobType::Int | KnobType::Bool) {
        return Err(format!(
            "knob '{key}' has an empty value; supply a {} value, or use \
             `channel config unset {key}` to clear it back to inherit",
            type_label(ty)
        ));
    }
    let value = match ty {
        KnobType::Bool => match raw {
            "true" => Value::Bool(true),
            "false" => Value::Bool(false),
            other => {
                return Err(format!(
                    "knob '{key}' expects a boolean (true|false), got '{other}'"
                ))
            }
        },
        KnobType::Int => {
            let n: i64 = raw
                .parse()
                .map_err(|_| format!("knob '{key}' expects an integer, got '{raw}'"))?;
            Value::Number(n.into())
        }
        KnobType::Str => Value::String(raw.to_string()),
        // A closed string enum: the value-set check lives in `reasoning` (it backs
        // the dotted reasoning knobs), failing locally rather than round-tripping a 400.
        KnobType::Enum(allowed) => reasoning::coerce_enum(key, allowed, raw)?,
        // A string list (RFC 0052 `autonomous.agenda`): comma-split into a JSON array
        // client-side (infallible — empty maps to an explicit empty agenda).
        KnobType::List => autonomous::coerce_list(raw),
    };
    Ok((key.to_string(), value))
}

/// Human-readable name for a knob's wire type, used in the empty-value steer
/// (only reached for the numeric/boolean knobs — see [`parse_set_assignment`]).
fn type_label(ty: KnobType) -> &'static str {
    match ty {
        KnobType::Bool => "boolean",
        KnobType::Int => "integer",
        KnobType::Str => "string",
        KnobType::Enum(_) => "enum",
        KnobType::List => "list",
    }
}

/// Fold a list of `key=value` specs into the sparse PATCH body.
///
/// Rejects an empty spec list (a no-op write is a usage error) and a knob set
/// more than once in one invocation (`floor_control=true floor_control=false` —
/// ambiguous; reject rather than silently let last-wins decide). Each spec is
/// validated by [`parse_set_assignment`]; dotted reasoning keys are nested by
/// [`reasoning::nest_dotted`] after collection so duplicate detection runs on the
/// flat key.
pub(crate) fn build_set_patch(specs: &[String]) -> Result<serde_json::Map<String, Value>, String> {
    if specs.is_empty() {
        return Err("at least one key=value assignment is required".into());
    }
    let mut flat = serde_json::Map::new();
    for spec in specs {
        let (key, value) = parse_set_assignment(spec)?;
        if flat.insert(key.clone(), value).is_some() {
            return Err(format!("knob '{key}' set more than once in one command"));
        }
    }
    Ok(reasoning::nest_dotted(flat))
}

/// Fold a list of knob names into the PATCH body that unsets each back to
/// inherit — every key maps to JSON `null`, the server's unset sentinel. A dotted
/// reasoning key nests its null ([`nest_dotted`]), so `unset reasoning.mode`
/// clears just that sub-knob (the server's per-sub-knob null branch) rather than
/// the whole block.
///
/// Rejects an empty list, an unknown knob (named, with the vocabulary), and a
/// knob repeated in one invocation.
pub(crate) fn build_unset_patch(keys: &[String]) -> Result<serde_json::Map<String, Value>, String> {
    if keys.is_empty() {
        return Err("at least one knob name to unset is required".into());
    }
    let mut flat = serde_json::Map::new();
    for key in keys {
        let key = key.trim();
        if knob_type(key).is_none() {
            return Err(format!(
                "unknown config knob '{key}'; expected one of: {}",
                knob_vocabulary()
            ));
        }
        if flat.insert(key.to_string(), Value::Null).is_some() {
            return Err(format!("knob '{key}' unset more than once in one command"));
        }
    }
    Ok(reasoning::nest_dotted(flat))
}

/// The knob cells of a config view in render order, pairing each wire label with
/// its resolved field. Driven off [`CONFIG_KNOBS`] so the render order and the
/// editable-knob set stay in lockstep (a knob added to the registry shows up in
/// `get` automatically), matching against the typed view's fields by name.
/// `pub(crate)` so the YAML follow-up verbs walk the same knob→(value, source)
/// cells (`export` emits the `"channel"` subset; `diff` compares each cell).
pub(crate) fn config_rows(view: &ChannelConfigView) -> Vec<(&'static str, &ConfigField)> {
    editable_knobs()
        .map(|(key, _)| {
            let field = match *key {
                "floor_control" => &view.floor_control,
                "salience_max_channel_members" => &view.salience_max_channel_members,
                "max_cascade_depth" => &view.max_cascade_depth,
                "max_replies_per_participant_per_interaction" => {
                    &view.max_replies_per_participant_per_interaction
                }
                "end_vote_threshold" => &view.end_vote_threshold,
                "end_vote_window" => &view.end_vote_window,
                "escalation_chair_id" => &view.escalation_chair_id,
                "interaction_idle_timeout_seconds" => &view.interaction_idle_timeout_seconds,
                "interaction_budget_tokens" => &view.interaction_budget_tokens,
                // The nested reasoning + autonomous blocks resolve their own dotted
                // rows; the editable set is pinned to the view fields by the lockstep
                // test, so a key matching neither is an `unreachable!` invariant break.
                other => view
                    .reasoning
                    .field(other)
                    .or_else(|| view.autonomous.field(other))
                    .unwrap_or_else(|| unreachable!("knob {other} has no view field")),
            };
            (*key, field)
        })
        .collect()
}

// ─── HTTP helpers ───────────────────────────────────────────────────────

/// A `…/config` response: the typed [`ChannelConfigView`] (for the human render
/// and the `revision` the next write needs) plus the server's raw response body,
/// kept verbatim for the `--json` passthrough (see [`passthrough_json`]) so the
/// CLI does not lose a field the server reports but the typed view does not model.
pub(crate) struct ConfigResponse {
    pub(crate) view: ChannelConfigView,
    pub(crate) raw: String,
}

/// Decode a successful `…/config` body into a [`ConfigResponse`], keeping the
/// raw bytes alongside the parsed view. (We read the body as text and parse it
/// ourselves rather than `resp.json()` so the same bytes back the `--json`
/// passthrough.)
fn decode_config_response(raw: String) -> Result<ConfigResponse, String> {
    let view = serde_json::from_str(&raw).map_err(|e| format!("invalid response: {e}"))?;
    Ok(ConfigResponse { view, raw })
}

/// `GET /api/v1/channels/{id}/config` → [`ConfigResponse`]. The caller is
/// responsible for validating `id` first. A non-2xx (403 toggle-off, 404 unknown
/// channel, 503 unwired) surfaces via [`api_error_message`]. `pub(crate)` so the
/// YAML follow-up verbs read effective config + revision through the same path.
pub(crate) async fn fetch_config(
    client: &reqwest::Client,
    server: &str,
    id: &str,
) -> Result<ConfigResponse, String> {
    let resp = client
        .get(format!("{server}/api/v1/channels/{id}/config"))
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }
    let raw = resp
        .text()
        .await
        .map_err(|e| format!("invalid response: {e}"))?;
    decode_config_response(raw)
}

/// Augment a 409-conflict API message with the operator's recovery step. Pulled
/// out of [`apply_config_patch`] so this user-facing copy has a regression guard
/// (the HTTP path itself is only live-verified) — a refactor that drops the
/// re-read steer fails [`conflict_hint_points_at_reread`] rather than silently
/// shipping a dead-end error.
fn conflict_hint(msg: &str) -> String {
    format!(
        "{msg}\nthe config changed since you last read it; re-run \
         `channel config get` and retry"
    )
}

/// `PATCH /api/v1/channels/{id}/config` with the sparse `patch` body under an
/// `If-Match: revision` optimistic-concurrency guard. Returns the post-apply
/// view (bumped revision + new effective config). A stale revision surfaces as a
/// 409 with a hint to re-read; other non-2xx pass through [`api_error_message`].
///
/// `pub(crate)` so the YAML follow-up's `import` applies each declared channel
/// block through the same optimistic-concurrency apply path.
pub(crate) async fn apply_config_patch(
    client: &reqwest::Client,
    server: &str,
    id: &str,
    patch: &serde_json::Map<String, Value>,
    revision: i64,
) -> Result<ConfigResponse, String> {
    let resp = client
        .patch(format!("{server}/api/v1/channels/{id}/config"))
        .header("If-Match", revision.to_string())
        .json(patch)
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;
    let status = resp.status();
    if !status.is_success() {
        let msg = api_error_message(resp).await;
        if status == StatusCode::CONFLICT {
            // The revision we read moved under us (a concurrent edit). Re-read
            // and retry — never decrement; the higher revision always wins.
            return Err(conflict_hint(&msg));
        }
        return Err(msg);
    }
    let raw = resp
        .text()
        .await
        .map_err(|e| format!("invalid response: {e}"))?;
    decode_config_response(raw)
}

/// The `--json` output line: the server's response body verbatim, with trailing
/// whitespace trimmed (the server's JSON encoder appends a newline). This is a
/// PASSTHROUGH, not a re-serialization of the typed [`ChannelConfigView`] — so a
/// field the server reports that the CLI does not yet model still reaches a
/// `--json` consumer instead of being silently dropped by a typed round-trip.
fn passthrough_json(raw: &str) -> String {
    raw.trim_end().to_string()
}

/// Emit a response as either the server's JSON body verbatim (`--json`, via
/// [`passthrough_json`]) or the human block (rendered from the typed view).
fn emit_view(id: &str, resp: &ConfigResponse, json_out: bool) {
    if json_out {
        println!("{}", passthrough_json(&resp.raw));
    } else {
        render_config_view(id, &resp.view);
    }
}

// ─── Subcommand entry points ────────────────────────────────────────────

/// `channel config get <name>` — effective values + provenance + revision.
pub(crate) async fn cmd_config_get(
    client: &reqwest::Client,
    server: &str,
    name: &str,
    json_out: bool,
) -> Result<(), String> {
    let id = canonicalize_channel_id(name);
    validate_path_param(&id, "channel id")?;
    let resp = fetch_config(client, server, &id).await?;
    emit_view(&id, &resp, json_out);
    Ok(())
}

/// `channel config set <name> key=value…` — apply one or more knob overrides
/// under the current revision's `If-Match` guard, then render the post-apply
/// config (the server echoes the bumped revision + new effective values).
pub(crate) async fn cmd_config_set(
    client: &reqwest::Client,
    server: &str,
    name: &str,
    specs: &[String],
    json_out: bool,
) -> Result<(), String> {
    // Build (and validate) the patch before any round-trip so a typo'd knob or
    // value fails fast without touching the server.
    let patch = build_set_patch(specs)?;
    apply_validated_patch(client, server, name, &patch, json_out).await
}

/// `channel config unset <name> knob…` — clear one or more knobs back to inherit.
pub(crate) async fn cmd_config_unset(
    client: &reqwest::Client,
    server: &str,
    name: &str,
    keys: &[String],
    json_out: bool,
) -> Result<(), String> {
    let patch = build_unset_patch(keys)?;
    apply_validated_patch(client, server, name, &patch, json_out).await
}

/// Shared write path for `set`/`unset`: validate the id, read the current
/// revision, then PATCH under its `If-Match` guard and render the result.
async fn apply_validated_patch(
    client: &reqwest::Client,
    server: &str,
    name: &str,
    patch: &serde_json::Map<String, Value>,
    json_out: bool,
) -> Result<(), String> {
    let id = canonicalize_channel_id(name);
    validate_path_param(&id, "channel id")?;
    // Read the current revision for the If-Match guard. This GET also surfaces a
    // toggle-off 403 / unknown-channel 404 before the write is attempted.
    let current = fetch_config(client, server, &id).await?;
    let resp = apply_config_patch(client, server, &id, patch, current.view.revision).await?;
    emit_view(&id, &resp, json_out);
    Ok(())
}

#[cfg(test)]
#[path = "channel_config_tests.rs"]
mod tests;
