//! Channel lifecycle verbs — `channel create` + `channel info` (RFC 0011 §C).
//!
//! Split from [`super::channel`] so that file stays under the 500-line review
//! cap (same convention as `channel_dispatch`/`channel_watch`/`channel_render`).
//! Thin-client pattern: each entry point marshals args into a REST call and
//! prints the response. Wire shapes mirror `internal/server/channel_types.go`.

use clap::ValueEnum;
use colored::Colorize;

use crate::commands::channel::canonicalize_channel_id;
use crate::commands::channel_dispatch::RespondPolicy;
use crate::commands::channel_types::{ChannelView, CreateChannelMember, CreateChannelRequest};
use crate::types::{api_error_message, validate_path_param, validate_resource_id};

// ─── Pure helpers (testable without an HTTP server) ─────────────────────

/// Validate a bare `channel create <name>` and return its canonical id.
///
/// The server derives `group:<name>` from the bare name (`handleCreateChannel`),
/// so a `:`-bearing name would produce a drifted id like `group:group:x` or
/// inject a `dm:`/`thread:` prefix — rejected locally with a clear message.
/// The bare name then has to clear the cross-component resource-id shape
/// ([`validate_resource_id`]): it *becomes* the addressable `group:<name>` id,
/// so it must obey the same lowercase/digit/hyphen rule as every member id and
/// channel id the rest of the CLI validates. The server itself only prepends
/// `group:` (no shape check), so — as with `validate_session_label` — the CLI
/// is intentionally the stricter path, keeping its own minted ids addressable
/// by the later `info`/`join` verbs. `validate_resource_id` also subsumes the
/// path-injection guard (its charset admits no `/`, `\`, `..`, `?`, `#`, `%`).
pub(crate) fn validate_new_channel_name(name: &str) -> Result<String, String> {
    let trimmed = name.trim();
    if trimmed.is_empty() {
        return Err("channel name must not be empty".into());
    }
    if trimmed.contains(':') {
        return Err(
            "channel name must be bare (no ':'); the server derives the canonical 'group:<name>' id"
                .into(),
        );
    }
    validate_resource_id(trimmed, "channel name")?;
    Ok(format!("group:{trimmed}"))
}

/// Parse a `channel create --member` spec of the form `<id>` or
/// `<id>:<disposition>` into a wire-ready [`CreateChannelMember`].
///
/// A bare `<id>` defaults to `when_mentioned` (the server default). The
/// disposition half is validated against the shared [`RespondPolicy`] value
/// set so a typo fails locally with the full vocabulary listed, rather than
/// round-tripping a server 400. Member ids never contain `:`
/// ([`validate_resource_id`] rejects it), so splitting on the first `:` is
/// unambiguous.
pub(crate) fn parse_member_spec(spec: &str) -> Result<CreateChannelMember, String> {
    // Distinguish "no `:`" (bare id → default) from "`:` with an empty half"
    // (an explicit-but-blank disposition, e.g. `iron-fox:`), which is a typo
    // worth rejecting rather than silently treating as the default.
    let (id, disposition) = match spec.split_once(':') {
        Some((id, disp)) => (id, Some(disp)),
        None => (spec, None),
    };
    validate_resource_id(id, "member id")?;
    let respond = match disposition {
        None => "when_mentioned".to_string(),
        Some("") => {
            return Err(format!(
                "member '{id}' has an empty disposition after ':'; drop the ':' for the default \
                 (when_mentioned) or supply one of: when_mentioned, always, never, participant, \
                 chair, addressed, observer"
            ));
        }
        Some(disp) => RespondPolicy::from_str(disp, false)
            .map_err(|_| {
                format!(
                    "invalid disposition '{disp}' for member '{id}'; expected one of: \
                     when_mentioned, always, never, participant, chair, addressed, observer"
                )
            })?
            .as_wire_str()
            .to_string(),
    };
    Ok(CreateChannelMember {
        id: id.to_string(),
        respond,
    })
}

