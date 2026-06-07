package channels

// router_salience.go holds the RFC 0030 Tier B (v0.3.8) per-channel salience-bid
// channel-size cap registry — the setter/getter the router exposes and the
// hot-path lookup the fanout uses to stamp the cap onto each dispatch envelope.
// Split out of router.go so that file stays under the 500-line review cap,
// mirroring how router_session.go / router_reconcile.go carved their concerns
// off the router topology. The startup resolver ([ChannelRouter.ResolveSalienceCaps])
// lives next to its floor-control sibling in floor_control.go.
//
// The `salienceMu` mutex + `salienceMaxMembers` map are declared on [ChannelRouter]
// in router.go (a struct field must live with the type); only the methods
// move here.

// SetSalienceMaxChannelMembers resolves the Tier B channel-size cap for
// `channelID`. A non-positive `maxMembers` normalizes to
// [DefaultSalienceMaxChannelMembers] so a zero/absent config row cannot silently
// disable the cap. Wired two ways like [ChannelRouter.SetFloorControl]: at
// startup via [ChannelRouter.ResolveSalienceCaps] and at runtime when a group
// channel is created through `POST /api/v1/channels`. The mutex makes the
// runtime call safe concurrently with traffic.
func (r *ChannelRouter) SetSalienceMaxChannelMembers(channelID string, maxMembers int) {
	if maxMembers <= 0 {
		maxMembers = DefaultSalienceMaxChannelMembers
	}
	r.salienceMu.Lock()
	defer r.salienceMu.Unlock()
	r.salienceMaxMembers[channelID] = maxMembers
}

// salienceMaxFor returns the resolved Tier B channel-size cap for `channelID`. A
// channel with no resolved entry falls back to [DefaultSalienceMaxChannelMembers]
// — the same value the agent-side seam would default to — so the wire field is
// always a sensible positive cap.
func (r *ChannelRouter) salienceMaxFor(channelID string) int {
	r.salienceMu.Lock()
	defer r.salienceMu.Unlock()
	if m, ok := r.salienceMaxMembers[channelID]; ok {
		return m
	}
	return DefaultSalienceMaxChannelMembers
}

// SalienceMaxChannelMembersFor reports the resolved Tier B channel-size cap for
// `channelID` and whether an explicit entry was set (`set` false means the
// default applies). Exposed for tests and ops introspection, mirroring
// [ChannelRouter.FloorControlFor]; the hot path reads [ChannelRouter.salienceMaxFor].
func (r *ChannelRouter) SalienceMaxChannelMembersFor(channelID string) (maxMembers int, set bool) {
	r.salienceMu.Lock()
	defer r.salienceMu.Unlock()
	m, ok := r.salienceMaxMembers[channelID]
	if !ok {
		return DefaultSalienceMaxChannelMembers, false
	}
	return m, true
}
