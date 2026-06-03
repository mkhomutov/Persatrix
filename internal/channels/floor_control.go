package channels

import "sync"

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

	// Split into mentioned vs. unmentioned responders so the final
	// concatenation is mentioned-first while preserving member order
	// within each group (stable tie-break).
	var mentionedResp, unmentionedResp []Member
	for _, m := range members {
		if m.ParticipantID == msg.SenderID {
			continue // never reply to self
		}
		if m.RespondPolicy == RespondNever {
			continue // read-on-demand; no dispatch, off both sets
		}

		isMentioned := mentioned[m.ParticipantID]
		isCandidate := false
		switch m.RespondPolicy {
		case RespondAlways:
			isCandidate = true
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
