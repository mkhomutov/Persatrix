package channels

import (
	"context"
	"fmt"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"
)

// floorTurn outcome label values for the
// `channel.conversation.floor_turn{outcome}` counter (amendment PR 4).
const (
	floorOutcomeReplied = "replied"
	floorOutcomeTimeout = "timeout"
)

// floor_control.go — RFC 0030 Layer 2.5 (floor control / speaker
// serialization). This file ships the *inert* primitives in PR 1: the
// per-channel floor registry and the responder-ordering helper, both with
// full unit coverage and no call site yet. PR 2 rewires
// [ChannelRouter.fanout] to drive a serialized speaker round through these;
// PR 3 flips the per-channel `floor_control` flag default on for group
// channels. See `docs/rfcs/0030-amendment-floor-control-pr-plan.md`.
//
// Why serialize at all: today a channel message is fanned out to every
// responder concurrently and fire-and-forget ([fanout.go]), so each persona
// composes against a transcript containing none of its peers' replies — N
// overlapping, mutually-blind replies to one stimulus. Floor control makes
// the responders take the floor one at a time, each reading the prior
// speaker's reply.

// floorRegistry is the orchestrator-side, in-process table that grants at
// most one floor round per channel at a time. It mirrors [replyWaiter]'s
// single-replica constraint: the floor state lives in this process, so a
// horizontally-scaled orchestrator where an agent's REST publish lands on a
// different replica than the one holding the floor would not serialize
// correctly — single-replica is the v0.3.x contract (see the replyWaiter
// doc-string for the same caveat).
//
// Keyed by `channel_id` per amendment decision D5 — the floor is a property
// of the channel, not of any one stimulus or interaction id (RFC 0020 is not
// wired Go-side; re-keying onto `interaction_id` is a post-v0.3.6 follow-up).
//
// Each channel's floor is a capacity-1 buffered chan used as a mutex: a
// single token in the chan means "available". [acquire] receives the token
// (blocking while another round holds it); [release] returns it. Using a
// token chan rather than a [sync.Mutex] makes [release] idempotent — a
// redundant release is a non-blocking no-op rather than a panic, so the
// loop's deferred release is safe even on paths that already released.
type floorRegistry struct {
	mu     sync.Mutex
	floors map[string]chan struct{}
}

func newFloorRegistry() *floorRegistry {
	return &floorRegistry{floors: make(map[string]chan struct{})}
}

// floorFor returns the per-channel token chan, creating it (in the
// available state) on first use. Guarded by the registry mutex; the
// returned chan is then operated on outside the lock so a parked [acquire]
// never holds the registry mutex.
//
// Entries are never evicted, but the key space is `channel_id` — bounded by
// the declared channel count (itself capped by `max_channels`) — so the map
// cannot grow without bound. The post-v0.3.6 re-key onto `interaction_id`
// noted on [floorRegistry] would make the key space unbounded; that follow-up
// must add eviction (e.g. drop a floor once its round completes).
func (f *floorRegistry) floorFor(channelID string) chan struct{} {
	f.mu.Lock()
	defer f.mu.Unlock()
	ch, ok := f.floors[channelID]
	if !ok {
		ch = make(chan struct{}, 1)
		ch <- struct{}{} // seed the available token
		f.floors[channelID] = ch
	}
	return ch
}

// acquire blocks until this channel's floor is free, then takes it. Pair
// every acquire with exactly one [release] (the redundant-release case is a
// safe no-op).
//
// acquire takes no [context.Context] and so cannot be cancelled or
// time-bounded: it parks until a [release] frees the floor. That is
// deliberate for the primitive — the per-turn timeout and any cancellation
// are the caller's responsibility (PR 2's serialized loop wraps the round in
// the `floor_turn_timeout_seconds` budget), not the registry's.
func (f *floorRegistry) acquire(channelID string) {
	<-f.floorFor(channelID)
}

// release returns the floor to the available state. Idempotent: releasing
// an already-free floor is a no-op rather than a panic, so a deferred
// release on a path that already released cannot double-fault.
func (f *floorRegistry) release(channelID string) {
	ch := f.floorFor(channelID)
	select {
	case ch <- struct{}{}:
	default:
		// Already available — redundant release, ignore.
	}
}

