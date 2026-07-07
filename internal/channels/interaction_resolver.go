package channels

// interaction_resolver.go — the RFC 0030 interaction-id producer
// (docs/rfcs/0030-interaction-id-producer-pr-plan.md, PR 1). The resolver is
// the orchestrator-side authority for "which interaction is this publish part
// of" (IP1): [ChannelRouter.publishCommit] calls it next to the cascade-depth
// clamp and stamps the resolved id onto the message metadata, where it
// persists and rides the existing fanout lift to
// `ChannelMessageEvent.interaction_id`. Inbound claims never key governance
// state (IP2) — the resolution replaces them, so the per-interaction maps
// (`replyCounts`, `endVotes`, `closedInteractions`) are only ever keyed by
// router-minted uuids.
//
// Scope (IP3): one open interaction per channel (deliberately per channel,
// not RFC 0020 §G's per-agent — the governance layers compose only on a
// shared key). `group`/`dm` rotate lazily on the publish path once the idle
// window passes; `thread` channels never rotate (the thread IS the
// interaction).
//
// Rotation defers the discard seams one generation (IP4): retiring an id
// emits `interaction_closed{trigger=idle}` immediately, but its
// `DiscardInteractionReplyBudget`/`DiscardInteractionEndVotes` fire at the
// channel's NEXT rotation. The one-generation grace closes a commit-path
// race — `publishCommit` runs on each caller's goroutine with per-hook leaf
// mutexes, so a concurrent commit that resolved the old id just before
// rotation can bump `replyCounts[old]` after an immediate discard, recreating
// the lifetime entry the seam exists to prune. Deferring lets every in-flight
// commit (milliseconds) drain long before the seams fire — ≥ one idle window
// later on the idle path; generational (the channel's next rotation OR next
// vote-close), not time-bounded, when quorum closes chain — and keeps the
// `closedInteractions` tombstone alive across a Layer 4 close so the landed
// post-close self-heal keeps working in the interim. In a channel that never
// rotates (thread, or explicit 0 window) the next vote-close is the ONLY
// discharge point, so the most recent closed id's tombstone persists there —
// at most one per channel, the deliberate bounded residue. At most one
// pending retiree per channel, and a rejected publish never retains an entry
// ([ChannelRouter.settleInteraction] deletes a never-committed one), so the
// table holds ≤ 2 ids per channel WITH PERSISTED HISTORY — plus the RFC 0052
// no-reopen ledger's ≤ [postCloseLatchGenerations] deliberately closed ids
// (interaction_close_latch.go) — bounded by real channels, not by
// caller-supplied channel ids, and not a leak.
//
// The Layer 4 quorum close notifies the resolver via
// [ChannelRouter.markInteractionClosed] (IP8) so the next publish mints fresh
// per RFC 0020 §C never-reopen — without it, the resolver would keep stamping
// the closed id for up to a full idle window and every publish in it would be
// post-close-suppressed: a quorum would silence the channel instead of ending
// one conversation.
//
// Restart (IP5): the table is in-memory; a restart loses it and the next
// publish mints fresh — RFC 0020 §C inheritance. The maps keyed by the lost
// ids died with the process too, so nothing leaks.

import (
	"context"
	"time"

	"github.com/google/uuid"
	"go.uber.org/zap"
)

// idleTrigger is the `trigger` attribute value on the
// `channel.conversation.interaction_closed` counter for a lazy idle rotation —
// the sibling of [endVotesTrigger] on the same instrument (§L; the
// governance-layers plan reserved `idle`/`structural`/`cost`).
const idleTrigger = "idle"

