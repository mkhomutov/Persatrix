package channels

import (
	"context"
	"slices"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"
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
	// in an escalated interaction, not only the misfiring ones, so a clean
	// hand-off disarms the trigger (see [ChannelRouter.claimResynthesizeMisfire]);
	// a no-op (nil) for every non-chair publish. Only the re-force DISPATCH
	// waits for the tail, gated on the bounded-close outcome below.
	pendingResynth := r.claimResynthesizeMisfire(msg, misfired)

	// Mark the responders as having an in-flight turn for the console presence
	// signal (RFC 0048 Tier 1) — exactly the members [orderResponders] expects
	// to reply, not the ingestion-only recipients dispatchConcurrent also
	// delivers to (marking those would strand a "thinking" line on a member that
	// will never answer). Cleared per-member when its reply re-enters
	// (publishCommit), by the TTL backstop, or — on the concurrent path's
	// bounding round, whose dispatch is withheld so no reply can ever re-enter —
	// by the close branch below. See activity.go.
	responders, nonResponders := orderResponders(members, msg, threadParentSenderID)
	respIDs := responderIDs(responders)
	r.markActivity(msg.ChannelID, respIDs)

	// The two dispatch paths (floor round vs concurrent), resolved once so the
	// RFC 0052 §D bounded close below has ONE call site serving both.
	settings, floorOn := r.floorSettingsFor(msg.ChannelID)
	floorPath := floorOn && settings.enabled && len(responders) >= 2
	var outcome floorRoundOutcome
	if floorPath {
		outcome = r.floorRound(ctx, msg, ct, threadParentSenderID, responders, nonResponders, settings.turnTimeout, channelSize, floorMentions)
	}

	// RFC 0052 §D bounded close, at the fanout tail on BOTH paths — one call
	// site (review round 5), with the per-path ordering the deep review pinned
	// preserved by POSITION relative to each path's dispatch:
	//
	//   - FLOOR path: the close runs AFTER the round (floorRound above), so the
	//     `max_rounds`-th round's discussion happens and then the interaction is
	//     at its terminal bound and must CLOSE, not be revived. KNOWN COST
	//     (review finding, complete fix deferred): the bounding stimulus was
	//     already delivered live inside the round, and the close notification
	//     re-delivers it under a fresh wire id, which every non-sender recipient
	//     ingests again (close_notification.py) — one duplicated final turn on
	//     each closed record. Distinguishing "re-delivery, close only" from
	//     "sole delivery, ingest then close" needs a redelivery marker on the
	//     wire, and ChannelMessageEvent has no metadata map — a typed proto
	//     field, which this Go-only slice deliberately does not touch; it lands
	//     with PR 4b-ii's wire work (docs/rfcs/0052-pr-plan.md).
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
	//     closing (close_notification.py). The one gap: a member with NO open
	//     scope no-ops rather than fabricate a 1-turn record — in practice every
	//     responder opened a scope on the prior live rounds (1..N-1), so it only
	//     bites at max_rounds=1 on this path, the documented tiny-bound edge
	//     ([DefaultAutonomousMaxRounds]).
	//
	// A `true` return means the id is retired, so every follow-on that could
	// revive the interaction is skipped: the stall escalation (a forced chair
	// turn's reply would mint fresh and reopen), the concurrent stimulus
	// dispatch, and the pending ISSUE-0099 re-force (its reply is not a floor
	// speaker, so it too would re-enter Publish and mint fresh — the arm died
	// with the retired interaction, and no consumed-arm cleanup is owed). A
	// no-op returning false on human channels (autonomous disabled → the hook
	// returns before touching state) and on sub-bound rounds, so ordinary
	// channels and mid-discussion rounds proceed byte-for-byte unchanged.
	if r.maybeBoundedClose(context.WithoutCancel(ctx), msg, ct, channelSize) {
		if !floorPath {
			// The withheld dispatch is the one mark/clear seam with NO reply to
			// re-enter publishCommit — the same "no reply can ever clear it"
			// condition the escalation error branches unmark for
			// (chair_escalation.go). Without this, the close strands every
			// responder "thinking" on the console for the full TTL on a
			// discussion that just terminated. The floor path needs no twin:
			// its round already dispatched, so repliers cleared via
			// publishCommit and silent speakers keep the pre-existing
			// stalled-round TTL decay.
			for _, id := range respIDs {
				r.clearActivity(msg.ChannelID, id)
			}
		}
		return
	}

	// Sub-bound round: the interaction is still open and the path's dispatch
	// proceeds unchanged. Chair-stall-escalation amendment §C 1: the stall tail
	// runs in the round's CALLER, after the floor is released and the round's
	// floor-speaker set is cleared — the chair's reply must re-fanout as a fresh
	// open-floor stimulus, which a floor turn would suppress. A send, never an
	// await (CE7).
	if floorPath {
		r.maybeEscalateStall(context.WithoutCancel(ctx), msg, ct, threadParentSenderID, outcome, members, channelSize, floorMentions)
	} else {
		r.dispatchConcurrent(context.WithoutCancel(ctx), msg, ct, threadParentSenderID, members, channelSize, floorMentions)
	}
	// ISSUE-0099 resynthesize, DISPATCH half — the re-force the head claimed,
	// dispatched only now that the bounded close has ruled the round sub-bound.
	// Detached, fire-and-forget, like the stall tail.
	if pendingResynth != nil {
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
func (r *ChannelRouter) dispatchConcurrent(ctx context.Context, msg ChannelMessage, ct ChannelType, threadParentSenderID string, members []Member, channelSize int, floorMentions []string) {
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
			r.dispatchTo(ctx, msg, ct, threadParentSenderID, m, channelSize, floorMentions, markerNone)
		}()
	}
	wg.Wait()
}

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
)

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
func (r *ChannelRouter) dispatchTo(ctx context.Context, msg ChannelMessage, ct ChannelType, threadParentSenderID string, m Member, channelSize int, floorMentions []string, marker dispatchMarker) error {
	dispatchCtx, cancel := context.WithTimeout(ctx, channelFanoutPerRecipientTimeout)
	defer cancel()
	// Resolve the channel's reasoning rung ONCE so Mode and Revise come from a
	// single locked snapshot: two separate ReasoningFor reads could be torn by a
	// concurrent SetReasoning (a runtime config apply) landing between them, and a
	// single read also drops the redundant mutex acquisition per recipient.
	reasoning := r.ReasoningFor(msg.ChannelID)
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
	}, msg)
	status := "ok"
	if err != nil {
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