// orderResponders splits a channel's membership into the **candidate
// responders** (which enter the serialized floor round) and the
// **non-responders** (delivered fire-and-forget for memory ingestion only,
// off the critical path), and orders the responders mentioned-first then by
// the incoming member order (amendment decision D3 — the round order is
// frozen at round start with a stable tie-break).
//
// `members` is expected in the deterministic [ChannelStore.GetMembers]
// order (joined_at ASC, participant_id ASC); the helper preserves that
// order within each group rather than re-sorting.
//
// Candidate responders = `always` members ∪ `when_mentioned` members that
// are mentioned ∪ thread-reply-to-self members (a `when_mentioned` member
// whose own message this stimulus is a thread reply to — `threadParentSenderID`
// pre-resolved by the router). The sender is always filtered (no self-reply)
// and `never` members are excluded from both sets (they read history on
// demand and never receive a dispatch, matching today's fanout).
//
// This candidate set is a *superset* of the receiver-side response gate's
// respond-true set ([agents/response_gate.py] — always; when_mentioned &
// mentioned; when_mentioned & thread-reply-to-self), which is what matters
// for correctness in both directions:
//   - No false negatives: a member the gate would let respond is never
//     dropped from the round, so nobody is silently denied the floor. The
//     thread-reply-to-self branch looks looser here (`threadParentSenderID
//     != ""` vs. the gate's `thread_id != "" && parent == self`), but the
//     router only resolves a non-empty `threadParentSenderID` for thread
//     events ([ChannelRouter.resolveThreadParentSenderID] returns "" when
//     `msg.ThreadID == ""`), so the two conditions are equivalent in
//     practice — do not let that invariant rot.
//   - False positives are harmless: a candidate the gate ultimately
//     suppresses simply yields no reply and the per-turn timeout advances
//     the loop (amendment §"Candidate responder set vs. delivery").
func orderResponders(members []Member, msg ChannelMessage, threadParentSenderID string) (responders, nonResponders []Member) {
	mentioned := make(map[string]bool, len(msg.Mentions))
	for _, id := range msg.Mentions {
		mentioned[id] = true
	}

	// RFC 0030 relevance amendment Tier A (v0.3.7): a message that names
	// specific recipients and is not an explicit broadcast (`MentionEveryone`
	// absent) is *directed* — an `always`/`participant` member who is not
	// named is suppressed by the receiver gate (directed_elsewhere). Mirror
	// that here so floor control does not queue such a member into the
	// serialized round only to have it stay silent and burn the per-turn
	// timeout. This keeps the candidate set a superset of the gate's
	// respond-true set: it now excludes exactly the members the gate now
	// excludes, so no member the gate would admit is dropped (the
	// no-false-negatives invariant above still holds).
	//
	// Floor-capable-directedness amendment (v0.3.8, §C item 3 — flipped in
	// the same change as the Python gate, preserving the candidate-set/gate
	// parity above): the directedness basis is the *floor-capable* mention
	// subset ([resolveFloorMentions]) — naming only parties that cannot take
	// the floor (the human, an `observer`, a non-member, the sender itself)
	// is open floor. The broadcast guard stays on raw mentions: `@everyone`
	// falls out of the intersection, so a subset-based guard would be
	// vacuously true and re-suppress "@everyone @iron-fox" (§E matrix pin).
	// The raw `mentioned` set keeps serving the admit/ordering checks (OQ 3).
	directed := len(resolveFloorMentions(members, msg.Mentions, msg.SenderID)) > 0 &&
		!mentioned[MentionEveryone]

	// Split into mentioned vs. unmentioned responders so the final
	// concatenation is mentioned-first while preserving member order
	// within each group (stable tie-break).
	var mentionedResp, unmentionedResp []Member
	for _, m := range members {
		if m.ParticipantID == msg.SenderID {
			continue // never reply to self
		}
		// Normalize at the read seam, mirroring the Python gate's
		// `_DISPOSITION_ALIASES` defence for the same row on the wire.
		// Identity in practice (the membership CHECK constraint admits only
		// the legacy triple), but a non-canonical spelling that ever does
		// reach a [Member] must classify identically here, in
		// [resolveFloorMentions], and in the gate — capable on one basis but
		// refused candidacy on another is the guaranteed-silence defect.
		policy := m.RespondPolicy.Normalize()
		if policy == RespondNever {
			continue // read-on-demand; no dispatch, off both sets
		}

		isMentioned := mentioned[m.ParticipantID]
		isCandidate := false
		switch policy {
		case RespondAlways:
			// Open-floor or broadcast → candidate; directed-elsewhere → not.
			isCandidate = isMentioned || !directed
		case RespondWhenMentioned:
			// Mentioned, or a thread reply to a message this member sent.
			isCandidate = isMentioned ||
				(threadParentSenderID != "" && m.ParticipantID == threadParentSenderID)
		}

		if !isCandidate {
			nonResponders = append(nonResponders, m)
			continue
		}
		if isMentioned {
			mentionedResp = append(mentionedResp, m)
		} else {
			unmentionedResp = append(unmentionedResp, m)
		}
	}

	responders = append(mentionedResp, unmentionedResp...)
	return responders, nonResponders
}

