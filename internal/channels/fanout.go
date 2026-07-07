package channels

import (
	"context"
	"errors"
	"slices"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/registry"
)

// channelFanoutMaxConcurrency caps the number of in-flight per-recipient
// dispatches inside [ChannelRouter.fanout]. Without a cap, a publish to an
// N-member channel where each Dispatch costs `channelFanoutPerRecipientTimeout`
// (5s) would block the publish path for O(N × 5s) on a stalled tail; the cap
// keeps worst-case publish latency at O(ceil(N / limit) × 5s) while bounding
// goroutine pressure at the publish call site. 16 is sized for v0.3.0 group-
// channel sizes (≤ 50 members typical) — review when channels grow large
// enough to make ceil(N/16) slow paths visible.
const channelFanoutMaxConcurrency = 16

// channelFanoutPerRecipientTimeout caps how long a single per-recipient
// dispatch can block before the router moves on. With the PR-4 gRPC
// dispatcher live, this is the deadline propagated into
// `AgentService.ReceiveChannelMessage`; the receiver is contractually
// fire-and-forget so the deadline only protects against a stuck dial.
const channelFanoutPerRecipientTimeout = 5 * time.Second

// fanout looks up subscribers, filters the sender, and dispatches the
// message to the remaining recipients. It chooses between two paths:
//
//   - **Serialized floor round** (RFC 0030 Layer 2.5) when floor control is
//     enabled for the channel and there are ≥2 candidate responders: the
//     responders take the floor one at a time, each reading the prior
//     speaker's reply (see [ChannelRouter.floorRound]). Non-responders are
//     delivered fire-and-forget for memory ingestion only, off the floor.
//   - **Concurrent fanout** otherwise (flag off, DM, or a single responder):
//     the pre-amendment path — every non-sender, non-`never` member is
//     dispatched with bounded concurrency (ISSUE-0014).
//
// Either way the publish call BLOCKS on fanout completion — Publish only
// returns once fanout is done. For the concurrent path that is bounded at
// O(ceil(N / `channelFanoutMaxConcurrency`) × `channelFanoutPerRecipientTimeout`);
// for the floor path it is the round duration (responders go serial, each
// bounded by the per-turn timeout) — the documented latency trade.
//
// Detaches the request context (`context.WithoutCancel`) so a client
// disconnect mid-fanout does not silently drop later subscribers — the
// HTTP response shape is no longer the caller's deadline by the time we are
// here.
//
// History: PR #245 added a per-recipient timeout to fix intra-publish
// starvation; ISSUE-0014 added the concurrency bound once the PR-4 gRPC
// dispatcher made the worst-case tail visible.
func (r *ChannelRouter) fanout(ctx context.Context, msg ChannelMessage, ct ChannelType, threadParentSenderID string) {
	members, err := r.store.GetMembers(ctx, msg.ChannelID)
	if err != nil {
		r.logger.Warn("channels: fanout member lookup failed",
			zap.String("channel_id", msg.ChannelID),
			zap.Error(err))
		return
	}

	// RFC 0030 Layer 2.5: serialize when floor control is on for this channel
	// and the candidate responder set is large enough to overlap. A DM or a
	// single responder cannot collide, so it falls through to the concurrent
	// path with no per-turn latency.
	// RFC 0030 Tier B (v0.3.8): the channel's member count rides every dispatch
	// so the agent-side seam can apply the TB6 channel-size cap. A per-publish
	// value (identical across recipients), captured once here.
	channelSize := len(members)

	// Floor-capable-directedness amendment (v0.3.8): resolve the suppression
	// basis once per publish and stamp it on every recipient's envelope. The
	// debug line surfaces the previously-silent resolution — mentions were
	// present but named no floor-capable member (the human operator, an
	// observer, a non-member, the sender itself), the case PR 2/2's gate
	// flip reclassifies to open floor (amendment §D). An explicit
	// `@everyone` broadcast is excluded: the sentinel always falls out of
	// the intersection, but a broadcast is open floor by contract (D3), not
	// a reclassification — logging it would mislabel every broadcast and
	// bury the signal this line exists to surface.
	floorMentions := resolveFloorMentions(members, msg.Mentions, msg.SenderID)
	// The previously-silent resolution: mentions were present but named no
	// floor-capable member (the operator, an observer, a non-member, the sender
	// itself), the case the gate flip reclassifies to open floor. An explicit
	// `@everyone` is open floor by contract (D3), not a reclassification.
	namedNoFloorCapable := len(msg.Mentions) > 0 && len(floorMentions) == 0 && !slices.Contains(msg.Mentions, MentionEveryone)
	if namedNoFloorCapable {
		r.logger.Debug("channels: mentions name no floor-capable member",
			zap.String("channel_id", msg.ChannelID),
			zap.String("message_id", msg.ID),
			zap.Int("mentions", len(msg.Mentions)))
	}
	// ISSUE-0099: `misfired` is the one publish-time-PROVABLE escalation
	// failure — the chair's forced-turn reply handed off to a target that
	// reached nobody. A misfired hand-off is a BARE message (outcome b); a
	// publish carrying an `end_interaction_vote` is the chair's synthesis
	// (outcome a), NOT a hand-off, even when it @-mentions the still-outstanding
	// voice — the framing invites "@-mention the missing voice inside your
	// vote's `content`", and that voice is routinely non-floor-capable (the
	// operator, or the observer the residue targets). Re-forcing on a vote would
	// inject a second synthesize-only turn after the chair already voted, so the
	// vote disarms the trigger (it still consumes the stash below) without
	// re-forcing.
	misfired := namedNoFloorCapable && !readEndInteractionVote(msg.Metadata)

	// The RFC 0052 autonomous block, resolved ONCE per fanout and threaded to
	// every consumer below — the synthesis-reply claim, the head staleness
	// check, and the tail trigger — so they read a single snapshot (the
	// [ChannelRouter.dispatchTo] ReasoningFor posture: separate reads could be
	// torn by a concurrent RFC 0050 apply landing between them) and the
	// per-publish autonomousMu traffic stays one acquisition in this scope.
	autonomous := r.AutonomousFor(msg.ChannelID)
	// RFC 0052 §D close-on-reply (PR 4b-ii): the chair's synthesis reply never
	// reaches this fanout — [ChannelRouter.publishCommit] claims it on the
	// COMMIT path, before the end-vote hook, and suppresses its fanout plan
	// entirely (PR #718 review: the claim lived at this head, where a reply
	// cast AS an end vote — the shape the directive invites — was consumed by
	// processEndVote first: spam-suppressed into the timeout net, or closed as
	// the unmetered `end_votes` shape).

	// ISSUE-0099 resynthesize, CLAIM half — at the fanout HEAD, before any floor
	// round can park it (PR 4b-i review round 5): the once-bound is "the chair's
	// FIRST publish after the forced turn consumes the arm". Claiming at the tail
	// (the round-4 shape) let two chair publishes on different path shapes invert
	// by a whole multi-turn floor round — the real forced-turn reply parked in
	// its round while a later innocuous chair message raced down the fast
	// concurrent path, reached its tail first, and claimed the arm with ITS
	// misfire flag. The head claim shrinks that inversion window to detached-
	// goroutine spawn jitter (PublishAsync fans on a detached goroutine, so
	// strict commit order is not literally guaranteed) — the pre-4b-i posture,
	// where the window was never observed to bite. Run for EVERY chair publish
	// in an escalated interaction (a synthesis reply was already claimed at the
	// commit path and never fans, so it cannot half-claim escalation state
	// here), not only the misfiring ones, so a clean hand-off disarms
	// the trigger (see [ChannelRouter.claimResynthesizeMisfire]); a no-op (nil)
	// for every non-chair publish. Only the re-force DISPATCH waits for the
	// tail, gated on the bounded-close outcome below.
	pendingResynth := r.claimResynthesizeMisfire(msg, misfired)

	// Mark the responders as having an in-flight turn for the console presence
	// signal (RFC 0048 Tier 1) — exactly the members [orderResponders] expects
	// to reply, not the ingestion-only recipients dispatchConcurrent also
	// delivers to (marking those would strand a "thinking" line on a member that
	// will never answer). Cleared per-member when its reply re-enters
	// (publishCommit), by the TTL backstop, or — on the concurrent path's
	// bounding/stale rounds, whose dispatch is withheld so no reply can ever
	// re-enter — by the withhold seam below. See activity.go.
	responders, nonResponders := orderResponders(members, msg, threadParentSenderID)
	respIDs := responderIDs(responders)
	r.markActivity(msg.ChannelID, respIDs)

	// The two dispatch paths (floor round vs concurrent), resolved once so the
	// RFC 0052 §D bounded close below has ONE call site serving both.
	settings, floorOn := r.floorSettingsFor(msg.ChannelID)
	floorPath := floorOn && settings.enabled && len(responders) >= 2
	// The floor path's HEAD staleness check (PR #716 review): the concurrent
	// path's close-before-dispatch ordering runs its ledger read at the tail,
	// BEFORE its dispatch — but the floor round runs before that tail, so a
	// deliberate close landing between this publish's commit and this detached
	// fanout would still dispatch every responder's LLM turn into the
	// terminated discussion. Withhold the round up front instead; the head
	// verdict then rules the stimulus stale for the whole fanout (the tail
	// trigger is skipped below — its ledger read would only re-derive the
	// same verdict) and the revival tails are withheld, exactly as on the
	// concurrent path. Like the concurrent withhold, the message is committed
	// history that reaches no member (the non-responder ingestion delivery is
	// withheld with the round) — the documented KNOWN COST, same 4b-ii
	// redelivery-marker vehicle.
	staleAtHead := floorPath && r.stimulusOutlivedClose(msg, autonomous)
	var outcome floorRoundOutcome
	// undelivered collects the round's per-recipient live-delivery failures
	// (live_delivery.go) — the bounded close below downgrades exactly those
	// members' notifications to sole delivery (PR #718 review). Always nil on
	// the concurrent path, whose close-before-dispatch ordering makes the
	// notification the sole delivery for everyone.
	var undelivered map[string]struct{}
	if floorPath && !staleAtHead {
		outcome, undelivered = r.floorRound(ctx, msg, ct, threadParentSenderID, responders, nonResponders, settings.turnTimeout, channelSize, floorMentions)
	}

	// RFC 0052 §D bounded close, at the fanout tail on BOTH paths — one call
	// site (review round 5), with the per-path ordering the deep review pinned
	// preserved by POSITION relative to each path's dispatch:
	//
	//   - FLOOR path: the close runs AFTER the round (floorRound above), so the
	//     `max_rounds`-th round's discussion happens and then the interaction is
	//     at its terminal bound and must CLOSE, not be revived. The 4b-i
	//     duplicate-final-turn cost is gone (PR 4b-ii): when this path's close
	//     notification carries a stimulus the round already delivered live, it
	//     is stamped with the typed `close_notification_redelivery` wire field
	//     and receivers close without re-ingesting it (close_notification.py).
	//   - CONCURRENT path: the close runs BEFORE the dispatch — unlike the floor
	//     path, whose round replies are suppressed as floor speakers, a
	//     concurrent dispatch's replies re-enter Publish, so dispatching the
	//     bounding stimulus first would let those replies re-fan (the resolver's
	//     post-close claim latch, router_publish_async.go, now also suppresses
	//     them — this ordering keeps the wasted dispatch and its LLM spend off
	//     the wire in the first place). Not a corner case: any autonomous round
	//     with a single responder (e.g. a two-persona roster) takes this path
	//     even under floor control. The bounding message is still persisted, and
	//     the close notification delivers it as the marked control event a
	//     member with an OPEN scope ingests as its record's final turn before
	//     closing (close_notification.py). A member with NO open scope would
	//     no-op instead of fabricating a 1-turn record — which is why the close
	//     never fires before the interaction's first live dispatch
	//     (maybeBoundedClose's round-1 guard, PR #716 review): even
	//     `max_rounds = 1` delivers the opening turn live and closes on the next
	//     tail, so every close leaves an artifact (§D).
	//
	// A `closed` return means the id is retired, so every follow-on that could
	// revive the interaction is skipped: the stall escalation (a forced chair
	// turn's reply would mint fresh and reopen), the concurrent stimulus
	// dispatch, and the pending ISSUE-0099 re-force (its reply is not a floor
	// speaker, so it too would re-enter Publish and mint fresh — the arm died
	// with the retired interaction, and no consumed-arm cleanup is owed). A
	// `stale` return — the stimulus belongs to a discussion a DELIBERATE close
	// terminated (PR #716 review) — skips the same revival tails plus the
	// concurrent dispatch below. A (false, false)
	// no-op on human channels (autonomous disabled → the hook returns before
	// touching state) and on sub-bound rounds, so ordinary channels and
	// mid-discussion rounds proceed byte-for-byte unchanged.
	// The head verdict is authoritative for a head-withheld floor round: the
	// tail call would repeat the same ledger read only to re-derive `stale` —
	// its latched branch returns before the tally advance, so no round is
	// counted, no close can fire, and no metric or state mutation is owed
	// (PR #716 review; was an unconditional call plus a `stale || staleAtHead`
	// fold). Branching first keeps the downstream branches immune to the one
	// theoretical divergence the fold defended against (the id evicted from
	// the bounded ledger between head and tail) — a head-withheld round still
	// never feeds a zero-value `outcome` into the escalation tail below.
	var closed, stale bool
	if staleAtHead {
		stale = true
	} else {
		closed, stale = r.maybeBoundedClose(context.WithoutCancel(ctx), msg, ct, members, channelSize, !floorPath, undelivered, autonomous)
	}
	// The ONE withhold seam (PR #716 review — this clear had been duplicated
	// verbatim across the close and stale branches): a dispatch withheld
	// after markActivity is the mark/clear case with NO reply to re-enter
	// publishCommit — the same "no reply can ever clear it" condition the
	// escalation error branches unmark for (chair_escalation.go). Without
	// this, a close or stale withhold strands every responder "thinking" on
	// the console for the full TTL on a discussion that just terminated. The
	// floor path joins the seam exactly when its round was withheld at the
	// head; a floor round that RAN needs no clear — repliers cleared via
	// publishCommit and silent speakers keep the pre-existing stalled-round
	// TTL decay. Any future branch that decides not to dispatch belongs
	// behind this same seam, or it re-strands the marks.
	if staleAtHead || (!floorPath && (closed || stale)) {
		// PR #718 review finding 8: while a synthesis close is armed, the chair
		// has a directed synthesis turn genuinely in flight — its "thinking"
		// mark (set by maybeArmSynthesisClose) must survive this clear, or the
		// console shows nobody thinking for the whole up-to-120s armed window on
		// the concurrent path (the arm reports stale, so the withhold runs in
		// the same fanout that just marked the chair). Spare exactly that mark;
		// every other withheld responder still clears (no reply will re-enter to
		// clear it). "" when nothing is armed, so ordinary stale/closed
		// withholds are unchanged.
		armedChair := r.armedSynthesisChair(msg.ChannelID)
		for _, id := range respIDs {
			if id == armedChair {
				continue
			}
			r.clearActivity(msg.ChannelID, id)
		}
	}
	if closed {
		return
	}

	// Sub-bound round: the interaction is still open and the path's dispatch
	// proceeds unchanged. Chair-stall-escalation amendment §C 1: the stall tail
	// runs in the round's CALLER, after the floor is released and the round's
	// floor-speaker set is cleared — the chair's reply must re-fanout as a fresh
	// open-floor stimulus, which a floor turn would suppress. A send, never an
	// await (CE7).
	if stale {
		// PR #716 review: the stimulus belongs to a discussion that DELIBERATELY
		// terminated — its id is in the no-reopen ledger, or it lost the closing
		// race to a concurrent bound-crossing sibling (maybeBoundedClose). Its
		// dispatch is withheld on BOTH paths — the concurrent dispatch below,
		// the floor round at the head check above: fanning it would draw
		// LLM replies the publish-path no-reopen latch then absorbs with the
		// spend already spent, the same close-before-dispatch goal as the
		// ordering above. The stall escalation below is skipped like
		// the resynthesize dispatch: both revive the terminated discussion.
		// KNOWN COST (accepted, PR #716 review): a withheld message is
		// committed history but reaches no member's agent-side record — the
		// winner's close notification carries only the winner's message.
		// Bounded to close-racing siblings and the armed-synthesis window's
		// stragglers (synthesis_close.go); STILL an accepted loss after
		// PR 4b-ii — its redelivery marker distinguishes duplicate deliveries,
		// it does not re-deliver a withheld one. True losslessness would need
		// a multi-message close fan, an OQ #5-class trade not yet owed. A
		// divergence WITHOUT a deliberate close (orphan-park artefact, idle
		// rotation) is NOT stale and dispatches normally below.
		r.logger.Debug("channels: stimulus outlived its closed interaction; dispatch and revival tails withheld",
			zap.String("channel_id", msg.ChannelID),
			zap.String("message_id", msg.ID),
			zap.Bool("floor_path", floorPath),
			zap.String("stamped_interaction_id", readInteractionID(msg.Metadata)))
	} else if floorPath {
		r.maybeEscalateStall(context.WithoutCancel(ctx), msg, ct, threadParentSenderID, outcome, members, channelSize, floorMentions)
	} else {
		r.dispatchConcurrent(context.WithoutCancel(ctx), msg, ct, threadParentSenderID, members, channelSize, floorMentions, nil)
	}
	// ISSUE-0099 resynthesize, DISPATCH half — the re-force the head claimed,
	// dispatched only now that the bounded close has ruled the round sub-bound
	// AND live. A stale stimulus skips it like a closed one (PR #716 review):
	// stale means the discussion terminated (or is terminating — a CAS-losing
	// bound-crosser can land while the winner's teardown is mid-flight, before
	// the id retires, when the dispatch's own openness re-check still passes),
	// and a re-forced chair turn's reply would revive it. The consumed arm
	// died with the terminated interaction, the closed branch's own posture.
	// Detached, fire-and-forget, like the stall tail.
	if pendingResynth != nil && !stale {
		r.dispatchResynthesizeMisfire(context.WithoutCancel(ctx), msg, ct, members, channelSize, pendingResynth)
	}
}

