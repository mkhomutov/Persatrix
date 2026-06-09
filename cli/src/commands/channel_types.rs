//! Channel REST DTOs — RFC 0011 §C.
//!
//! Wire shapes mirror `internal/server/channel_types.go`. Field names
//! match the Go JSON tags exactly. `--json` output preserves field
//! shapes that are explicitly modeled here; unknown server fields are
//! dropped (no `#[serde(deny_unknown_fields)]`, no flatten-extras
//! catch-all). Adding a server field requires a coordinated bump on
//! this file.

use serde::{Deserialize, Serialize};

#[derive(Deserialize, Serialize)]
pub(crate) struct ChannelMember {
    pub(crate) id: String,
    #[serde(rename = "respond")]
    pub(crate) respond_policy: String,
    pub(crate) joined_at: String,
    /// RFC 0030 Tier B (v0.3.8) salience signal. The store normalizes the
    /// disposition vocabulary to the legacy triple before persisting
    /// (`chair`/`participant` → `always`, `observer` → `never`), so
    /// `respond_policy` alone cannot tell a salience-gated participant from a
    /// legacy `always`-replier. `salience_gated` is the one bit that survives
    /// — it's what lets `channel info` read back the disposition an operator
    /// set. `#[serde(default)]` tolerates a pre-v0.3.8 server that omits it.
    #[serde(default)]
    pub(crate) salience_gated: bool,
    /// Per-member salience `threshold` (the bid score to clear). Tri-state:
    /// absent → unset (bias-to-silence); a `chair` carries the low server
    /// default. Mirrors the Go `threshold,omitempty` pointer.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) threshold: Option<f64>,
}

/// JSON shape returned by `GET /api/v1/channels` (list rows) and
/// `GET /api/v1/channels/{id}` (single channel with members).
#[derive(Deserialize, Serialize)]
pub(crate) struct ChannelView {
    pub(crate) id: String,
    // `--json` re-serializes this view, so mirror the Go `name,omitempty`:
    // a DM/thread row (and the create 201) carry an empty name that the
    // server drops; emitting `"name":""` would diverge from the wire.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub(crate) name: String,
    #[serde(rename = "channel_type")]
    pub(crate) channel_type: String,
    #[serde(default)]
    pub(crate) description: String,
    pub(crate) created_at: String,
    /// Populated on both list and single-channel fetch as of PR #316
    /// (when `handleListChannels` began returning members per row to
    /// match the per-channel response shape). `#[serde(default)]` keeps
    /// the field absent-tolerant for forward-compat with pre-#316
    /// servers, which omitted `members` on list rows entirely — see
    /// `channel_view_deserializes_list_row_without_members` below.
    ///
    /// `skip_serializing_if` mirrors the Go `members,omitempty`: the create
    /// 201 omits members (server contract), so re-serializing an empty Vec as
    /// `"members":[]` under `--json` would falsely tell a script the channel
    /// has none — see `channel_view_serialize_matches_server_omitempty`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub(crate) members: Vec<ChannelMember>,
}

/// Envelope for `GET /api/v1/channels`. `next_cursor` mirrors the Go
/// server's keyset-pagination signal (empty string when the trailing
/// page has been returned). Consumed by
/// [`crate::commands::channel::should_warn_truncation`] to surface a
/// stderr warning when the listing is partial (PR #302 deep-review
/// finding 1).
#[derive(Deserialize)]
pub(crate) struct ListChannelsResponse {
    pub(crate) channels: Vec<ChannelView>,
    #[serde(default)]
    pub(crate) next_cursor: String,
}

#[derive(Deserialize, Serialize)]
pub(crate) struct ChannelMessage {
    pub(crate) id: String,
    pub(crate) channel_id: String,
    pub(crate) sender_id: String,
    pub(crate) content: String,
    pub(crate) timestamp: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub(crate) thread_id: String,
    #[serde(default)]
    pub(crate) mentions: Vec<String>,
    /// Mirrors `channelMessageResponse.Metadata` (`map[string]any` with
    /// `omitempty`). Captured so `--json` output round-trips server
    /// metadata (e.g. trace propagation, source-system tags) instead
    /// of silently dropping it. Keep `Option<Map>` over `Map` so the
    /// absent-vs-empty distinction matches Go's `omitempty` exactly.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) metadata: Option<serde_json::Map<String, serde_json::Value>>,
}

/// Envelope for `GET /api/v1/channels/{id}/messages` and the thread
/// endpoint.
#[derive(Deserialize)]
pub(crate) struct HistoryResponse {
    pub(crate) messages: Vec<ChannelMessage>,
}

