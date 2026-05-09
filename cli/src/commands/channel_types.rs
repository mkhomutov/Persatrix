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
}

/// JSON shape returned by `GET /api/v1/channels` (list rows) and
/// `GET /api/v1/channels/{id}` (single channel with members).
#[derive(Deserialize, Serialize)]
pub(crate) struct ChannelView {
    pub(crate) id: String,
    #[serde(default)]
    pub(crate) name: String,
    #[serde(rename = "channel_type")]
    pub(crate) channel_type: String,
    #[serde(default)]
    pub(crate) description: String,
    pub(crate) created_at: String,
    /// Empty on list; populated on single-channel fetch.
    #[serde(default)]
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
    fn channel_view_deserializes_list_row() {
        // GET /api/v1/channels returns rows with no members; matches
        // internal/server/channel_handlers.go::handleListChannels.
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
        assert!(row.members.is_empty(), "list rows omit members");
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
        };
        let json = serde_json::to_value(&req).unwrap();
        assert_eq!(json["sender_id"], "alice");
        assert_eq!(json["content"], "hi");
        assert!(json.get("thread_id").is_none(), "empty thread_id omitted");
        assert!(json.get("mentions").is_none(), "empty mentions omitted");
    }

    #[test]
    fn publish_message_request_includes_mentions_when_set() {
        let req = PublishMessageRequest {
            sender_id: "alice".to_string(),
            content: "ping".to_string(),
            thread_id: "msg-100".to_string(),
            mentions: vec!["bob".to_string(), "carol".to_string()],
        };
        let json = serde_json::to_value(&req).unwrap();
        assert_eq!(json["thread_id"], "msg-100");
        assert_eq!(json["mentions"], serde_json::json!(["bob", "carol"]));
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
