package channels

// MemberPolicy is the resolved per-member respond-policy triple — the
// canonical legacy `Policy` plus the RFC 0030 Tier B salience signals
// derived from the member's *declared* disposition. It is what a write
// boundary actually persists / puts on the wire: `respond_policy` +
// `memberships.threshold` / `salience_gated` (and the matching
// `ChannelMessageEvent` fields). The named triple exists so the
// derive-then-normalize pairing travels as one value instead of three
// loose locals re-assembled per call site.
//
// Deliberately NOT embedded in [Member], even though Member carries the
// same three fields: MemberPolicy.Policy is canonical by construction
// (only [ResolveMemberPolicy] makes one), while Member.RespondPolicy at
// the CreateChannelWithMembers boundary legally holds an empty or
// declared value the store itself defaults and normalizes. Embedding
// would stamp the "resolved" invariant on a field that isn't.
type MemberPolicy struct {
	Policy        RespondPolicy
	SalienceGated bool
	Threshold     *float64
}

// ResolveMemberPolicy is the single write-boundary constructor for
// [MemberPolicy]: it derives the Tier B salience signals from the
// *declared* disposition ([ResolveSalienceSignal]), then normalizes and
// validates it ([canonicalRespondPolicy]). An unknown declared value is
// rejected with [ErrInvalidRespondPolicy] and the zero MemberPolicy.
//
// The internal ordering is the point. Deriving after normalization would
// silently lose a `participant`'s bid-ness (its canonical `always` form,
// declared bare, does not bid) — an ordering constraint every call site
// previously had to remember on its own. Callers that resolve a declared
// disposition into persisted/wire state (the store's AddMember /
// SetMemberPolicy, the REST create handler) go through here; the config
// loader ([MemberConfig.UnmarshalYAML]) keeps the non-validating pair
// because it deliberately defers rejection to [Config.Validate], which
// reports the channel/member index alongside the bad value.
//
// There is deliberately no explicit-threshold parameter: every boundary
// that resolves through here carries only the disposition, and the one
// source of operator thresholds (the config loader) bypasses the
// constructor. Accepting a threshold without also enforcing the
// combination rule [Config.Validate] applies (a threshold is only legal
// on a member that can bid) would let an invalid pair persist silently —
// if a wire shape ever grows a threshold field, add the parameter and
// that rule together.
func ResolveMemberPolicy(declared RespondPolicy) (MemberPolicy, error) {
	salienceGated, threshold := ResolveSalienceSignal(declared, nil)
	canonical, err := canonicalRespondPolicy(declared)
	if err != nil {
		return MemberPolicy{}, err
	}
	return MemberPolicy{
		Policy:        canonical,
		SalienceGated: salienceGated,
		Threshold:     threshold,
	}, nil
}
