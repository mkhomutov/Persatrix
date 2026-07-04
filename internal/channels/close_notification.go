package channels

// End-vote-close-propagation amendment (RFC 0030 §H follow-up,
// docs/rfcs/0030-amendment-end-vote-close-propagation.md): the Layer 4
// quorum close suppresses the closing vote's fanout — correctly, so the
// room stops — but that starves every member's agent-local tracker of
// the close itself: with no follow-up traffic each member buries the
// converged discussion as "went idle" up to a full agent-side idle
// window later, and the chair never learns its synthesis closed the
// room (found live by MT-CHANNEL-GOV-004). This file is the CP1/CP2/CP5
// orchestrator half: re-dispatch the closing message to every
// dispatch-served non-sender member as a marked, ingestion-grade close
// NOTIFICATION — control, never stimulus.

import (
	"context"
	"maps"
	"slices"

	"github.com/google/uuid"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"
)

// Close-notification dispatch outcomes (CP5): every per-recipient dispatch
// emits `channel.conversation.close_notification{channel_type, outcome}`.
// Two outcomes only — the notification has no disposition chain to walk
// (the close already happened); a member excluded by contract (the
// sender, a `RespondNever` row) is not an outcome, it is not a recipient.
const (
	closeNotificationDispatched    = "dispatched"
	closeNotificationDispatchError = "dispatch_error"
)

// finalizeInteractionClose runs the shared close-teardown tail both deterministic
// close causes produce — the Layer 4 quorum end-vote
// ([ChannelRouter.processEndVote]) and the RFC 0052 bounded close
// ([ChannelRouter.boundedClose]) — AFTER each has done its cause-specific work:
// the single-shot `closedInteractions` CAS and the `interaction_closed{…}` record
// emit (whose args differ — the end-vote's quorum count vs the bounded close's
// structural/cost trigger). The steps here are cause-agnostic and ORDER-SENSITIVE:
//
//  1. record each tracked participant's leftover reply allowance BEFORE the
//     counters are discarded, so the reply_budget_remaining histogram observes the
//     interaction's final state (the Layer 4 → Layer 2 composition seam);
//  2. discard the per-participant reply counters (the §F reset seam
//     reply_budget.go reserved for the Layer 4 close path);
//  3. retire the id (IP8) so the channel's NEXT publish mints a FRESH interaction
//     — the close ends one conversation, not the channel — recording `trigger` as
//     the OQ 5 close cause; the closed id parks as the pending retiree, its
//     tombstone (added by the caller's CAS) surviving until the deferred discard so
//     any commit racing this close is suppressed and self-healed;
//  4. fan the marked close NOTIFICATION so every agent-local tracker closes its
//     scope NOW and authors its RFC 0020 summary instead of burying the converged
//     discussion as "went idle" a window later. `excludeSender` differs by cause
//     (see [ChannelRouter.notifyInteractionClose]).
//
// Keeping the tail in one place means a future teardown change — e.g. the deferred
// PR 7 wallet EvictInteraction step — lands for both causes at once and cannot
// silently drift between the two close sites.
// `redelivery` (PR 4b-ii) says the closing message was already delivered live
// via ordinary fanout — true only for the floor-path bounded close's stimulus;
// always false on the end-vote path (the closing vote's own fanout is
// suppressed, so the notification is its sole delivery).
func (r *ChannelRouter) finalizeInteractionClose(ctx context.Context, msg ChannelMessage, ct ChannelType, interactionID, trigger string, excludeSender, redelivery bool) {
	r.recordReplyBudgetRemainingAtClose(ctx, msg.ChannelID, interactionID, ct)
	r.DiscardInteractionReplyBudget(interactionID)
	r.markInteractionClosed(msg.ChannelID, interactionID, trigger)
	// The wire-stamped close cause (PR 4b-ii): ONLY the RFC 0052 bounded
	// close's structural/cost triggers ride the notification — the field's
	// presence doubles as the receiver's OQ #6 metering key ("this close is
	// an autonomous bounded close"), so the end-vote trigger must map to the
	// empty pre-4b-ii wire shape or every human end-vote close would start
	// leasing its summaries.
	boundedTrigger := ""
	if trigger == structuralTrigger || trigger == costTrigger {
		boundedTrigger = trigger
	}
	r.notifyInteractionClose(ctx, msg, ct, excludeSender, boundedTrigger, redelivery)
}

