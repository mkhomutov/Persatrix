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

// readInteractionID extracts the inbound interaction_id from a publish
// metadata bag. Returns "" when absent or non-string — a malformed claim
// is treated as the untracked case (every governance layer stays at its
// uncapped default) rather than failing the dispatch, mirroring
// readParticipantType's tolerance.
func readInteractionID(metadata map[string]any) string {
	if metadata == nil {
		return ""
	}
	if v, ok := metadata[interactionIDMetadataKey].(string); ok {
		return v
	}
	return ""
}
