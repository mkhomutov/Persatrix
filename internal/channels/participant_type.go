package channels

// participantTypeMetadataKey is the wire-level key for the sender's peer
// type ("user" | "agent") carried on the publish metadata bag and lifted
// onto the typed `ChannelMessageEvent.sender_participant_type` proto
// field ([RFC 0011 participant-type amendment]). Centralised so a future
// rename is one edit rather than a multi-callsite hunt — mirrors
// `cascadeDepthMetadataKey`.
//
// The producer side (the REST chat handler in `internal/server`) writes
// this key with the same literal; the proto-roundtrip + dispatcher tests
// pin the end-to-end contract so the two stay in agreement.
//
// [RFC 0011 participant-type amendment]: ../../docs/rfcs/0011-amendment-participant-type-wire-propagation.md
const participantTypeMetadataKey = "participant_type"

// readParticipantType extracts the inbound participant_type from a
// publish metadata bag. Returns "" when absent or non-string — a
// malformed claim is treated as the genuine agent-to-agent case (the
// agent-side read path resolves an empty value to "agent") rather than
// failing the dispatch, mirroring readCascadeDepth's tolerance.
func readParticipantType(metadata map[string]any) string {
	if metadata == nil {
		return ""
	}
	if v, ok := metadata[participantTypeMetadataKey].(string); ok {
		return v
	}
	return ""
}
