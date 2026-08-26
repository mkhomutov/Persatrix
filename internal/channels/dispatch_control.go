package channels

// dispatch_control.go — the [dispatchMarker] enum and its [dispatchControl]
// wrapper: which orchestrator-authored control marker (and marker payload) a
// [ChannelRouter.dispatchTo] call stamps on its envelope. Split out of
// fanout.go when PR 4b-ii's dispatchControl pushed that file past the 500-line
// review cap (`scripts/checks/file_size.py --strict`), the dispatch_envelope.go
// precedent — the marker seam changes on the amendments' cadence, fanout on the
// routing's.

// dispatchMarker selects which orchestrator-authored control marker, if
// any, a dispatch stamps on its envelope. The markers' never-alias
// invariant ([DispatchEnvelope.ChairEscalation] vs
// [DispatchEnvelope.InteractionCloseNotification]) was previously held by
// call-site discipline over two adjacent positional bools — `..., true,
// false)` against `..., false, true)`, which a silent transposition
// defeats with no compile error (PR #613 review). One enum value cannot
// set two envelope bools, so the aliased state is unrepresentable at this
// seam, and the next amendment adds a constant instead of widening every
// call site.
type dispatchMarker uint8

const (
	// markerNone is every ordinary dispatch: fanout, floor turns.
	markerNone dispatchMarker = iota
	// markerChairEscalation is the CE3 forced turn, stamped only by
	// [ChannelRouter.maybeEscalateStall]'s dispatch.
	markerChairEscalation
	// markerCloseNotification is the CP2 end-vote close notification,
	// stamped only by [ChannelRouter.notifyInteractionClose]'s dispatches.
	markerCloseNotification
	// markerChairEscalationResynthesize is the ISSUE-0099 second forced turn,
	// stamped only by [ChannelRouter.dispatchResynthesizeMisfire]. It
	// is a REFINEMENT of markerChairEscalation, not a peer: dispatchTo stamps
	// BOTH `ChairEscalation` and `ChairEscalationResynthesize` for it, so the
	// admission lift (which keys on ChairEscalation) is unchanged and only the
	// framing flips. The never-alias invariant the enum protects is between
	// {ChairEscalation, CloseNotification}; a resynthesize marker still never
	// sets CloseNotification, so that invariant holds.
	markerChairEscalationResynthesize
	// markerConvene is the RFC 0052 §B convene forced turn, stamped only by
	// [ChannelRouter.ConveneChannel]'s dispatch. It is its OWN directed lane,
	// not a refinement of any other marker: dispatchTo stamps only `Convene`
	// for it, so the never-alias invariant between {ChairEscalation,
	// CloseNotification, Convene} holds — a convene dispatch never sets a
	// chair-escalation or close-notification flag, and vice versa.
	markerConvene
	// markerSynthesisTurn is the RFC 0052 §D synthesis forced turn (PR 4b-ii),
	// stamped only by [ChannelRouter.maybeArmSynthesisClose]'s dispatch to the
	// escalation chair. Its own directed lane, the convene shape: dispatchTo
	// stamps only `SynthesisTurn` for it, extending the never-alias invariant
	// across all four control markers.
	markerSynthesisTurn
)

// dispatchControl bundles a dispatch's control marker with the two
// close-notification wire values that ride beside it (PR 4b-ii): the truthful
// bounded-close cause and the already-delivered-live redelivery flag. The
// extras are honoured ONLY with [markerCloseNotification] — [dispatchTo] stamps
// them structurally under that marker alone, so a stray value on an ordinary
// dispatch is unrepresentable on the wire (the [dispatchMarker] never-alias
// discipline, extended to the marker's payload). The zero value is an ordinary
// unmarked dispatch.
type dispatchControl struct {
	marker dispatchMarker
	// closeTrigger is the RFC 0052 bounded close's truthful cause
	// ("structural" | "cost"), empty for the end-vote close and every
	// non-notification dispatch — its presence is the receiver's OQ #6
	// metering key, so it must never widen past the bounded close.
	closeTrigger string
	// closeRedelivery marks the closing message as already delivered live
	// via ordinary fanout (the floor-path bounded close), so the receiver
	// skips the duplicate final-turn ingest.
	closeRedelivery bool
	// respondersTurn marks an ORDINARY ([markerNone]) dispatch to a recipient
	// [orderResponders] elected to answer — the concurrent fanout's responder
	// subset and the floor round's granted speaker. Meaningless under a
	// control marker, which answers the question by itself (see
	// [dispatchControl.expectsReply]), and false by default so a new call
	// site is ingestion-only until it says otherwise: the write it gates
	// fails closed, so silence must mean "do not attribute".
	respondersTurn bool
}

// expectsReply reports whether the orchestrator asked this recipient for a
// turn, which is what [DispatchEnvelope.ExpectsReply] carries to the dispatch
// chokepoint. The distinction is NOT "did the message arrive": the fanout
// delivers to members whose reply the receiver gate suppresses so they still
// ingest the room (fanout.go's "un-addressed participants amnesiac" note), and
// a stimulus nobody will answer must not leave a causal-attribution entry
// behind for that agent's next, unrelated publish to inherit.
//
// The control markers answer for themselves — the four FORCED turns exist to
// draw a reply, and the close notification is told rather than asked — so only
// the ordinary lane consults `respondersTurn`. Written as an exhaustive switch
// on the marker rather than a boolean expression so the next marker added to
// the enum has to state which lane it is in.
func (c dispatchControl) expectsReply() bool {
	switch c.marker {
	case markerNone:
		return c.respondersTurn
	case markerCloseNotification:
		return false
	case markerChairEscalation, markerChairEscalationResynthesize, markerConvene, markerSynthesisTurn:
		return true
	default:
		// Unreachable while the enum is exhaustive above. Fail closed: an
		// unrecognised marker attributes nothing rather than guessing.
		return false
	}
}

// closeNotificationWireFields returns the two typed close-notification wire
// values ([dispatchTo] stamps them on the envelope) — but ONLY under
// [markerCloseNotification]. Gating the WHOLE extras payload once here (rather
// than re-spelling the marker check per field at the call site) is the
// structural half of the [dispatchControl] extras contract: a stray trigger or
// redelivery flag on any other dispatch is unrepresentable on the wire. Off the
// marker it returns the proto3 zero pair — the pre-4b-ii wire shape.
func (c dispatchControl) closeNotificationWireFields() (trigger string, redelivery bool) {
	if c.marker != markerCloseNotification {
		return "", false
	}
	return c.closeTrigger, c.closeRedelivery
}