/// Render a single channel's detail block (id, type, description, members).
/// Shared by `channel info` and the `channel create` confirmation, which both
/// render a server-fetched [`ChannelView`] (one source of truth — see
/// [`cmd_channel_create`]'s read-after-write).
///
/// Each member shows its persisted `respond` token *plus* the v0.3.8 salience
/// signal. The store normalizes the disposition vocabulary to the legacy triple
/// before persisting (`chair`/`participant` → `always`, `observer` → `never`),
/// so `respond` alone reads back as the legacy token; the `[salience-gated …]`
/// suffix is what lets an operator confirm a participant/chair disposition
/// actually took effect. An empty `joined_at` is elided.
///
/// `heading` prefixes the id line (e.g. `"Created "` for the create
/// confirmation, `""` for `info`) so the verb's one-line summary and the
/// detail block share a single id render rather than printing it twice.
fn render_channel_view(view: &ChannelView, heading: &str) {
    println!(
        "{heading}{}  {}",
        format!("#{}", view.id).cyan(),
        view.channel_type
    );
    if !view.description.is_empty() {
        println!("  {}", view.description);
    }
    if view.members.is_empty() {
        println!("  (no members)");
        return;
    }
    println!("  members:");
    for m in &view.members {
        let salience = if m.salience_gated {
            match m.threshold {
                Some(t) => format!("  [salience-gated, threshold {t}]"),
                None => "  [salience-gated]".to_string(),
            }
        } else {
            String::new()
        };
        let joined = if m.joined_at.is_empty() {
            String::new()
        } else {
            format!("  {}", m.joined_at.dimmed())
        };
        println!(
            "    {}  {}{}{}",
            m.id.bold(),
            m.respond_policy,
            salience.dimmed(),
            joined
        );
    }
}

/// `GET /api/v1/channels/{canonical}` → typed [`ChannelView`]. The caller is
/// responsible for validating `canonical` first.
async fn fetch_channel(
    client: &reqwest::Client,
    server: &str,
    canonical: &str,
) -> Result<ChannelView, String> {
    let resp = client
        .get(format!("{server}/api/v1/channels/{canonical}"))
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }
    resp.json()
        .await
        .map_err(|e| format!("invalid response: {e}"))
}

// ─── Subcommand entry points ────────────────────────────────────────────

pub(crate) async fn cmd_channel_create(
    client: &reqwest::Client,
    server: &str,
    name: &str,
    description: &str,
    member_specs: &[String],
    json_out: bool,
) -> Result<(), String> {
    // Validate locally (rejects `:`-bearing / mis-shaped names); the POST sends
    // the bare name and the server derives the canonical id we read back below.
    let canonical = validate_new_channel_name(name)?;
    if member_specs.is_empty() {
        return Err(
            "at least one --member is required (e.g. --member iron-fox:participant)".into(),
        );
    }
    let members = member_specs
        .iter()
        .map(|s| parse_member_spec(s))
        .collect::<Result<Vec<_>, _>>()?;
    let req = CreateChannelRequest {
        name: name.trim().to_string(),
        description: description.trim().to_string(),
        members,
    };
    let resp = client
        .post(format!("{server}/api/v1/channels"))
        .json(&req)
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }
    // The POST 201 echoes channelToResponse(created, nil) — members omitted, and
    // the disposition we sent is normalized at the store boundary (chair/
    // participant → always, observer → never). Echoing the members we *sent*
    // would therefore misreport the persisted dispositions. Read-after-write
    // instead: GET the channel so the confirmation shows exactly what `channel
    // info` would — one source of truth, no client-side replication of the
    // server's Normalize(). The parsed 201 body is kept only as a fallback if
    // the read-back fails (the channel was still created, so we must not error).
    let post_view: ChannelView = resp
        .json()
        .await
        .map_err(|e| format!("invalid response: {e}"))?;
    let view = match fetch_channel(client, server, &canonical).await {
        Ok(v) => v,
        Err(e) => {
            eprintln!("warning: created #{canonical} but could not read back members: {e}");
            post_view
        }
    };
    if json_out {
        println!("{}", serde_json::to_string(&view).unwrap());
        return Ok(());
    }
    render_channel_view(&view, "Created ");
    Ok(())
}

