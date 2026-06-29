package server

import (
	"context"

	"github.com/mkhomutov/persatrix/internal/channels"
)

// channel_governance.go holds the server-side glue that stamps the default
// RFC 0030 governance bundle onto a group channel created at runtime through
// `POST /api/v1/channels`. Split out of channel_handlers.go (which sits at the
// 500-line review cap) so the create handler stays under it; the logic is pure
// orchestration of the router's per-subsystem setters, mirroring how startup
// resolves the same knobs separately in cmd/orchestrator/channels.go.

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
//   - the Layer 1 interaction cost ceiling — `ApplyDefaultInteractionBudget`
//     stamps the fleet default for the same reason (RFC 0050 amendment made it
//     router-held with the same meaningful-zero semantics as the reply budget;
//     `Set(_, 0)` would pin uncapped, not inherit). Without this seed a runtime
//     channel reads 0 instead of a non-zero `default_interaction_budget_tokens`
//     until the next restart re-runs ResolveInteractionBudgets — which would
//     also mis-report the GET /config effective value and let a first sparse
//     PATCH freeze the baseline at the wrong value (the ISSUE-0103 footgun).
//
// None of these has a REST field, so startup resolution re-forces them on
// restart; this runtime path keeps the channel governed in the meantime. A nil
// router (channels subsystem disabled) is a no-op.
//
// The RFC 0051 reasoning rung is governance-aware, so it reads whether the
// just-created channel has a salience-gated member off the store
// ([Server.channelHasSalienceGatedMember]) — the same live-membership signal the
// config apply path uses — to pick the go-live default rung.
func (s *Server) applyRuntimeGroupGovernance(ctx context.Context, canonicalID string) {
	if s.channelRouter == nil {
		return
	}
	s.channelRouter.SetFloorControl(canonicalID, true, 0)
	s.channelRouter.SetSalienceMaxChannelMembers(canonicalID, 0)
	s.channelRouter.ApplyDefaultReplyBudget(canonicalID)
	s.channelRouter.ApplyDefaultInteractionBudget(canonicalID)
	// RFC 0051 (v0.3.10) PR 6 go-live: a runtime-created group ships on the
	// GOVERNED default rung — `bid` if it has a salience-gated member, else `off` —
	// matching what startup's ResolveReasoning resolves it to, so the channel is
	// `bid`-by-default from creation (not a second-class `off` until the next
	// restart) and the GET /config surface reads an explicit entry rather than the
	// getter's package-default fallback. An explicit `off` stays a kill switch via
	// the apply path.
	rc := channels.DefaultReasoningConfig()
	if s.channelHasSalienceGatedMember(ctx, canonicalID) {
		rc.Mode = channels.GovernedDefaultReasoningMode
	}
	s.channelRouter.SetReasoning(canonicalID, rc)
	// RFC 0052 (v0.3.11): a runtime-created group ships with the disabled autonomous
	// default, so the GET /config surface reads an explicit entry rather than the
	// getter's package-default fallback (and a first sparse PATCH freezes the right
	// baseline). Autonomy is opt-in: the channel stays an ordinary one until armed.
	s.channelRouter.SetAutonomous(canonicalID, channels.DefaultAutonomousConfig())
}