/// Request body for `POST /api/v1/channels/{id}/messages`.
///
/// Subset of the Go `publishMessageRequest` ([channel_types.go]):
/// `metadata` (`map[string]any`, omitempty) and `channel_type` (omitempty)
/// are accepted server-side but not yet exposed by the CLI surface. Add
/// the corresponding fields here when `--metadata` / `--channel-type`
/// flags land — extending the wire shape silently would otherwise
/// surprise contributors who assume parity. PR #302 deep-review N3.
#[derive(Serialize)]
pub(crate) struct PublishMessageRequest {
    pub(crate) sender_id: String,
    pub(crate) content: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub(crate) thread_id: String,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub(crate) mentions: Vec<String>,
    /// RFC 0031 Phase 3 `--session` override (id-or-label resolved to a
    /// canonical id CLI-side before send). Omitted when empty so the
    /// orchestrator keeps its boot default / auto-binding — matching the Go
    /// `session_id,omitempty` tag on `publishMessageRequest`.
    #[serde(skip_serializing_if = "String::is_empty")]
    pub(crate) session_id: String,
    /// ISSUE-0085 PR 5 `--epoch` run/test-isolation override, orthogonal to
    /// `session_id`. Omitted when empty so the orchestrator keeps its boot
    /// epoch ("live") — matching the Go `epoch_id,omitempty` tag.
    #[serde(skip_serializing_if = "String::is_empty")]
    pub(crate) epoch_id: String,
}

/// One `{id, respond}` member entry in a [`CreateChannelRequest`], mirroring
/// the Go `channelMemberRequest` (internal/server/channel_types.go). The
/// `respond` token is a `channels.RespondPolicy` wire string validated CLI-side
/// via [`crate::commands::channel_dispatch::RespondPolicy`].
#[derive(Serialize, Clone, Debug)]
pub(crate) struct CreateChannelMember {
    pub(crate) id: String,
    pub(crate) respond: String,
}

/// Request body for `POST /api/v1/channels`. Mirrors the Go `createChannelRequest`
/// — the `name` is sent BARE (the server derives the canonical `group:<name>`
/// id), and at least one member is required (the server 400s otherwise).
#[derive(Serialize)]
pub(crate) struct CreateChannelRequest {
    pub(crate) name: String,
    pub(crate) description: String,
    pub(crate) members: Vec<CreateChannelMember>,
}

#[derive(Serialize)]
pub(crate) struct AddMemberRequest {
    pub(crate) id: String,
    /// Empty string is preserved on the wire; the server falls back to
    /// `when_mentioned` when blank. The CLI surface validates non-empty
    /// values via the `RespondPolicy` enum, so blank is unreachable in
    /// practice — preserved here for parity with `addMemberRequest`
    /// across alternative callers (e.g. ad-hoc curl scripts mirrored
    /// against this DTO).
    pub(crate) respond: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn channel_view_deserializes_list_row_without_members() {
        // GET /api/v1/channels populates `members` per row as of PR #316;
        // this test pins the forward-compat path when `members` is
        // *absent* on the wire — e.g. a pre-#316 server, or a future
        // endpoint that elides the field. `#[serde(default)]` on
        // `ChannelView::members` supplies the empty `Vec` rather than
        // failing deserialization. The "members present" path is
        // exercised by `channel_view_deserializes_with_members` below.
        let json = serde_json::json!({
            "id": "group:planning",
            "name": "planning",
            "channel_type": "group",
            "description": "team planning",
            "created_at": "2026-05-09T10:00:00Z",
        });
        let row: ChannelView = serde_json::from_value(json).unwrap();
        assert_eq!(row.id, "group:planning");
        assert_eq!(row.channel_type, "group");
        assert!(
            row.members.is_empty(),
            "absent `members` deserializes to empty Vec via serde default",
        );
    }

    #[test]
    fn channel_view_deserializes_with_members() {
        // GET /api/v1/channels/{id} populates the members array.
        let json = serde_json::json!({
            "id": "group:planning",
            "name": "planning",
            "channel_type": "group",
            "description": "",
            "created_at": "2026-05-09T10:00:00Z",
            "members": [
                {"id": "alice", "respond": "always",        "joined_at": "2026-05-09T10:00:00Z"},
                {"id": "bob",   "respond": "when_mentioned", "joined_at": "2026-05-09T10:01:00Z"},
            ],
        });
        let ch: ChannelView = serde_json::from_value(json).unwrap();
        assert_eq!(ch.members.len(), 2);
        assert_eq!(ch.members[0].id, "alice");
        assert_eq!(ch.members[0].respond_policy, "always");
        assert_eq!(ch.members[1].respond_policy, "when_mentioned");
    }