// channelFloorSettings is the resolved per-channel floor-control config held
// by the router ([ChannelRouter.floorSettings]). `enabled` gates the
// serialized round; `turnTimeout` is the per-speaker budget (amendment D2).
type channelFloorSettings struct {
	enabled     bool
	turnTimeout time.Duration
}

// recordFloorSpeaker / clearFloorSpeakers / isFloorSpeakerReply implement the
// deferred-fanout seam (amendment D1). As each speaker is granted the floor the
// round records it in the channel's floor-speaker set; [ChannelRouter.Publish]
// reads that set to recognise a floor-turn reply and skip its re-fanout (the
// loop is the sole dispatcher). All three take the shared floorMu.
//
// The set accumulates every speaker the *active* round has granted the floor —
// not just the current turn-holder — so a speaker that exhausted its turn
// budget (D2) and replies late, while a later speaker holds the floor, is still
// recognised as belonging to this round and suppressed. [clearFloorSpeakers]
// drops the whole set at round end, so a reply that genuinely arrives after the
// round re-fanouts normally (bounded by `cascade_depth`).
func (r *ChannelRouter) recordFloorSpeaker(channelID, participantID string) {
	r.floorMu.Lock()
	speakers := r.floorSpeakers[channelID]
	if speakers == nil {
		speakers = make(map[string]struct{})
		r.floorSpeakers[channelID] = speakers
	}
	speakers[participantID] = struct{}{}
	r.floorMu.Unlock()
}

func (r *ChannelRouter) clearFloorSpeakers(channelID string) {
	r.floorMu.Lock()
	delete(r.floorSpeakers, channelID)
	r.floorMu.Unlock()
}

func (r *ChannelRouter) isFloorSpeakerReply(channelID, senderID string) bool {
	r.floorMu.Lock()
	defer r.floorMu.Unlock()
	speakers, ok := r.floorSpeakers[channelID]
	if !ok {
		return false
	}
	_, held := speakers[senderID]
	return held
}

// floorRound runs the serialized speaker round for a channel with floor
// control enabled (RFC 0030 Layer 2.5). The caller ([ChannelRouter.fanout])
// has already split membership via [orderResponders] and confirmed there are
// ≥2 responders.
//
// Sequence:
//
//  1. Deliver non-responders (`when_mentioned` & not mentioned) fire-and-
//     forget, concurrently, off the floor — memory ingestion only, exactly
//     as the pre-amendment fanout did. They never enter the serialized queue.
//  2. Acquire the per-channel floor so only one round runs at a time
//     (serializes concurrent stimuli on the same channel — D5).
//  3. Grant the floor to each responder in turn ([runFloorTurn]); the loop
//     waits for each speaker's reply to be persisted before dispatching the
//     next, so speaker k reads speakers 1..k-1.
//  4. Release the floor and clear the round's floor-speaker set.
//
// The whole round runs inline on the publish goroutine (as the concurrent
// fanout does), so the stimulus publish blocks until the round completes —
// the documented latency trade of serialization.
//
// Worst-case latency is additive in the silent candidates: a responder that
// stays silent — or whose receiver-side response gate ultimately suppresses it
// (the harmless false-positive in [orderResponders]) — consumes its full
// `turnTimeout` before the loop advances. A round of N candidates where M stay
// silent therefore blocks the publish path for up to M×`turnTimeout`. Size
// `floor_turn_timeout_seconds` (default [DefaultFloorTurnTimeoutSeconds], 45s)
// against the expected silent-candidate count before enabling per channel (PR 3).
func (r *ChannelRouter) floorRound(
	ctx context.Context,
	msg ChannelMessage,
	ct ChannelType,
	threadParentSenderID string,
	responders, nonResponders []Member,
	turnTimeout time.Duration,
	channelSize int,
	floorMentions []string,
) (floorRoundOutcome, map[string]struct{}) {
	detached := context.WithoutCancel(ctx)

	// Per-recipient live-delivery failures, collected across BOTH delivery
	// shapes of the round (the serial floor turns and the fire-and-forget
	// non-responder ingestion) — the bounded close's redelivery marker must
	// not claim a delivery that never landed (PR #718 review; live_delivery.go).
	failures := &liveDeliveryFailures{}

	if len(nonResponders) > 0 {
		r.dispatchConcurrent(detached, msg, ct, threadParentSenderID, nonResponders, channelSize, floorMentions, failures)
	}

	r.floors.acquire(msg.ChannelID)
	defer r.floors.release(msg.ChannelID)
	defer r.clearFloorSpeakers(msg.ChannelID)

	// Time the round from floor acquisition (after non-responder fanout and
	// the inter-round wait) so the histogram measures the serialization cost
	// the responders actually impose, not queueing behind a prior round.
	start := time.Now()
	// Aggregate the per-turn outcomes into the round outcome the caller's
	// stall-detection tail reads (chair-stall-escalation amendment CE1) —
	// the same tallies the `floor_turn{outcome}` counter already records.
	var outcome floorRoundOutcome
	for _, speaker := range responders {
		outcome.granted++
		if r.runFloorTurn(detached, msg, ct, threadParentSenderID, speaker, turnTimeout, channelSize, floorMentions, failures) {
			outcome.replied++
		}
	}
	r.recordFloorRound(detached, ct, time.Since(start))
	return outcome, failures.snapshot()
}