// openInteraction is one channel's resolver entry: the open interaction id,
// its idle clock, and the one pending retiree whose discard seams fire at the
// next rotation (IP4). A vote-close empties `id` (next resolve mints fresh)
// while parking the closed id in `retired` so its tombstone outlives any
// racing commit.
//
// `idCommitted` records whether the current `id` has at least one PERSISTED
// publish, and `lastActivity` is the time of the channel's last persisted
// publish — both written by [ChannelRouter.settleInteraction], never by the
// resolve itself, so a rejected publish (non-member, throttled, store error)
// is invisible to the idle clock. A minted-but-uncommitted id is tentative:
// it never idle-rotates (rotating it would emit `interaction_closed` for an
// interaction containing zero messages) and is adopted by the next committed
// publish instead.
// `prev` records the channel's most recently CLOSED interaction and the
// trigger that closed it ([idleTrigger] / [endVotesTrigger]) — the producer
// plan OQ 5 close-cause attribution [ChannelRouter.publishCommit] stamps
// onto every publish of the successor interaction. One [previousClose]
// field rather than two parallel strings (PR 607 second-pass review): the
// pair is only ever written and read as a unit, and a future close path
// setting one half without the other would silently produce a
// half-attributed cause. Deliberately separate from `retired`: that slot is
// the discard-seam ledger and can also park an orphaned commit
// ([ChannelRouter.settleInteraction]) — an interleaving artefact, not a
// close — so it cannot double as the close-cause record. The pair persists
// until the channel's next close overwrites it; in-memory like the rest of
// the table, so a restart re-mints with no attribution (IP5 — receivers
// treat absent as unknown).
type openInteraction struct {
	id           string
	idCommitted  bool
	lastActivity time.Time
	retired      string
	prev         previousClose
	// chairEscalated is the chair-stall-escalation amendment's CE5 ration:
	// true once this interaction's one forced turn has been dispatched. It
	// rides the entry deliberately — rotation/close replaces the entry's id
	// (and a fresh mint starts unmarked), so the mark dies with the
	// interaction and needs no lifetime map. Guarded by interactionMu;
	// written via [ChannelRouter.markChairEscalated].
	chairEscalated bool
	// escalatedStimulus is the stalled stimulus the first forced turn was
	// built from (ISSUE-0099), cloned and stashed when that turn dispatches
	// ([ChannelRouter.storeEscalatedStimulus]). The resynthesize re-dispatch
	// reuses it rather than the chair's misfired reply: re-sending the chair's
	// own reply to the chair would self-suppress at the gate (the same
	// `self_sender` defence that makes a plain refund inert), whereas this
	// carries the ORIGINAL non-chair sender. nil until escalated; rides the
	// entry and dies with the interaction like chairEscalated.
	//
	// It doubles as the ISSUE-0099 "armed" bit and the loop guard:
	// [ChannelRouter.claimChairReply] consumes it (back to nil) on the chair's
	// FIRST publish after the forced turn — the forced-turn reply — whether or
	// not that reply misfired. So a clean hand-off disarms the trigger, and a
	// later innocuous chair message naming no floor-capable member can no
	// longer be mistaken for the reply's misfire; a second misfire likewise
	// finds nothing to claim and stands.
	escalatedStimulus *ChannelMessage
	// escalatedThreadParent is the thread-parent attribution the first forced
	// turn carried (ISSUE-0099), stashed beside escalatedStimulus so the
	// re-dispatch reproduces the ORIGINAL stimulus's thread context rather than
	// the misfired reply's — the reply is a different message in the tree.
	// Meaningless ("") off threads, like the value it mirrors.
	escalatedThreadParent string
	// roundCount is the RFC 0052 bounded-close round tally (v0.3.11 PR 4b): the
	// number of fanout cycles this autonomous interaction has run (one floor round
	// under floor control, one message without it), advanced once per fanout by
	// [ChannelRouter.advanceBoundedCloseRound] and compared against
	// `autonomous.max_rounds` (bounded_close.go). Rides the entry like
	// chairEscalated — rotation/close replaces the id and the fresh mint below
	// zeroes it, so the tally dies with the interaction and needs no lifetime map.
	// Guarded by interactionMu.
	roundCount int
	// agendaCursor is the RFC 0052 §C anti-collapse cursor (v0.3.11 PR 6): the
	// index of the agenda item the discussion is currently on, 0 = the first item
	// the convener's opening turn posed. Advanced at most one item per stall,
	// MONOTONICALLY, by [ChannelRouter.claimConvenerCadence] — the per-agenda-item
	// generalization of the CE5 one-escalation ration AND its loop guard (an item
	// is never re-posed once advanced past, so the convener re-invites any one item
	// at most once). Rides the entry like chairEscalated (dies with the
	// generation; the fresh mint below zeroes it); guarded by interactionMu.
	agendaCursor int
	// agendaItemDiscussed records whether the CURRENT agenda item has drawn at
	// least one substantive (replied) round — the best-effort liveness target's
	// input ([ChannelRouter.recordAgendaProgress] sets it on a working round). An
	// item that reaches its stall UNdiscussed earns one re-invite before the cursor
	// advances (the RFC §C "re-invite an item rather than skip it on the first quiet
	// round" target, shipped at its default of one substantive turn per item); reset
	// on each advance. Guarded by interactionMu.
	agendaItemDiscussed bool
	// agendaItemReinvited is the current item's re-invite ration: true once its one
	// liveness re-invite has been spent, so a second stall on the same item advances
	// instead of re-inviting forever — the convener re-invites any one item at most
	// once (RFC §C 2). Reset on advance; guarded by interactionMu.
	agendaItemReinvited bool
	// recentlyClosed is the RFC 0052 no-reopen ledger: this channel's
	// deliberately closed interaction ids, newest last, bounded to
	// [postCloseLatchGenerations]. Written by the close notification, read by
	// the resolve's latch — story, scope, and lifetime in
	// interaction_close_latch.go. Guarded by interactionMu.
	recentlyClosed []string
	// pendingSynthesis is the RFC 0052 §D armed close-on-reply (PR 4b-ii):
	// non-nil while the chair's synthesis turn is outstanding — story, claim,
	// and timeout in synthesis_close.go. Rides the entry like chairEscalated
	// (dies with the generation); guarded by interactionMu.
	pendingSynthesis *pendingSynthesisClose
}