    #[test]
    fn channel_member_deserializes_salience_signal() {
        // GET /api/v1/channels/{id} carries the v0.3.8 Tier B signal so a
        // normalized `always` member can be told apart from a salience-gated
        // participant/chair. A chair surfaces gated=true + the default threshold.
        let json = serde_json::json!({
            "id": "alice", "respond": "always", "joined_at": "2026-05-09T10:00:00Z",
            "salience_gated": true, "threshold": 0.15,
        });
        let m: ChannelMember = serde_json::from_value(json).unwrap();
        assert_eq!(m.respond_policy, "always");
        assert!(m.salience_gated);
        assert_eq!(m.threshold, Some(0.15));
    }

    #[test]
    fn channel_member_defaults_salience_signal_when_absent() {
        // A pre-v0.3.8 server (or a legacy `always` member) omits the fields;
        // serde defaults keep the row absent-tolerant.
        let json = serde_json::json!({
            "id": "bob", "respond": "when_mentioned", "joined_at": "2026-05-09T10:00:00Z",
        });
        let m: ChannelMember = serde_json::from_value(json).unwrap();
        assert!(!m.salience_gated);
        assert_eq!(m.threshold, None);
    }

    #[test]
    fn channel_view_serialize_matches_server_omitempty() {
        // `--json` re-serializes the typed view, so it must mirror the Go
        // `omitempty` tags: an empty `name` (DM/thread) and an empty `members`
        // (the create 201, which omits members) must NOT appear as `""`/`[]`.
        // Emitting `"members":[]` on create would tell a script the channel has
        // no members when it was just created with some.
        let view = ChannelView {
            id: "group:planning".into(),
            name: String::new(),
            channel_type: "group".into(),
            description: String::new(),
            created_at: "2026-05-09T10:00:00Z".into(),
            members: Vec::new(),
        };
        let v = serde_json::to_value(&view).unwrap();
        assert!(
            v.get("name").is_none(),
            "empty name omitted (server omitempty)"
        );
        assert!(
            v.get("members").is_none(),
            "empty members omitted (server omitempty)"
        );
        // description has no omitempty on the server, so it stays present.
        assert_eq!(v.get("description").and_then(|d| d.as_str()), Some(""));
    }

    #[test]
    fn channel_member_serialize_omits_unset_threshold() {
        let m = ChannelMember {
            id: "bob".into(),
            respond_policy: "when_mentioned".into(),
            joined_at: "2026-05-09T10:00:00Z".into(),
            salience_gated: false,
            threshold: None,
        };
        let v = serde_json::to_value(&m).unwrap();
        assert!(v.get("threshold").is_none(), "unset threshold omitted");
        // round-trips under the `respond` wire name.
        assert_eq!(
            v.get("respond").and_then(|r| r.as_str()),
            Some("when_mentioned")
        );
    }

    #[test]
    fn list_channels_response_deserializes_envelope() {
        let json = serde_json::json!({
            "channels": [
                {"id": "group:a", "name": "a", "channel_type": "group", "description": "", "created_at": "2026-05-09T10:00:00Z"},
                {"id": "dm:alice:bob", "channel_type": "dm", "description": "", "created_at": "2026-05-09T10:01:00Z"},
            ],
            "next_cursor": "dm:alice:bob",
        });
        let resp: ListChannelsResponse = serde_json::from_value(json).unwrap();
        assert_eq!(resp.channels.len(), 2);
        assert_eq!(resp.next_cursor, "dm:alice:bob");
        // DM rows omit name; serde default supplies the empty string.
        assert_eq!(resp.channels[1].name, "");
    }

    #[test]
    fn list_channels_response_omits_next_cursor_when_absent() {
        // Go's `omitempty` drops next_cursor on the trailing page.
        let json = serde_json::json!({"channels": []});
        let resp: ListChannelsResponse = serde_json::from_value(json).unwrap();
        assert!(resp.channels.is_empty());
        assert_eq!(resp.next_cursor, "");
    }

    #[test]
    fn channel_message_deserializes_with_thread_id() {
        let json = serde_json::json!({
            "id": "msg-123",
            "channel_id": "group:planning",
            "sender_id": "alice",
            "content": "Hello",
            "timestamp": "2026-05-09T10:00:00Z",
            "thread_id": "msg-100",
            "mentions": ["bob"],
        });
        let msg: ChannelMessage = serde_json::from_value(json).unwrap();
        assert_eq!(msg.thread_id, "msg-100");
        assert_eq!(msg.mentions, vec!["bob".to_string()]);
    }

    #[test]
    fn channel_message_handles_omitempty_thread_id() {
        // Top-level publish: thread_id is empty → Go's `omitempty` drops it.
        let json = serde_json::json!({
            "id": "msg-1",
            "channel_id": "group:x",
            "sender_id": "alice",
            "content": "Hi",
            "timestamp": "2026-05-09T10:00:00Z",
            "mentions": [],
        });
        let msg: ChannelMessage = serde_json::from_value(json).unwrap();
        assert_eq!(msg.thread_id, "");
        assert!(msg.mentions.is_empty());
    }