// recordInteractionClosedMetric bumps the `interaction_closed{channel_type,
// trigger}` counter — the one cause-agnostic step every close-cause record
// function shares. The three siblings ([ChannelRouter.recordInteractionClosed]
// (end_votes), [ChannelRouter.recordInteractionClosedIdle] (idle), and
// [ChannelRouter.recordInteractionClosedBounded] (structural/cost)) differ only
// in their structured log line; the instrument emit is identical, so it lives
// here once. Centralizing it means a future change to the instrument (a new
// attribute, an exemplar, a unit) lands for every close cause at once and cannot
// silently drift between them — the same anti-drift rationale
// [ChannelRouter.finalizeInteractionClose] centralizes the teardown for. Nil-safe
// like every other channel instrument.
func (r *ChannelRouter) recordInteractionClosedMetric(ctx context.Context, ct ChannelType, trigger string) {
	if r.metrics != nil && r.metrics.InteractionClosed != nil {
		r.metrics.InteractionClosed.Add(ctx, 1, metric.WithAttributes(
			attribute.String("channel_type", string(ct)),
			attribute.String("trigger", trigger),
		))
	}
}

// notifyInteractionClose fans a channel-close signal to every dispatch-served
// member as the CP2 marked dispatch, so each agent-local tracker closes the
// channel scope NOW instead of idling out a window later. Shared by the two
// deterministic close causes: the Layer 4 quorum end-vote
// ([ChannelRouter.processEndVote]) and the RFC 0052 bounded close
// ([ChannelRouter.boundedClose]).
//
// `excludeSender` selects the recipient set the two causes need, and the
// distinction is load-bearing (bounded-close deep review):
//   - END-VOTE (`excludeSender` true): `msg` is the closing vote and its
//     sender's own vote action ALREADY closed its local tracker, so
//     re-notifying it is redundant — skip it.
//   - BOUNDED CLOSE (`excludeSender` false): `msg` is merely the round-
//     triggering stimulus; nothing closed its sender's tracker. Excluding it
//     would strand that participant — routinely the convener/chair whose reply
//     drove the open-floor round — on the very "went idle" bury this fan exists
//     to prevent, authoring no RFC 0020 summary the §D artifact requires. So the
//     bounded close notifies the sender too.
//
// Contract (CP1): recipients are otherwise the members the dispatcher serves —
// `RespondAlways` / `RespondWhenMentioned`. `RespondNever` members (the human
// seam) sit outside the dispatch contract by design regardless of
// `excludeSender`: fanout's v0.3.0 short-circuit and
// [DispatchEnvelope.Recipient]'s invariant exclude them upstream of the
// dispatcher, they run no agent-local tracker to starve, and their surface reads
// the persisted message from the store on demand.
//
// Posture (CP5): fire-and-forget, off the publish path — called from
// [ChannelRouter.processEndVote]'s close branch and [ChannelRouter.boundedClose],
// never awaited, every degraded branch nets to the status quo (the member's
// tracker idles out with the legacy label). The WHOLE fan is off-path (PR #613 review):
// the member lookup and the spawning loop run on a detached, tracked
// wrapper goroutine, so the closing publish pays one goroutine spawn,
// never a store read — and the per-recipient loop applies the same
// ISSUE-0014 semaphore bound as [ChannelRouter.dispatchConcurrent], with
// the acquire on the wrapper's loop so the backpressure stalls the
// wrapper, never the publisher. Every goroutine here recovers via
// [ChannelRouter.recoverFanout]: detached workers have no
// recoveryMiddleware umbrella, and an unrecovered panic in any goroutine
// terminates the whole orchestrator.
//
// "Never awaited" is not "untracked": the wrapper and each per-recipient
// worker register on the router's fanout drain WaitGroup — the workers
// Add while the wrapper's own registration holds the count positive, so
// the drain never races an Add-from-zero — graceful shutdown's drain
// bounds them instead of leaking them past process exit, and
// [ChannelRouter.WaitForPendingFanout] stays the deterministic assert
// point the committed acceptance relies on — this is the one dispatch
// the suppressed publish path never joins.
//
// The context is detached (`context.WithoutCancel`) BEFORE any work,
// member lookup included: the close is a one-shot signal (the
// interaction is already closed; nothing retries it) that outlives the
// closing publish's HTTP response by construction, and a client
// disconnect must not silently drop it — a lookup still descended from
// the request context would die with the request and drop the entire
// fan (PR #613 review; pinned by the disconnect acceptance). The same
// posture as [ChannelRouter.fanout]'s callers, which detach before any
// fan work runs.
//
// Each recipient gets a FRESH event id with CLONED reference fields —
// the metadata bag and the mentions slice (CE3's lesson, both halves:
// the agent-side conversation window dedups by message id, so
// redelivering the persisted vote under its own id would be silently
// dropped by any member that ingested it pre-close; and the
// per-recipient goroutines outlive this call, so an aliased map or
// backing array would let a future write corrupt a sibling's dispatch).
//
// Thread channels ride the same seam (CP4 — a thread IS its
// interaction). `threadParentSenderID` is deliberately empty: it exists
// to serve receiver-side directedness decisions, and a marked event
// never reaches them — the gate refuses it pre-LLM (CP3).
// `closeTrigger` / `redelivery` (PR 4b-ii) are the two typed wire fields the
// fan stamps on every recipient's envelope: the truthful bounded-close cause
// ("" on the end-vote path — see [ChannelRouter.finalizeInteractionClose]) and
// the already-delivered-live marker that lets receivers skip the duplicate
// final-turn ingest on a floor-path bounded close.
func (r *ChannelRouter) notifyInteractionClose(ctx context.Context, msg ChannelMessage, ct ChannelType, excludeSender bool, closeTrigger string, redelivery bool) {
	notifyCtx := context.WithoutCancel(ctx)
	r.fanoutWG.Add(1)
	go func() {
		defer r.fanoutWG.Done()
		defer r.recoverFanout("close_notification", msg.ChannelID, msg.ID)
		members, err := r.store.GetMembers(notifyCtx, msg.ChannelID)
		if err != nil {
			// CP5: fail-open. No members means no one to notify; the trackers
			// idle out exactly as they would have pre-amendment.
			r.logger.Warn("channels: close-notification member lookup failed; close stands unannounced",
				zap.String("channel_id", msg.ChannelID),
				zap.Error(err))
			return
		}
		channelSize := len(members)
		sem := make(chan struct{}, channelFanoutMaxConcurrency)
		for _, m := range members {
			// Normalized at the read seam like every other policy read
			// ([dispatchConcurrent], [orderResponders], the Python gate's
			// `_DISPOSITION_ALIASES`): identity for store-canonical rows,
			// but CP1 defines the recipient set as the set fanout serves,
			// so the two predicates must not diverge (PR #613 review).
			if (excludeSender && m.ParticipantID == msg.SenderID) || m.RespondPolicy.Normalize() == RespondNever {
				continue
			}
			notification := msg
			notification.ID = uuid.NewString()
			// The bag is cloned VERBATIM, vote keys included, deliberately
			// (PR #613 review): the wire event's `interaction_id` and
			// `cascade_depth` are typed extractions FROM it in
			// [GRPCMessageDispatcher.channelMessageToProto], so stripping
			// the "stale" end-vote key would invite stripping fields the
			// agent-side close (PR 3) consumes. The retained key is inert:
			// the bag itself is never serialized onto the wire event, and a
			// dispatch never re-enters [ChannelRouter.Publish], so nothing
			// downstream can re-read it as a live vote.
			notification.Metadata = maps.Clone(msg.Metadata)
			notification.Mentions = slices.Clone(msg.Mentions)
			recipient := m
			sem <- struct{}{}
			r.fanoutWG.Add(1)
			go func() {
				defer r.fanoutWG.Done()
				defer func() { <-sem }()
				defer r.recoverFanout("close_notification", msg.ChannelID, notification.ID)
				outcome := closeNotificationDispatched
				if err := r.dispatchTo(notifyCtx, notification, ct, "", recipient, channelSize, nil, dispatchControl{
					marker:          markerCloseNotification,
					closeTrigger:    closeTrigger,
					closeRedelivery: redelivery,
				}); err != nil {
					// dispatchTo already warned with the recipient + error; the
					// outcome label is this path's own failure surface (CP5).
					outcome = closeNotificationDispatchError
				}
				if r.metrics != nil && r.metrics.CloseNotification != nil {
					r.metrics.CloseNotification.Add(notifyCtx, 1, metric.WithAttributes(
						attribute.String("channel_type", string(ct)),
						attribute.String("outcome", outcome),
					))
				}
			}()
		}
	}()
}