// openCommitted reports whether this entry holds an open COMMITTED
// interaction — the ONE shared predicate behind the escalation read, the
// bounded-close round advance, and the idle-rotation eligibility below
// (PR #716 review: the copied expression would drift on any tightening).
// Nil-tolerant so map-miss callers need no guard. Caller holds interactionMu.
func (e *openInteraction) openCommitted() bool {
	return e != nil && e.id != "" && e.idCommitted
}

// resolveInteractionID returns the open interaction id for `channelID`,
// minting or rotating as needed, and is the ONLY writer of the governance
// interaction key space (IP2). `inbound` is the publisher's claim, used for
// the divergence debug log and — on an AUTONOMOUS channel — for the RFC 0052
// no-reopen latch: a claim naming a deliberately closed interaction is KEPT,
// not overridden, and the fourth return reports it. The latch SCOPE (OQ #2)
// is resolved HERE, not by the caller (PR #716 review): the resolver already
// holds the channel id, so deriving the gate beside the ledger read it gates
// keeps the rule in one place — a future publish-adjacent caller (a standing
// convene, a synthesis dispatch) cannot silently opt out by copying a
// latch-less call shape. The latch reads the ledger in the SAME critical
// section that would otherwise mint, so a concurrent close cannot slip
// between the decision and the mint (the ledger's story lives in
// interaction_close_latch.go). A latched resolve touches no resolver state:
// the settle hook is a no-op and the close-cause attribution is zero (the
// publish is the closed record's tail, not a successor's boundary signal).
// Seam firing and telemetry run outside interactionMu — the discard seams
// take their own leaf mutexes, and holding two governance mutexes at once
// would mint a lock-ordering edge no other path has.
//
// The second return is the channel's OQ 5 close-cause attribution (the most
// recently closed id + its trigger, zero when none) — read under the same
// lock as the resolve so the stamped cause always belongs to the resolved
// id's own predecessor, and stamped onto the publish metadata by
// [ChannelRouter.publishCommit].
//
// The returned settle hook is the resolver's half of the reply-reservation
// pattern ([ChannelRouter.enforceReplyBudget]'s release): the caller invokes
// it exactly once, with the persist outcome, and only THAT advances the idle
// clock or retains a fresh entry — see [ChannelRouter.settleInteraction].
// The split is deliberate: minting and rotation stay visible at resolve time
// because concurrent publishes must agree on the open id and a rotated id was
// past its window regardless of this publish's fate (the rotation is lazy
// either way — only its trigger moves from "next commit" to "next attempt"),
// while everything that asserts "a publish happened" reconciles to
// persistence. Without the split, a rejected publish leaks an entry keyed by
// the caller-supplied channel id (the unauthenticated REST path reaches here
// before the store's membership check — unbounded attacker-influenceable
// growth), and a throttled participant's in-window retries hold its own
// exhausted interaction open forever, so the idle rotation that would reset
// its budget never fires.
func (r *ChannelRouter) resolveInteractionID(ctx context.Context, channelID string, ct ChannelType, inbound string) (string, previousClose, func(persisted bool), bool) {
	now := r.interactionNow()

	// The latch scope gate: only a stamped claim on an autonomous channel can
	// latch — human channels never latch and keep minting fresh (byte-for-byte
	// unchanged), and an unstamped publish (the operator, the convener's
	// opening turn) never latches, so the channel stays re-convenable (IP8).
	// AutonomousFor takes its own leaf mutex, read BEFORE interactionMu below,
	// so the resolve still holds one governance mutex at a time; the
	// short-circuit keeps unstamped (human-TYPED) traffic off that mutex.
	// Agent replies claim their dispatched-under id on every channel type
	// (the PR #716 echo); on a human channel the claim stops at this gate.
	latchClaim := inbound != "" && r.AutonomousFor(channelID).Enabled

	r.interactionMu.Lock()
	entry := r.openInteractions[channelID]
	if latchClaim && entry != nil && entry.latchedClaim(inbound) {
		r.interactionMu.Unlock()
		return inbound, previousClose{}, func(bool) {}, true
	}
	if entry == nil {
		entry = &openInteraction{}
		r.openInteractions[channelID] = entry
	}
	window := r.idleWindowLocked(channelID)
	var rotated, discard string
	// Only a committed id can idle out: an uncommitted mint has no messages,
	// so "rotating" it would close an interaction that never existed on the
	// record (and its stale lastActivity predates the mint anyway). `eligible`
	// is exactly that precondition; when it holds the gap is a rotation
	// DECISION (fired or not), logged below for ISSUE-0095 — the no-fire path
	// was otherwise traceless.
	// RFC 0052 PR 4b-ii (PR #718 review): an armed synthesis close is
	// terminating — never idle-rotate it, or the fresh-mint reset below disarms
	// the pending close without running it and silently drops the §D artifact.
	eligible := entry.openCommitted() && ct != ChannelTypeThread && window > 0 &&
		entry.pendingSynthesis == nil
	var gap time.Duration
	var lastActivity time.Time
	if eligible {
		lastActivity = entry.lastActivity
		gap = now.Sub(lastActivity)
		if gap > window {
			rotated = entry.id
			discard = entry.retired // the previous retiree's deferred seams fire now
			entry.retired = rotated
			entry.id = ""
			// OQ 5 close-cause attribution: this resolve IS the idle close, so
			// the publish that triggered it (the successor's first message)
			// already carries the cause.
			entry.prev = previousClose{id: rotated, trigger: idleTrigger}
		}
	}
	var disarmedChair string
	var disarmedTimer bool
	if entry.id == "" {
		entry.id = uuid.NewString()
		entry.idCommitted = false
		// A fresh interaction carries a fresh escalation ration (CE5): the
		// mark belongs to the id it was spent on, and the entry is reused
		// across generations. The ISSUE-0099 resynthesize state belongs to the
		// same generation — clear it in lockstep so a new interaction neither
		// inherits a spent re-dispatch nor a stale stimulus pointer.
		entry.chairEscalated = false
		entry.escalatedStimulus = nil
		entry.escalatedThreadParent = ""
		// RFC 0052: a fresh interaction bounds independently — a re-convened
		// autonomous discussion gets its own max_rounds tally, not the retiree's
		// — and never inherits an armed synthesis close (PR 4b-ii): the arm
		// belonged to the retired generation, so its reply/timer must not close
		// (or withhold traffic on) the successor. A live arm here should be
		// unreachable (idle rotation is arm-gated above; deliberate closes
		// disarm before emptying the id) — but the previous shape silently
		// DROPPED the disarm's timerStopped return, so if the branch were ever
		// reached the stopped timer's synthesisWG count leaked and every later
		// shutdown drain hung forever (PR #718 review). Route it through the
		// shared release tail like every other disarm terminal, and warn: the
		// unreachability claim is now observable instead of load-bearing.
		entry.roundCount = 0
		// RFC 0052 §C (PR 6): a re-convened discussion works its agenda from the
		// top with fresh per-item rations — the cursor/liveness state belonged to
		// the retired generation, the chairEscalated sibling.
		entry.agendaCursor = 0
		entry.agendaItemDiscussed = false
		entry.agendaItemReinvited = false
		disarmedChair, disarmedTimer = entry.disarmPendingSynthesisChairLocked()
	}
	resolved := entry.id
	prev := entry.prev
	r.interactionMu.Unlock()

	if disarmedChair != "" {
		r.logger.Warn("channels: fresh interaction mint found a live armed synthesis; disarmed defensively",
			zap.String("channel_id", channelID),
			zap.String("escalation_chair_id", disarmedChair))
	}
	r.releaseSynthesisArm(channelID, disarmedChair, disarmedTimer)

	if eligible {
		// ISSUE-0095: one line per eligible resolve (committed, non-thread,
		// window>0), fired or not — the within-window case was otherwise
		// traceless. Read the fields, not the boolean: `rotated` is set exactly
		// when `gap > window` (same lock, same values), so it is fully derivable
		// from the `gap`/`window` already on the line and a `gap > window` line
		// that reads `rotated=false` is unreachable. A genuine no-fire — wall
		// clock idle past the INTENDED window yet no rotation — surfaces instead
		// as a wrong field: `window` larger than expected (mis-resolved window),
		// or `gap`/`last_activity` out of step with wall clock (a phantom
		// publish advanced the clock, or a skewed `now`). Those are the tells.
		// Limitation: a no-fire whose cause is ineligibility (an entry left
		// uncommitted when it should hold history) takes the mint path and logs
		// nothing here. Debug keeps it cheap (one line per eligible publish);
		// note the orchestrator suppresses Debug at staging/production
		// InfoLevel — diagnosing a fleet no-fire needs --env development or a
		// raised level, same as the live MT stack runs.
		r.logger.Debug("channels: interaction idle-rotation decision",
			zap.String("channel_id", channelID),
			zap.Duration("window", window),
			zap.Time("now", now),
			zap.Time("last_activity", lastActivity),
			zap.Duration("gap", gap),
			zap.Bool("rotated", rotated != ""),
		)
	}
	r.discardInteractionGovernance(discard)
	if rotated != "" {
		r.recordInteractionClosedIdle(ctx, channelID, ct, rotated)
	}
	if inbound != "" && inbound != resolved {
		// IP2: the claim is overridden, not honoured. Debug, not warn — the
		// common producer of a stale claim is an agent echoing the id of the
		// interaction it was *dispatched* in after a rotation, which is
		// expected traffic, not an attack signature.
		r.logger.Debug("channels: inbound interaction_id claim overridden by resolver",
			zap.String("channel_id", channelID),
			zap.String("claimed", inbound),
			zap.String("resolved", resolved))
	}
	return resolved, prev, func(persisted bool) { r.settleInteraction(channelID, resolved, now, persisted) }, false
}

