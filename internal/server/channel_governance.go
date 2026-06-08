package server

// channel_governance.go holds the server-side glue that stamps the default
// RFC 0030 governance bundle onto a group channel created at runtime through
// `POST /api/v1/channels`. Split out of channel_handlers.go (which sits at the
// 500-line review cap) so the create handler stays under it; the logic is pure
// orchestration of the router's per-subsystem setters, mirroring how startup
// resolves the same three knobs separately in cmd/orchestrator/channels.go.

// applyRuntimeGroupGovernance gives a freshly-created group channel the same
// governance a config-declared one gets at startup, so a runtime channel is not
// a second-class citizen until the next restart re-resolves it:
//
//   - floor control (RFC 0030 Layer 2.5) — default ON for groups;
//   - the Tier B salience channel-size cap — `Set(_, 0)` applies the default;
//   - the Layer 2 per-participant reply budget — `ApplyDefaultReplyBudget`
//     stamps the fleet default (a distinct call because reply-budget zero is
//     uncapped-as-a-value, so it cannot ride the salience-style `Set(_, 0)`
//     sentinel).
//
// None of the three has a REST field, so startup resolution re-forces them on
// restart; this runtime path keeps the channel governed in the meantime. A nil
// router (channels subsystem disabled) is a no-op.
func (s *Server) applyRuntimeGroupGovernance(canonicalID string) {
	if s.channelRouter == nil {
		return
	}
	s.channelRouter.SetFloorControl(canonicalID, true, 0)
	s.channelRouter.SetSalienceMaxChannelMembers(canonicalID, 0)
	s.channelRouter.ApplyDefaultReplyBudget(canonicalID)
}
