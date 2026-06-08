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
// NOTE: unlike `participantTypeMetadataKey` (written by the REST chat
// handler), no producer writes this key yet — RFC 0020 interaction tracking
// is agent-side and there is no orchestrator-side resolver, so `readInteractionID`
// currently always returns "" (the untracked case). The key + helper are the
// inert substrate; the producer lands with the layer PRs that consume it.
//
// [RFC 0030 governance layers]: ../../docs/rfcs/0030-governance-layers-pr-plan.md
const interactionIDMetadataKey = "interaction_id"

// interactionIDMaxBytes bounds the inbound interaction_id by UTF-8 byte length. The
// id is an attacker-influenceable opaque token off the untrusted publish
// metadata bag, and the layer PRs that consume it (Layer 2 reply budget,
// Layer 4 end-of-interaction votes) key per-interaction maps on the value —
// an unbounded id is an unbounded map-key growth vector (the same concern
// floor_control.go calls out for its session maps). 128 mirrors the agent
// receive path's `_CHANNEL_THREAD_ID_MAX_CHARS` cap and leaves generous
// headroom over the RFC 0020 id (a 36-char uuid4 / 26-char ULID).
//
// This publish-side bound is one of two: the value is also seeded onto agent
// metadata at the receive boundary, where the per-interaction map key is
// actually created, so the same byte cap is enforced there too
// (`_INTERACTION_ID_MAX_BYTES` in agents/channel_wire_metadata.py). A
// publish-only bound would leave a non-Go / compromised producer's oversized
// id riding straight onto that metadata unbounded.
const interactionIDMaxBytes = 128

// readInteractionID extracts the inbound interaction_id from a publish
// metadata bag. Returns "" when absent, non-string, or longer than
// interactionIDMaxBytes — a malformed or oversized claim is treated as the
// untracked case (every governance layer stays at its uncapped default)
// rather than failing the dispatch, mirroring readParticipantType's
// tolerance. Over-length falls back to empty rather than truncating: a
// clipped opaque token would key a *different* interaction, which is worse
// than treating the publish as untracked.
func readInteractionID(metadata map[string]any) string {
	if metadata == nil {
		return ""
	}
	if v, ok := metadata[interactionIDMetadataKey].(string); ok {
		if len(v) > interactionIDMaxBytes {
			return ""
		}
		return v
	}
	return ""
}
