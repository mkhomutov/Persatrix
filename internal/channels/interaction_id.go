package channels

// interactionIDMetadataKey is the wire-level key for the RFC 0020
// `interaction_id` carried on the publish metadata bag and lifted onto the
// typed `ChannelMessageEvent.interaction_id` proto field ([RFC 0030
// governance layers]). Centralised so a future rename is one edit rather
// than a multi-callsite hunt — mirrors `cascadeDepthMetadataKey` and
// `participantTypeMetadataKey`.
//
// The deterministic governance layers (Layer 1 cost ceiling, Layer 2 reply
// budget, Layer 4 end-of-interaction votes) all attribute per interaction,
// so the id must survive the publish→fanout boundary the same way
// cascade_depth and participant_type do — `ChannelMessageEvent` has no
// metadata map, so a first-class field is required.
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