    #[test]
    fn publish_message_request_omits_empty_optional_fields() {
        // `skip_serializing_if` keeps the wire shape clean — top-level
        // publish without mentions or thread sends only sender_id +
        // content.
        let req = PublishMessageRequest {
            sender_id: "alice".to_string(),
            content: "hi".to_string(),
            thread_id: String::new(),
            mentions: Vec::new(),
            session_id: String::new(),
            epoch_id: String::new(),
        };
        let json = serde_json::to_value(&req).unwrap();
        assert_eq!(json["sender_id"], "alice");
        assert_eq!(json["content"], "hi");
        assert!(json.get("thread_id").is_none(), "empty thread_id omitted");
        assert!(json.get("mentions").is_none(), "empty mentions omitted");
        assert!(json.get("session_id").is_none(), "empty session_id omitted");
        assert!(json.get("epoch_id").is_none(), "empty epoch_id omitted");
    }

    #[test]
    fn publish_message_request_includes_mentions_when_set() {
        let req = PublishMessageRequest {
            sender_id: "alice".to_string(),
            content: "ping".to_string(),
            thread_id: "msg-100".to_string(),
            mentions: vec!["bob".to_string(), "carol".to_string()],
            session_id: String::new(),
            epoch_id: String::new(),
        };
        let json = serde_json::to_value(&req).unwrap();
        assert_eq!(json["thread_id"], "msg-100");
        assert_eq!(json["mentions"], serde_json::json!(["bob", "carol"]));
    }

    #[test]
    fn publish_message_request_includes_session_id_when_set() {
        // RFC 0031 Phase 3: a resolved `--session` id rides on `session_id`.
        let req = PublishMessageRequest {
            sender_id: "alice".to_string(),
            content: "hi".to_string(),
            thread_id: String::new(),
            mentions: Vec::new(),
            session_id: "run-arc-3".to_string(),
            epoch_id: String::new(),
        };
        let json = serde_json::to_value(&req).unwrap();
        assert_eq!(json["session_id"], "run-arc-3");
    }

    #[test]
    fn publish_message_request_includes_epoch_id_when_set() {
        // ISSUE-0085 PR 5: a resolved `--epoch` id rides on `epoch_id`,
        // orthogonal to the `--session` override.
        let req = PublishMessageRequest {
            sender_id: "alice".to_string(),
            content: "hi".to_string(),
            thread_id: String::new(),
            mentions: Vec::new(),
            session_id: String::new(),
            epoch_id: "ci-run-5".to_string(),
        };
        let json = serde_json::to_value(&req).unwrap();
        assert_eq!(json["epoch_id"], "ci-run-5");
        assert!(json.get("session_id").is_none(), "empty session_id omitted");
    }

    #[test]
    fn channel_message_round_trips_metadata() {
        // PR #302 finding #5: the orchestrator's channelMessageResponse
        // carries `Metadata map[string]any` (omitempty). The Rust DTO
        // must capture and re-serialize it so `--json` does not silently
        // drop server-side fields.
        let json = serde_json::json!({
            "id": "msg-1",
            "channel_id": "group:x",
            "sender_id": "alice",
            "content": "hi",
            "timestamp": "2026-05-09T10:00:00Z",
            "mentions": [],
            "metadata": {"trace_id": "abc-123", "source": "cli"},
        });
        let msg: ChannelMessage = serde_json::from_value(json.clone()).unwrap();
        let metadata = msg.metadata.as_ref().expect("metadata preserved");
        assert_eq!(metadata["trace_id"], "abc-123");
        assert_eq!(metadata["source"], "cli");
        // Re-serialize: metadata round-trips byte-equivalent.
        let reserialized = serde_json::to_value(&msg).unwrap();
        assert_eq!(reserialized["metadata"], json["metadata"]);
    }

    #[test]
    fn channel_message_omits_metadata_when_absent() {
        // Mirrors Go's `omitempty`: a message without metadata
        // serializes without the key.
        let json = serde_json::json!({
            "id": "msg-1",
            "channel_id": "group:x",
            "sender_id": "alice",
            "content": "hi",
            "timestamp": "2026-05-09T10:00:00Z",
            "mentions": [],
        });
        let msg: ChannelMessage = serde_json::from_value(json).unwrap();
        assert!(msg.metadata.is_none());
        let reserialized = serde_json::to_value(&msg).unwrap();
        assert!(
            reserialized.get("metadata").is_none(),
            "absent metadata stays absent on the wire"
        );
    }
}