pub(crate) async fn cmd_channel_info(
    client: &reqwest::Client,
    server: &str,
    name: &str,
    json_out: bool,
) -> Result<(), String> {
    let canonical = canonicalize_channel_id(name);
    validate_path_param(&canonical, "channel id")?;
    let view = fetch_channel(client, server, &canonical).await?;
    if json_out {
        println!("{}", serde_json::to_string(&view).unwrap());
        return Ok(());
    }
    render_channel_view(&view, "");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    // ─── validate_new_channel_name ─────────────────────────────────────

    #[test]
    fn validate_new_channel_name_accepts_bare_and_returns_canonical() {
        assert_eq!(
            validate_new_channel_name("planning").unwrap(),
            "group:planning"
        );
        // Surrounding whitespace is trimmed before canonicalization.
        assert_eq!(
            validate_new_channel_name("  planning  ").unwrap(),
            "group:planning"
        );
    }

    #[test]
    fn validate_new_channel_name_rejects_empty() {
        assert!(validate_new_channel_name("   ").is_err());
    }

    #[test]
    fn validate_new_channel_name_rejects_non_resource_id_shape() {
        // The name becomes the addressable `group:<name>` id, so it must obey
        // the same resource-id shape the CLI enforces on every other id (the
        // CLI is intentionally the stricter path). Uppercase, spaces, and edge
        // hyphens are rejected before the round-trip.
        assert!(validate_new_channel_name("Planning").is_err(), "uppercase");
        assert!(validate_new_channel_name("my room").is_err(), "space");
        assert!(
            validate_new_channel_name("-planning").is_err(),
            "leading hyphen"
        );
        assert!(
            validate_new_channel_name("planning-").is_err(),
            "trailing hyphen"
        );
        // Valid id-shaped names still pass and canonicalize.
        assert_eq!(
            validate_new_channel_name("sprint-2").unwrap(),
            "group:sprint-2"
        );
    }

    #[test]
    fn validate_new_channel_name_rejects_colon_bearing_names() {
        // A `:`-bearing name would drift the server-derived id
        // (group:group:x) or inject a dm:/thread: prefix — reject locally.
        assert!(validate_new_channel_name("group:planning").is_err());
        assert!(validate_new_channel_name("dm:a:b").is_err());
    }

    // ─── parse_member_spec ─────────────────────────────────────────────

    #[test]
    fn parse_member_spec_bare_id_defaults_to_when_mentioned() {
        let m = parse_member_spec("iron-fox").unwrap();
        assert_eq!(m.id, "iron-fox");
        assert_eq!(m.respond, "when_mentioned");
    }

    #[test]
    fn parse_member_spec_accepts_v038_dispositions() {
        for (spec, wire) in [
            ("iron-fox:participant", "participant"),
            ("ada:chair", "chair"),
            ("rex:observer", "observer"),
            ("ivy:addressed", "addressed"),
            ("bob:always", "always"),
        ] {
            let m = parse_member_spec(spec).unwrap();
            assert_eq!(m.respond, wire, "spec {spec}");
        }
    }

    #[test]
    fn parse_member_spec_rejects_unknown_disposition() {
        let err = parse_member_spec("ada:moderator").unwrap_err();
        assert!(
            err.contains("moderator"),
            "error names the bad value: {err}"
        );
        assert!(
            err.contains("participant"),
            "error lists the vocabulary: {err}"
        );
    }

    #[test]
    fn parse_member_spec_rejects_invalid_member_id() {
        // Uppercase fails validate_resource_id before the disposition is parsed.
        assert!(parse_member_spec("Ada:chair").is_err());
    }

    #[test]
    fn parse_member_spec_rejects_empty_disposition_after_colon() {
        // `iron-fox:` is an explicit-but-blank disposition — a typo, not the
        // default. A bare `iron-fox` (no colon) still defaults silently.
        let err = parse_member_spec("iron-fox:").unwrap_err();
        assert!(
            err.contains("empty disposition"),
            "explains the blank half: {err}"
        );
        assert_eq!(
            parse_member_spec("iron-fox").unwrap().respond,
            "when_mentioned",
            "bare id (no colon) still defaults",
        );
    }
}
