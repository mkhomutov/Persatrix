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
/// inject a `dm:`/`thread:` prefix. Reject it locally and run the resulting
/// canonical id through [`validate_path_param`] (blocks traversal/injection).
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
    let canonical = format!("group:{trimmed}");
    validate_path_param(&canonical, "channel id")?;
    Ok(canonical)
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
    let (id, disposition) = match spec.split_once(':') {
        Some((id, disp)) => (id, disp),
        None => (spec, ""),
    };
    validate_resource_id(id, "member id")?;
    let respond = if disposition.is_empty() {
        "when_mentioned".to_string()
    } else {
        RespondPolicy::from_str(disposition, false)
            .map_err(|_| {
                format!(
                    "invalid disposition '{disposition}' for member '{id}'; expected one of: \
                     when_mentioned, always, never, participant, chair, addressed, observer"
                )
            })?
            .as_wire_str()
            .to_string()
    };
    Ok(CreateChannelMember {
        id: id.to_string(),
        respond,
    })
}

/// Render a single channel's detail block (id, type, description, members).
/// Shared by `channel info` and the `channel create` confirmation. Each member
/// tuple is `(id, disposition, joined_at)`; an empty `joined_at` is elided (the
/// create confirmation echoes members without a persisted timestamp).
fn render_channel_detail(
    id: &str,
    channel_type: &str,
    description: &str,
    members: &[(String, String, String)],
) {
    println!("{}  {}", format!("#{id}").cyan(), channel_type);
    if !description.is_empty() {
        println!("  {description}");
    }
    if members.is_empty() {
        println!("  (no members)");
        return;
    }
    println!("  members:");
    for (member_id, respond, joined_at) in members {
        let joined = if joined_at.is_empty() {
            String::new()
        } else {
            format!("  {}", joined_at.dimmed())
        };
        println!("    {}  {}{}", member_id.bold(), respond, joined);
    }
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
    // Validate locally (rejects `:`-bearing names + path-injection); the POST
    // sends the bare name and the server derives the canonical id.
    validate_new_channel_name(name)?;
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
        description: description.to_string(),
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
    // The POST response echoes channelToResponse(created, nil) — members are
    // omitted on create (server contract), so for the human view we echo the
    // members we just sent (accurate: the server accepted them with 201). For
    // --json we emit the server's response verbatim; `channel info` re-reads
    // the persisted members with their joined_at timestamps.
    let view: ChannelView = resp
        .json()
        .await
        .map_err(|e| format!("invalid response: {e}"))?;
    if json_out {
        println!("{}", serde_json::to_string(&view).unwrap());
        return Ok(());
    }
    println!("Created {}", format!("#{}", view.id).cyan());
    let rendered: Vec<(String, String, String)> = req
        .members
        .iter()
        .map(|m| (m.id.clone(), m.respond.clone(), String::new()))
        .collect();
    render_channel_detail(&view.id, &view.channel_type, description.trim(), &rendered);
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
    let resp = client
        .get(format!("{server}/api/v1/channels/{canonical}"))
        .send()
        .await
        .map_err(|e| format!("connection failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(api_error_message(resp).await);
    }
    let view: ChannelView = resp
        .json()
        .await
        .map_err(|e| format!("invalid response: {e}"))?;
    if json_out {
        println!("{}", serde_json::to_string(&view).unwrap());
        return Ok(());
    }
    let rendered: Vec<(String, String, String)> = view
        .members
        .iter()
        .map(|m| (m.id.clone(), m.respond_policy.clone(), m.joined_at.clone()))
        .collect();
    render_channel_detail(&view.id, &view.channel_type, &view.description, &rendered);
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
}