// dispatchConcurrent fans `msg` out to every member of `members` other than
// the sender and `never` participants, with peak in-flight dispatches capped
// at `channelFanoutMaxConcurrency` (ISSUE-0014). Blocks until every selected
// recipient has been dispatched or hit `channelFanoutPerRecipientTimeout`.
//
// `ctx` is expected to already be detached from the request lifetime by the
// caller (so the floor path can reuse it for off-floor non-responder
// delivery without re-detaching).
//
// `failures` collects per-recipient dispatch errors for the floor path's
// redelivery accounting (live_delivery.go); nil (a recording no-op) on the
// concurrent fanout path, whose bounded close is sole-delivery by ordering.
func (r *ChannelRouter) dispatchConcurrent(ctx context.Context, msg ChannelMessage, ct ChannelType, threadParentSenderID string, members []Member, channelSize int, floorMentions []string, failures *liveDeliveryFailures) {
	// Buffered channel as a semaphore: each goroutine acquires a slot
	// before starting and releases it on exit, so peak in-flight
	// dispatches never exceed `channelFanoutMaxConcurrency`. The
	// acquire is on the publishing goroutine (not inside the worker)
	// so we apply backpressure on the loop itself rather than letting
	// goroutine creation outpace dispatch completion.
	sem := make(chan struct{}, channelFanoutMaxConcurrency)
	var wg sync.WaitGroup

	for _, m := range members {
		if m.ParticipantID == msg.SenderID {
			continue
		}
		if m.RespondPolicy.Normalize() == RespondNever {
			// `respond: never` participants do not receive dispatches in
			// the v0.3.0 contract — they read history on demand. The
			// response gate (PR 4b) is the canonical enforcement point;
			// short-circuiting here keeps the dispatcher free of policy
			// knowledge and saves a wasted gRPC call. Normalized at the
			// read seam like [orderResponders]' candidate loop and the
			// gate's `_DISPOSITION_ALIASES`: identity for store-canonical
			// rows, but a non-canonical spelling must mean the same thing
			// at every policy read.
			continue
		}
		// RFC 0030 Tier A note: a directed-elsewhere `always` member (one
		// the floor path's [orderResponders] drops to non-responder, see
		// floor_control.go) is intentionally NOT short-circuited here. The
		// receiver gate suppresses its *reply* (directed_elsewhere) but the
		// dispatch still lands so the member *ingests* the message into
		// memory — the gate decides whether to respond, not whether to
		// remember (agents/persona_runtime/action_loop.py's ingest-on-
		// suppress). Filtering it out to mirror the floor path would make
		// un-addressed participants amnesiac. The floor path can drop it
		// because it re-delivers non-responders fire-and-forget for exactly
		// this ingestion; the concurrent path's single dispatch is that
		// delivery.
		m := m
		sem <- struct{}{}
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer func() { <-sem }()
			// These workers run off the request goroutine on both publish
			// paths, so a panicking dispatch is not caught by the server's
			// recoveryMiddleware — recover here or it crashes the process.
			defer r.recoverFanout("dispatch", msg.ChannelID, msg.ID)
			if err := r.dispatchTo(ctx, msg, ct, threadParentSenderID, m, channelSize, floorMentions, dispatchControl{}); err != nil {
				failures.record(m.ParticipantID)
			}
		}()
	}
	wg.Wait()
}

