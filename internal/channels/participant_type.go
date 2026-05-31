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

// validParticipantTypes is the canonical peer-type vocabulary, the Go
// anchor mirroring the Python `participant.VALID_PARTICIPANT_TYPES`
// frozenset. The agent-side relationship tier honours only these two
// values (`record_close.py::extract_peer_from_interaction` clamps any
// other to "agent"); external boundaries validate against this set so an
// out-of-vocabulary value is rejected loudly rather than silently
// degraded.
var validParticipantTypes = map[string]struct{}{
	"agent": {},
	"user":  {},
}

// IsValidParticipantType reports whether t is a recognised peer type
// ("agent" | "user"). The REST chat handler uses it to reject an explicit
// out-of-vocabulary `participant_type` at the request boundary, matching
// the gRPC SendChatMessage servicer's `validate_participant_type` guard.
// The empty string is NOT valid here: callers apply their own default
// (REST chat defaults an omitted field to "user") before validating.
func IsValidParticipantType(t string) bool {
	_, ok := validParticipantTypes[t]
	return ok
}

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
