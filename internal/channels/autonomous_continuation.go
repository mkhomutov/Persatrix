package channels

// autonomous_continuation.go — RFC 0052 §B productive-round continuation
// (ISSUE-0110). The floor-control amendment (RFC 0030 Layer 2.5) makes the
// round loop the SOLE dispatcher: an in-round speaker's reply is persisted but
// its fanout is suppressed (router_publish_async.go D1), because on a human
// channel the human reads the round and continues the conversation. On an
// `autonomous.enabled` channel there is no human — so a round in which the
// roster ACTUALLY REPLIED ended the discussion outright: the replies woke
// nobody, the stall tail no-oped (it reacts to silence, and the round
// replied), idle rotation is lazily evaluated on the next publish that never
// comes, and the interaction sat open forever with no synthesis. Exactly the
// arc RFC 0052 §Risk describes as "A's post wakes B, whose post wakes A…,
// bounded by the cascade-depth cap and the reply budget" — the wake was
// missing. First reproduced live 2026-07-20 (the first productive round an
// autonomous channel ever had on a real provider; the offline mock roster is
// always silent, so every prior round took the stall path).
//
// The seam: at the fanout tail of a PRODUCTIVE floor round on an armed
// channel, re-fan the round's last reply as the next open-floor stimulus —
// the exact wake the suppression deferred. Every shipped bound composes
// unchanged: the re-fanned chain advances the §D round tally at each tail
// (`max_rounds`), the wallet SOFT budget trigger reads the same tails, the
// reply budget meters each speaker, and the Layer-0 cascade-depth cap is
// honoured, never bypassed (§Risk) — when the would-be continuation stimulus
// sits AT the cap, the unattended discussion has crossed a terminal bound
// with no human to continue past it, so it takes the §D structural close
// (synthesis-armed on a chaired channel) instead of wedging open.
//
// SCOPE (RFC 0052 OQ #2): everything here is gated on `autonomous.enabled` —
// a human channel's productive round still ends with the round, byte-for-byte
// (pinned by TestHumanFloorRound_NoContinuation).

import (
	"context"

	"go.uber.org/zap"
)

// maybeContinueDiscussion is the continuation seam, called at the fanout tail
// on the floor path once the bounded close has ruled the round sub-bound and
// live (never on the closed/stale branches, so an armed synthesis window or a
// terminated discussion is never re-stimulated). A no-op unless the channel
// is armed and the round replied.
//
// The re-fan runs on a tracked detached goroutine (the PublishAsync spawn
// shape — recovered, in-flight-counted, drainable via fanoutWG; the Add runs
// on a goroutine already holding a fanout count, the drain-safe direction).
// The chain cannot recurse unboundedly: each continued round advances the §D
// tally toward `max_rounds`, spend advances toward the soft budget, and the
// reply's depth advances toward the Layer-0 cap — whichever bound crosses
// first ends the chain (the first two via [ChannelRouter.maybeBoundedClose]
// at the next tail, the cap via the close below). `ctx` is expected detached.
func (r *ChannelRouter) maybeContinueDiscussion(ctx context.Context, ct ChannelType, outcome floorRoundOutcome, a AutonomousConfig) {
	if !a.Enabled || outcome.replied == 0 || outcome.lastReply == nil {
		return
	}
	reply := *outcome.lastReply
	if readCascadeDepth(reply.Metadata) >= r.maxCascadeDepthFor(reply.ChannelID) {
		// Layer-0 terminal bound: continuing would bypass the cascade cap
		// (§Risk forbids it), and stopping silently would wedge the
		// interaction open forever. Close with the artifact instead.
		r.closeOnCascadeBound(ctx, reply, ct)
		return
	}
	r.logger.Debug("channels: autonomous continuation re-fans the round's last reply",
		zap.String("channel_id", reply.ChannelID),
		zap.String("message_id", reply.ID),
		zap.String("sender_id", reply.SenderID))
	r.fanoutInFlight.Add(1)
	r.fanoutWG.Add(1)
	go func() {
		defer r.fanoutWG.Done()
		defer r.fanoutInFlight.Add(-1)
		defer r.recoverFanout("autonomous_continuation", reply.ChannelID, reply.ID)
		r.fanout(ctx, reply, ct, r.resolveThreadParentSenderID(ctx, reply))
	}()
}

// closeOnCascadeBound runs the §D structural close for an armed channel whose
// discussion chain reached the Layer-0 cascade-depth cap — the terminal bound
// nobody can continue past on an unattended channel. Two call sites, one per
// path that can strand the chain: the continuation seam above (a floor
// round's last reply at the cap) and the publishCommit cap branch (an at-cap
// NON-floor-speaker publish on the concurrent path, whose fanout suppression
// would otherwise leave the interaction immortal-but-inert).
//
// Reuses the bounded-close machinery end to end: the chair synthesis arm
// (goal-directed artifact) with the immediate artifact-bearing close as its
// fallback, the tombstone CAS (a racing end-vote or sibling close wins
// benignly), and the `structural` trigger label. `redelivery` is false — the
// closing message was never live-delivered (an in-round reply is
// floor-speaker-suppressed; a capped concurrent publish never fanned), so the
// close notification is its sole delivery and receivers ingest it as the
// record's final turn.
//
// A mid-flight RFC 0050 disable is honoured (operator took manual control →
// leave the interaction open). [boundStandsAgainst]'s `max_rounds`-raise
// extension deliberately does NOT apply: a raise extends a structural ROUND
// crossing, not a depth crossing — more rounds cannot lower the chain's depth.
func (r *ChannelRouter) closeOnCascadeBound(ctx context.Context, msg ChannelMessage, ct ChannelType) {
	fresh := r.AutonomousFor(msg.ChannelID)
	if !fresh.Enabled {
		return
	}
	stampedID := readInteractionID(msg.Metadata)
	if stampedID == "" {
		return // untracked publish; nothing to close.
	}
	members, err := r.store.GetMembers(ctx, msg.ChannelID)
	if err != nil {
		// Degraded: with no member list the synthesis arm cannot dispatch
		// (synthesisUnavailable) and the immediate close still notifies via
		// the close fan's own member read. Log and proceed.
		r.logger.Warn("channels: cascade-bound close member lookup failed; closing without synthesis arm",
			zap.String("channel_id", msg.ChannelID), zap.Error(err))
		members = nil
	}
	r.logger.Info("channels: autonomous discussion reached the cascade-depth cap; running the structural close",
		zap.String("channel_id", msg.ChannelID),
		zap.String("interaction_id", stampedID),
		zap.Int("max_cascade_depth", r.maxCascadeDepthFor(msg.ChannelID)))
	notify := closeNotify{}
	switch r.maybeArmSynthesisClose(ctx, msg, ct, members, len(members), stampedID, structuralTrigger, notify, fresh) {
	case synthesisArmed, synthesisAlreadyArmed:
		return // the close lands on the chair's claimed reply (or the timeout net).
	case synthesisEntryMovedOn, synthesisUnavailable:
		// Fall through to the immediate artifact-bearing close; the tombstone
		// CAS below keeps a racing closer single-shot.
	}
	r.boundedClose(ctx, msg, ct, stampedID, structuralTrigger, notify)
}