// settleInteraction reconciles the resolver table to the persist outcome of
// one publish (the hook [ChannelRouter.resolveInteractionID] returned).
//
// Persisted: the resolved id becomes committed and the idle clock advances to
// the resolve-time `now` — the ONLY writer of `lastActivity`, so the window
// measures channel history, not attempts (the reply budget's "counter tracks
// history, not attempts" invariant, applied to the clock). A nil entry here
// means a concurrent rejected publish that shared this tentative mint settled
// first and deleted it; recreate it so the persisted row's stamped id stays
// the channel's open interaction.
//
// Rejected: drop the entry iff this publish's tentative state is ALL it holds
// — same id, never committed, no pending retiree (a retiree implies committed
// history whose deferred discard must still fire). That makes the table's
// bound real: entries exist only for channels with at least one persisted
// publish, never for arbitrary caller-supplied channel ids.
//
// Orphaned commit: a persisted id that is no longer the entry's open id was
// stranded by an interleaving — a sibling rejected publish deleted the shared
// tentative mint and a third publish reminted before this settle ran. The
// orphan's row is already channel history, so its committed governance state
// (the reply-budget reservation, a possible vote or tombstone) must still
// reach a discard seam: park it as the pending retiree when the slot is free,
// giving it the same next-rotation/next-close discharge as any retiree. An
// OCCUPIED slot is never clobbered — the occupant's one-generation deferral
// protects a real commit racing a rotation/close (IP4), and displacing it
// early would reopen that race. The skip's residue (a settle whose persist
// spanned an entire rotation cycle leaves one untracked counter map) is
// accepted: router-minted, requires a publish in flight for a full idle
// window, and not reachable at attacker-chosen rate.
func (r *ChannelRouter) settleInteraction(channelID, resolved string, now time.Time, persisted bool) {
	r.interactionMu.Lock()
	entry := r.openInteractions[channelID]
	if !persisted {
		if entry != nil && entry.id == resolved && !entry.idCommitted && entry.retired == "" {
			delete(r.openInteractions, channelID)
		}
		r.interactionMu.Unlock()
		return
	}
	if entry == nil {
		entry = &openInteraction{id: resolved}
		r.openInteractions[channelID] = entry
	}
	// firstCommit is the open seam for the RFC 0050 Layer 1 budget snapshot: the
	// transition into committed is exactly "this interaction now exists on the
	// record", the snapshot-at-open moment. Recorded after the lock is released
	// so budgetMu never nests under interactionMu.
	firstCommit := false
	if entry.id == resolved {
		firstCommit = !entry.idCommitted
		entry.idCommitted = true
	} else if entry.retired == "" {
		// `resolved` was orphaned mid-flight (see doc): park it so the next
		// rotation/close discharges its governance state. A non-empty slot
		// already holds either `resolved` itself (the racing-commit case —
		// nothing to do) or an earlier retiree whose deferral must win.
		entry.retired = resolved
	}
	if now.After(entry.lastActivity) {
		entry.lastActivity = now
	}
	r.interactionMu.Unlock()

	if firstCommit {
		r.snapshotInteractionBudget(resolved, channelID)
	}
}
