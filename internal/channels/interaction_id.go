package channels

// interactionIDMetadataKey is the wire-level key for the RFC 0020
// `interaction_id`: when a publisher supplies it on the publish metadata bag
// it is lifted onto the typed `ChannelMessageEvent.interaction_id` proto
// field ([RFC 0030 governance layers]). Centralised so a future rename is one
// edit rather than a multi-callsite hunt — mirrors `cascadeDepthMetadataKey`
// and `participantTypeMetadataKey`.
//
// The deterministic governance layers (Layer 1 cost ceiling, Layer 2 reply
// budget, Layer 4 end-of-interaction votes) all attribute per interaction,
// so the id must survive the publish→fanout boundary the same way
// cascade_depth and participant_type do — `ChannelMessageEvent` has no
// metadata map, so a first-class field is required.
//
// The producer is the router's own resolver (interaction_resolver.go, the
// interaction-id producer plan IP1/IP2): `publishCommit` stamps the resolved
// id under this key on every publish, REPLACING any inbound claim — a
// publisher-supplied value is read only for the override debug log and never
// keys governance state. `readInteractionID` therefore reads the router's own
// stamped value downstream of resolution (the end-vote and reply-budget
// hooks), and the inbound claim upstream of it.
//
// [RFC 0030 governance layers]: ../../docs/rfcs/0030-governance-layers-pr-plan.md
const interactionIDMetadataKey = "interaction_id"

// interactionIDMaxBytes bounds the inbound interaction_id by UTF-8 byte length. The
// id is an attacker-influenceable opaque token off the untrusted publish
// metadata bag, and the layer PRs that consume it (Layer 2 reply budget,
// Layer 4 end-of-interaction votes) key per-interaction maps on the value —
// an unbounded id is an unbounded map-key growth vector (the same concern
// floor_control.go calls out for its session maps). 128 reuses the *value* of
// the agent receive path's `_CHANNEL_THREAD_ID_MAX_CHARS` cap (that cap counts
// code points, this bound counts bytes — equal for the ASCII id) and leaves
// generous headroom over the RFC 0020 id (a 36-char uuid4 / 26-char ULID).
//
// This publish-side bound is one of two: the value is also seeded onto agent
// metadata at the receive boundary, where the per-interaction map key is
// actually created, so the same byte cap is enforced there too
// (`_INTERACTION_ID_MAX_BYTES` in agents/channel_wire_metadata.py). A
// publish-only bound would leave a non-Go / compromised producer's oversized
// id riding straight onto that metadata unbounded.
const interactionIDMaxBytes = 128

// previousInteractionIDMetadataKey / previousInteractionTriggerMetadataKey
// carry the channel's most recently retired interaction id and the trigger
// that retired it ("idle" / "end_votes" — the §L instrument vocabulary) onto
// the publishes of the SUCCESSOR interaction (producer plan OQ 5). The id
// rotation alone carries no cause, so the agent-side rotation close had to
// label every observed rotation "structural"; these keys let the receiver
// pick the truthful close reason (idle_gap vs structural). Producer:
// [ChannelRouter.publishCommit] stamps the resolver's own retired-close
// record, REPLACING (deleting, when there is no retiree) any inbound claim —
// like `interaction_id`, a publisher-supplied value never drives receiver
// state. Absent is the no-retiree case (old producer / fresh channel /
// restart re-mint) and receivers keep the pre-OQ5 behaviour.
const (
	previousInteractionIDMetadataKey      = "previous_interaction_id"
	previousInteractionTriggerMetadataKey = "previous_interaction_close_trigger"
)

// readInteractionID extracts the inbound interaction_id from a publish
// metadata bag. Returns "" when absent, non-string, or longer than
// interactionIDMaxBytes — a malformed or oversized claim is treated as the
// untracked case (every governance layer stays at its uncapped default)
// rather than failing the dispatch, mirroring readParticipantType's
// tolerance. Over-length falls back to empty rather than truncating: a
// clipped opaque token would key a *different* interaction, which is worse
// than treating the publish as untracked.
func readInteractionID(metadata map[string]any) string {
	return readBoundedIDValue(metadata, interactionIDMetadataKey)
}

// readPreviousInteractionID extracts the retired-interaction id stamped by
// [ChannelRouter.publishCommit] (producer plan OQ 5) for the dispatch-time
// lift onto `ChannelMessageEvent.previous_interaction_id`. Same bound and
// tolerance as [readInteractionID] — the value is router-stamped on every
// routed publish, but the reader stays defensive for a path that bypassed
// the resolver.
func readPreviousInteractionID(metadata map[string]any) string {
	return readBoundedIDValue(metadata, previousInteractionIDMetadataKey)
}

// readPreviousInteractionTrigger extracts the retired interaction's close
// trigger for the dispatch-time lift onto
// `ChannelMessageEvent.previous_interaction_close_trigger`. Allowlisted to
// the §L trigger vocabulary the resolver actually stamps ([idleTrigger] /
// [endVotesTrigger]); anything else reads as absent — the receiver then
// keeps its legacy label, which is the same degradation an unrecognised
// value would get agent-side (the seed point re-validates).
func readPreviousInteractionTrigger(metadata map[string]any) string {
	if metadata == nil {
		return ""
	}
	if v, ok := metadata[previousInteractionTriggerMetadataKey].(string); ok {
		if v == idleTrigger || v == endVotesTrigger {
			return v
		}
	}
	return ""
}

// readBoundedIDValue is the shared tolerant reader behind the two
// interaction-id metadata keys: "" when absent, non-string, or over
// interactionIDMaxBytes (never truncated — a clipped opaque token would key
// a different interaction).
func readBoundedIDValue(metadata map[string]any, key string) string {
	if metadata == nil {
		return ""
	}
	if v, ok := metadata[key].(string); ok {
		if len(v) > interactionIDMaxBytes {
			return ""
		}
		return v
	}
	return ""
}