// recordFloorTurn / recordFloorRound emit the RFC 0030 Layer 2.5 floor-control
// telemetry (amendment PR 4). Both are nil-safe — a router built without a
// metrics handle (unit tests, minimal deployments) records nothing — matching
// the contract every other channel instrument honours.
func (r *ChannelRouter) recordFloorTurn(ctx context.Context, ct ChannelType, outcome string) {
	if r.metrics == nil || r.metrics.FloorTurn == nil {
		return
	}
	r.metrics.FloorTurn.Add(ctx, 1, metric.WithAttributes(
		attribute.String("channel_type", string(ct)),
		attribute.String("outcome", outcome),
	))
}

func (r *ChannelRouter) recordFloorRound(ctx context.Context, ct ChannelType, d time.Duration) {
	if r.metrics == nil || r.metrics.FloorRoundDuration == nil {
		return
	}
	r.metrics.FloorRoundDuration.Record(ctx, float64(d)/float64(time.Millisecond),
		metric.WithAttributes(attribute.String("channel_type", string(ct))))
}

// runFloorTurn grants the floor to one speaker: it records the speaker in the
// round's floor-speaker set (so its reply's publish defers fanout — D1, and
// stays deferred even if the reply lands after this turn advances), registers
// the reply waiter *before* dispatch (closing the replies-faster-than-register
// race, mirroring [ChannelRouter.PublishAndAwait]), dispatches the stimulus
// to that speaker alone, then waits for the speaker's reply to be persisted
// or for the per-turn timeout (D2) before returning so the loop advances.
//
// Per-message turn boundary: the turn advances on the *first* reply the speaker
// publishes, because the waiter is single-shot ([replyWaiter.Notify]). An agent
// that splits one logical turn across several SEND_CHANNEL_MESSAGE publishes
// (e.g. tool_call → tool_result → final_answer) advances the floor on its first
// message, so a later speaker may read history before the trailing messages
// persist — the mutual-visibility guarantee holds per *message*, not per
// multi-message turn. Persist-happens-before-Notify still guarantees that first
// message is in history before the next speaker dispatches; the agent-side
// contract is to fold a turn into a single publish. Multi-message reply
// semantics are deferred to v0.4 (docs/issues/ISSUE-0033).
func (r *ChannelRouter) runFloorTurn(
	ctx context.Context,
	msg ChannelMessage,
	ct ChannelType,
	threadParentSenderID string,
	speaker Member,
	turnTimeout time.Duration,
	channelSize int,
	floorMentions []string,
	failures *liveDeliveryFailures,
) bool {
	r.recordFloorSpeaker(msg.ChannelID, speaker.ParticipantID)

	// Presence Tier 1 (RFC 0048): re-stamp the speaker as thinking at the moment
	// its turn is granted. [ChannelRouter.fanout] marks every responder once at
	// round start, but a serialized round dispatches them one at a time while the
	// activity TTL is sized for a single turn — so a late speaker's round-start
	// mark can age out of the indicator before its turn even begins. Re-marking
	// here restarts its TTL from actual dispatch, so the console shows it thinking
	// for its own turn rather than dropping it mid-queue. See activity.go.
	r.markActivity(msg.ChannelID, []string{speaker.ParticipantID})

	replyCh, cancel, err := r.waiter.Register(msg.ChannelID, speaker.ParticipantID)
	if err != nil {
		// A waiter for this (channel, speaker) already exists — e.g. a
		// re-entrant round, or a chat PublishAndAwait sharing the key space.
		// Don't clobber it; dispatch and advance on the per-turn timeout
		// alone (replyCh stays nil, so the select below cannot match it).
		r.logger.Warn("channels: floor turn waiter already registered; advancing on timeout only",
			zap.String("channel_id", msg.ChannelID),
			zap.String("speaker", speaker.ParticipantID),
			zap.Error(err))
		replyCh = nil
	} else {
		defer cancel()
	}

	if err := r.dispatchTo(ctx, msg, ct, threadParentSenderID, speaker, channelSize, floorMentions, dispatchControl{}); err != nil {
		// Fire-and-forget by contract (the warn lives in dispatchTo) — but the
		// bounded close's redelivery marker must know this speaker never got
		// the stimulus live (PR #718 review; live_delivery.go).
		failures.record(speaker.ParticipantID)
	}

	timer := time.NewTimer(turnTimeout)
	defer timer.Stop()
	select {
	case <-replyCh:
		// Speaker's reply landed and was persisted; the next speaker will
		// read it from history. Advance.
		r.recordFloorTurn(ctx, ct, floorOutcomeReplied)
		return true
	case <-timer.C:
		// Speaker stayed silent past its turn budget; advance rather than
		// stall the round (D2). A candidate the response gate ultimately
		// suppressed reaches here too — harmless, the loop just moves on.
		// The waiter-already-registered path (replyCh == nil) also lands
		// here: the loop never saw this speaker's reply, so it is a timeout
		// for telemetry purposes.
		r.recordFloorTurn(ctx, ct, floorOutcomeTimeout)
		return false
	}
}

