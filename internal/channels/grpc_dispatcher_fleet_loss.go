package channels

// grpc_dispatcher_fleet_loss.go — ISSUE-0125: telling "this member is not
// registered" apart from "nothing at all is registered".
//
// Split out of grpc_dispatcher.go, which reached the 500-line review cap when
// the ISSUE-0124 attribution write landed (the interaction_close_latch.go
// precedent). A pure move: this is the whole of the fleet-loss signal — the
// optional resolver half it needs, the probe, and the re-arm — and nothing in
// it is part of the dispatch mechanics the parent file describes. The
// `fleetLossReported` flag stays on the struct there, where the rest of the
// dispatcher's state is declared.

import (
	"context"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/registry"
)

// fleetLister is the OPTIONAL second half of [AgentResolver], satisfied by the
// production *registry.InMemoryRegistry and used for one thing only: telling
// "this member is not registered" apart from "nothing at all is registered".
//
// Kept as a type assertion rather than a required method on [AgentResolver] so
// a resolver that cannot enumerate itself (a stub, a scoped view) still works —
// it simply gets no fleet-loss signal. See [GRPCMessageDispatcher.probeFleetLoss].
type fleetLister interface {
	List(ctx context.Context) ([]registry.AgentInfo, error)
}

// probeFleetLoss reports, at ERROR and once per outage, that the orchestrator
// holds ZERO registered agents while a channel still has members to deliver to.
//
// This is the signal ISSUE-0125 asks for on its own merit. The per-recipient
// "dispatch target not registered" WARN in [GRPCMessageDispatcher.Dispatch]
// sits at warn deliberately — one unregistered member is the normal cost of a
// standing human participant or a mistyped channels.yaml membership, and
// reddening the logs for that would make the level useless. But the SAME line is all an operator gets when an
// orchestrator restart has emptied the registry and every single dispatch in the
// deployment is being dropped: /healthz is green, containers are up, publishes
// return 201, and the personas are simply mute. The two cases are distinguished
// here by the only thing that separates them — whether anything at all is
// registered.
//
// Runs on the miss path only, which is already the slow, rare branch, so the
// whole-directory List costs nothing on the delivery path.
func (d *GRPCMessageDispatcher) probeFleetLoss(ctx context.Context, channelID string) {
	lister, ok := d.resolver.(fleetLister)
	if !ok {
		return
	}
	agents, err := lister.List(ctx)
	if err != nil {
		// Never upgrade a delivery miss into a fleet-loss claim on a failed
		// probe — the claim is the whole value of the line.
		return
	}
	if len(agents) > 0 {
		d.noteFleetAlive() // this outage (if any) is over
		return
	}
	if d.fleetLossReported.Swap(true) {
		return // already reported for this outage
	}
	d.logger.Error("channels: orchestrator has zero registered agents while channels have members — the whole fleet is unreachable and every dispatch is being dropped (agents register at their own startup; see ISSUE-0125)",
		zap.String("channel_id", channelID),
	)
}

// noteFleetAlive re-arms the fleet-loss ERROR, so the NEXT outage is reported
// as its own event rather than deduplicated away against the last one.
//
// It must be called from somewhere a healthy deployment actually reaches.
// Clearing the flag only inside probeFleetLoss is not enough: that runs on the
// resolver-miss path, so a recovery in which every channel member registers
// again produces no miss, never clears the flag, and silently downgrades the
// signal to once per process — the exact thing the field's contract disclaims.
// A resolve that SUCCEEDED is the proof a healthy deployment does produce, and
// it is the one this dispatcher already has in hand.
//
// The Load guard keeps the steady state a read: without it every delivery would
// write a shared cache line for a flag that is almost always already false.
func (d *GRPCMessageDispatcher) noteFleetAlive() {
	if d.fleetLossReported.Load() {
		d.fleetLossReported.Store(false)
	}
}