// dispatchTo delivers `msg` to a single recipient with the per-recipient
// timeout and emits the `channel.messages.delivered` counter. Shared by the
// concurrent path ([dispatchConcurrent]), the serialized floor turn
// ([runFloorTurn]), the chair-escalation forced turn
// ([ChannelRouter.maybeEscalateStall]), and the close-notification fan
// ([ChannelRouter.notifyInteractionClose]) so all four honour the same
// deadline + metric contract.
//
// `marker` stamps at most one orchestrator-authored control marker on the
// envelope (see [dispatchMarker]) — [markerNone] on every ordinary
// dispatch. The dispatch error is returned so the escalation tail can
// label its `chair_escalation{outcome}` and the close fan its
// `close_notification{outcome}`; the fanout and floor-turn callers are
// fire-and-forget by contract and ignore it (the warn + the delivered
// counter's status=error emitted here are their entire failure surface).
func (r *ChannelRouter) dispatchTo(ctx context.Context, msg ChannelMessage, ct ChannelType, threadParentSenderID string, m Member, channelSize int, floorMentions []string, control dispatchControl) error {
	dispatchCtx, cancel := context.WithTimeout(ctx, channelFanoutPerRecipientTimeout)
	defer cancel()
	marker := control.marker
	// Resolve the channel's reasoning rung ONCE so Mode and Revise come from a
	// single locked snapshot: two separate ReasoningFor reads could be torn by a
	// concurrent SetReasoning (a runtime config apply) landing between them, and a
	// single read also drops the redundant mutex acquisition per recipient.
	reasoning := r.ReasoningFor(msg.ChannelID)
	// Both close-notification extras are gated once, on the whole payload, so
	// the envelope cannot honour one without the other or leak either off the
	// marker (the dispatchControl contract).
	closeTrigger, closeRedelivery := control.closeNotificationWireFields()
	err := r.dispatcher.Dispatch(dispatchCtx, DispatchEnvelope{
		Recipient:            m,
		ThreadParentSenderID: threadParentSenderID,
		// RFC 0030 Tier B (v0.3.8): the per-publish channel-size + resolved cap
		// the agent-side seam reads for the TB6 channel-size gate. The
		// per-recipient bid signals (salience_gated/threshold) ride on
		// `m`/`Recipient`; these two are channel-wide.
		ChannelSize:               channelSize,
		SalienceMaxChannelMembers: r.salienceMaxFor(msg.ChannelID),
		// RFC 0051 PR 6 go-live: the channel's resolved reasoning rung, read off the
		// (flip-aware) router so a governed channel ships `bid` by default and an
		// explicit `off` stays the kill switch. Channel-wide, like ChannelSize.
		ReasoningMode: reasoning.Mode,
		// RFC 0051 PR 8 (Phase 5a): the channel's resolved reflexion round count,
		// off the same single resolve. Channel-wide; 0 = single-pass.
		ReasoningRevise: reasoning.Revise,
		// Floor-capable-directedness amendment (v0.3.8): per-publish, like
		// ChannelSize — resolved once in [ChannelRouter.fanout].
		FloorMentions:                floorMentions,
		ChairEscalation:              marker == markerChairEscalation || marker == markerChairEscalationResynthesize,
		ChairEscalationResynthesize:  marker == markerChairEscalationResynthesize,
		InteractionCloseNotification: marker == markerCloseNotification,
		Convene:                      marker == markerConvene,
		SynthesisTurn:                marker == markerSynthesisTurn,
		// The close-notification extras ride ONLY under their marker, gated as
		// one payload by closeNotificationWireFields (the dispatchControl
		// contract): a stray value on any other dispatch is structurally
		// unrepresentable on the wire.
		InteractionCloseTrigger:    closeTrigger,
		InteractionCloseRedelivery: closeRedelivery,
	}, msg)
	status := "ok"
	switch {
	case err == nil:
	case errors.Is(err, registry.ErrAgentNotFound):
		// A never-registered member (a human peer on a chat-surface DM, a
		// mistyped channels.yaml membership) is the documented best-effort
		// miss, not a delivery failure: the error return must stand — the
		// undelivered ledger keys the close-notification redelivery marker
		// on it — but a standing member misses on EVERY message, so
		// counting it as status="error" (plus a second warn on top of the
		// dispatcher's) turns a healthy channel into a permanent error
		// signal. Distinct status, and the dispatcher's single warn (with
		// the read-via-history context) is the whole log surface.
		status = "unregistered"
	default:
		status = "error"
		r.logger.Warn("channels: dispatch failed",
			zap.String("channel_id", msg.ChannelID),
			zap.String("recipient", m.ParticipantID),
			zap.Error(err))
	}
	if r.metrics != nil && r.metrics.MessagesDelivered != nil {
		r.metrics.MessagesDelivered.Add(ctx, 1, metric.WithAttributes(
			attribute.String("channel_type", string(ct)),
			attribute.String("status", status),
		))
	}
	return err
}