// ResolveFloorControl applies RFC 0030 Layer 2.5 floor-control settings to
// every group channel known at startup, so a channel behaves the same whether
// it was declared in `config/channels.yaml` or created at runtime via
// `POST /api/v1/channels` (the path the RFC 0048 console "New channel" form
// drives) — including a runtime-created channel that was persisted in the store
// by a prior process and is therefore present at startup but absent from config.
//
// Resolution, config taking precedence:
//   - Each config-declared channel uses its per-channel resolved value
//     ([ChannelConfig.FloorControlEnabled]), so an explicit `floor_control: false`
//     opt-out is honoured.
//   - Every other group channel present in the store defaults ON (the group
//     default), with the canonical 45s per-turn timeout.
//
// DM and thread channels are skipped: floor control is a no-op below two
// responders, and these are single-responder / sub-conversation contexts. The
// per-process create handler ([Server.handleCreateChannel]) resolves channels
// created *after* startup; this method covers the startup snapshot. Call once
// after [ChannelRouter.ReconcileConfig]; it is idempotent.
func (r *ChannelRouter) ResolveFloorControl(ctx context.Context, cfg *Config) error {
	configured := make(map[string]bool)
	if cfg != nil {
		for _, decl := range cfg.Channels {
			id := decl.CanonicalID()
			configured[id] = true
			r.SetFloorControl(id, decl.FloorControlEnabled(),
				time.Duration(decl.FloorTurnTimeoutSeconds)*time.Second)
		}
	}
	// limit <= 0 returns every row; the group-channel count is bounded by
	// `max_channels` (default 50), so the single unpaged scan is cheap.
	all, err := r.store.ListChannels(ctx, 0, "")
	if err != nil {
		return fmt.Errorf("channels: resolve floor control: list channels: %w", err)
	}
	for _, ch := range all {
		if ch.Type != ChannelTypeGroup || configured[ch.ID] {
			continue
		}
		// Store-resident group channel not in config — a runtime-created
		// channel that survived a restart. Default ON (SetFloorControl
		// normalizes the zero timeout to the 45s default).
		r.SetFloorControl(ch.ID, true, 0)
	}
	return nil
}
