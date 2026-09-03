package wallet

// synthesis_reserve.go — RFC 0052 (v0.3.11) PR 4a, the close-path accounting half
// of the bounded close. New wallet accounting with no shipped analog, carved out
// of wallet.go (at the review-friendly size cap) alongside the Layer 1 ceiling it
// extends in interaction_budget.go.
//
// An autonomous interaction MUST terminate AND always leave both artifacts — the
// chair synthesis turn and a per-persona RFC 0020 summary — even on a budget-
// exhausted close ([RFC 0052 §D](../../docs/rfcs/0052-autonomous-agent-channels.md)).
// The fail-closed per-interaction cost ceiling (interaction_budget.go) would
// otherwise deny the very leases that PRODUCE those artifacts once the discussion
// has spent the cap. The reserve prevents that: it splits the cap into
//
//   - a SOFT threshold ([SynthesisSoftBudgetTokens]) the DISCUSSION is bounded by,
//     and
//   - a RESERVE ([SynthesisReserveTokens]) held back for the close path.
//
// When running spend ([WalletService.InteractionSpend]) reaches the soft
// threshold, the bounded close (PR 4b) synthesizes-and-closes so the close path's
// leases have hard-cap headroom. NOTE the headroom is only as large as the spend
// NOT YET made when the trigger samples: the soft threshold is checked once per
// fanout tail, and [AcquireLease] enforces one undifferentiated hard cap, so
// leases that land between the last sample and the close (a crossing floor round;
// an armed-window straggler) can consume the reserve first-come-first-served. The
// reserve GUARANTEES the trigger fires early, not that the held-back tokens
// survive to the close — that stronger property needs a discussion-cause soft-cap
// check in the wallet (the reserve-preservation gap noted in the package doc).
// The reserve is sized for `1 + R` close-path calls: the chair synthesis turn
// PLUS one RFC 0020 summary per close-derived RECORD. OQ #6 meters that summary
// so it counts toward the cap, and the close summary is authored per-agent
// (close_path.py spawns one finalize_closed_interaction per agent_id), so the
// summary calls land on the shared per-interaction budget.
//
// `R` WAS the persona roster N — one summary per persona — and stopped being it
// at the v0.3.15 `(principal, speaker, scope)` re-key (ISSUE-0123 / ISSUE-0131,
// residuals PR 3). A room no longer holds one interaction record per persona; it
// holds one per persona PER `(principal, speaker)` pair that persona observed,
// and the close fan closes every one of them, each authoring — and metering —
// its own summary. So the multiplier is `personas × principals × speakers`
// ([CloseRecordUpperBound] derives the room's bound), and `1 + N` under-sized the
// reserve by that factor for two releases' worth of group rooms. Under-sizing is
// SILENT: a denied close-path lease commits the RFC 0020 janitor's
// `"[interaction summary unavailable]"` placeholder and nothing retries it.
//
// KNOWN GAP (tracked, not fixed here — but no longer SILENT): the half-cap clamp
// on [SynthesisReserveTokens] guarantees the DISCUSSION a positive working budget,
// but on a small cap with a large roster it can hold back LESS than the raw
// `1 + R` sizing calls for — the two guarantees ("discussion survives" and "every
// record's close summary is funded") trade off against each other, and this
// accounting picks the former. When the clamp bites, the close path can still
// exhaust the (smaller) reserve mid-close, and the records whose summary lease is
// then denied fall through to the RFC 0020 janitor's `"[interaction summary
// unavailable]"` placeholder — the same documented floor as any other non-budget
// synthesis failure, not a new failure mode.
//
// WHAT THE v0.3.15 RE-SIZE CHANGED ABOUT IT: the third dimension makes this clamp
// bite in the NORMAL case rather than the edge case. `R` grows with the CUBE of
// the room (see [CloseRecordUpperBound] — the partition argument divides the naive
// `channelSize³` by about four, it does not drop a degree), so a mid-size roster
// against a six-figure cap now clamps where a persona-count roster did not, and an
// all-agent room under `auth.mode: disabled` — one `local` principal, N speakers,
// N personas — is exactly that shape. The consequence is bounded and in the safe direction:
// a clamped reserve is half the cap, so the discussion gets half its budget and
// the close fires earlier, never later. The consequence that is NOT bounded is the
// under-funded close, so the re-size ships the clamp's SIGNAL with it —
// [SynthesisReserveClamped] is the predicate, and the bounded close warns and
// counts on it (`channel.conversation.synthesis_reserve_clamped`). The CONSTANT
// stays deliberately un-guessed: the calibration is
// [ISSUE-0138](../../docs/issues/ISSUE-0138-close-reserve-multiplier-calibration.md),
// filed in the ISSUE-0109 idiom off that telemetry rather than picked here. Its
// alternatives — a per-roster minimum cap, scoping the metered summary to a single
// designated close-summarizer per RFC §D, or sizing off the interaction's OBSERVED
// `(principal, speaker)` pairs instead of the room's worst case — are calibration
// decisions, not accounting-layer ones.
//
// KNOWN GAP #2 (tracked, not fixed here): even when the clamp does NOT bite, the
// `1` of `1 + R` under-sizes the CHAIR synthesis turn — and this is the more likely
// §D failure of the two. [DefaultSynthesisCallReserveTokens] is derived from the
// BOUNDED RFC 0020 summary (a fixed input window), but the chair turn synthesizes
// over the FULL discussion context — its input scales with the discussion, up to
// ~[SynthesisSoftBudgetTokens] worth of tokens — so its true per-call cost can
// exceed the whole reserve on a large discussion under ANY cap, clamped or not.
// When it does, the hard cap denies the CHAIR TURN ITSELF (not just a tail-end
// persona summary), and "always produce an artifact" fails for the one artifact the
// reserve most exists to protect. This is INDEPENDENT of and ADDITIVE to the clamp
// gap above. The flat placeholder ships here; the real per-call reserve for the
// chair turn — a FRACTION of the soft budget, not a flat unit — is the same
// ISSUE-0138 / OQ #5 calibration decision (see the PR-plan "Deep-review
// follow-ups").
//
// PR 4a shipped these DARK. Since then PR 4b-i wired the bounded close to
// [WalletService.InteractionSpend] / [SynthesisSoftBudgetTokens] as its
// soft-budget trigger — but note that remains a ROUTER-SIDE trigger POINT, not
// wallet-enforced funding: [AcquireLease] still enforces only the single hard cap
// and does not know a close-path lease from a discussion lease, so a crossing
// floor round or an armed-window straggler can spend into the reserve before the
// chair turn / summaries lease it (a reserve-PRESERVATION gap distinct from the
// two sizing gaps below; tracked on the same PR 4b/OQ #5 calibration list — a
// real reservation needs a discussion-cause soft-cap check in [AcquireLease]).
// [WalletService.EvictInteraction] is still UNCALLED by any production path:
// RFC 0052 PR 7 (standing channels) owns wiring it into the close, where the
// residue leak bites and the schedule timer supplies the settle point its
// cross-process precondition needs (see [WalletService.EvictInteraction]).

// DefaultSynthesisCallReserveTokens is the conservative worst-case token cost of
// ONE close-path LLM call, used to size the reserve. It tracks the RFC 0020 close
// summary's bounds (agents/persona_runtime/summarize_close.py:
// SUMMARIZATION_TARGET_TOKENS=2000 input context + SUMMARIZATION_MAX_OUTPUT_TOKENS=1024
// output ≈ 3024) with headroom for the prompt/envelope overhead.
//
// The SAME unit also stands in for the chair synthesis turn (the "1" of `1 + R`).
// That is a distinct call shape — a goal-directed synthesis over the full discussion
// context, not a bounded per-persona summary — and is NOT actually bounded in size by
// this constant; the Layer-0 depth cap (RFC 0030) bounds recursion *depth*, not a
// single call's token cost, so it is not a sizing argument for reusing this unit.
// Reusing the summary-derived unit for the chair turn is a placeholder, not a
// verified bound: a real per-call reserve for the chair turn is one of the OQ #5
// calibration inputs (see the package doc's KNOWN GAP note), not settled by this
// constant.
//
// SOAK-VALIDATED (ISSUE-0109, v0.3.11 live calibration): unchanged at 3500.
// Across 7 live arcs (3–4-seat rosters, 200k caps, single- and four-vendor)
// every close path — chair synthesis turn + all per-persona summaries — fit
// inside the `1 + N` reserve with zero close-path lease denials (post-F-1/
// ISSUE-0111), and no arc's discussion spend even reached the soft threshold
// (peak utilization 0.59 of cap). Neither KNOWN GAP bit live. The
// `interaction_cap_utilization` close histogram (ISSUE-0109) now feeds the
// next calibration pass off telemetry.
//
// THAT SOAK DOES NOT CARRY FORWARD to the v0.3.15 multiplier, and re-reading it
// as if it did is the trap this note exists to close. BOTH of its findings are
// measured against the old sizing:
//
//   - The per-CALL unit was validated against `1 + N` records, never `1 + R` —
//     those arcs ran a pre-re-key close authoring ONE summary per persona. The
//     unit itself is unchanged and still tracks one bounded RFC 0020 summary;
//     what changed is how many of them a close issues.
//   - "No arc reached the soft threshold" was measured against a threshold the
//     re-size LOWERED, so it inverts on the rosters it was collected from. A
//     4-seat room at a 200k cap is sized for 129 500 close-path tokens (R = 36),
//     which the half-cap clamp cuts to 100 000 and leaves the discussion 100 000
//     — under the 0.59 × 200k = 118 000 peak that same soak recorded. That arc
//     would now close on `cost` where it ran on, and since the config CLAMPS,
//     [SynthesisReserveClamped] says so on every close. The 3-seat row is the
//     quiet one: 73 500 reserve, 126 500 for the discussion — still above the
//     peak, but down from the 186 000 the `1 + N` sizing left it, with no
//     counter, because an unclamped reserve is the honest full sizing. The
//     operator-facing form of both is in the channels guide's sizing table.
//
// ISSUE-0138 owns the re-soak.
const DefaultSynthesisCallReserveTokens int64 = 3500

// maxSynthesisReserveNumerator / maxSynthesisReserveDenominator cap the reserve at
// HALF the cap, so the discussion always retains a positive working budget even
// under a tiny per-interaction cap where the raw `1 + R` reserve would otherwise
// swallow the whole ceiling and starve the conversation to nothing. This clamp
// protects the DISCUSSION, not the close: it does NOT scale down with the record
// count, so it bites for any cap/room combination where `(1 + R) × unit` exceeds
// half the cap — which since the v0.3.15 re-size includes ordinary configs, not
// only tiny caps or full [DefaultSalienceMaxChannelMembers]-sized rosters. When it
// bites, the close path is the one left under-funded — see the package doc's
// KNOWN GAP note, and [SynthesisReserveClamped] for the signal that says so.
// Expressed as a fraction (not a float) to stay integer-exact.
const (
	maxSynthesisReserveNumerator   int64 = 1
	maxSynthesisReserveDenominator int64 = 2
)

// CloseRecordUpperBound returns R: the largest number of close-derived RFC 0020
// summaries a room of `channelSize` members can issue at one close, and therefore
// the reserve's multiplier ([SynthesisReserveTokens]).
//
// The v0.3.15 record key is `(principal, speaker, scope)` (ISSUE-0123 /
// ISSUE-0131), so one close fans `personas × principals × speakers` summaries:
// every persona holds one record per `(principal, speaker)` pair it observed, and
// the room-wide close fan closes all of them.
//
// Each dimension is bounded by the room, and the bound is TIGHTER than the naive
// `channelSize² × (channelSize + [ControlSenderSpeakers])` because the three are
// not independent:
//
//   - speakers <= channelSize + [ControlSenderSpeakers]. Any member can speak,
//     persona or person — and so can the orchestrator's own synthetic control
//     senders, which are NOT members. See that constant: the speaker axis is the
//     one dimension the roster does not bound.
//   - personas <= channelSize. Only a persona authors a summary.
//   - principals <= (channelSize - personas) + 1. A principal is a TENANT, and a
//     tenant enters a room only by an authenticated PERSON publishing into it, so
//     the personas and the principal-bearing members partition the roster. The +1
//     is the shared `local` bucket every agent-origin turn, autonomous tick and
//     `auth.mode: disabled` deployment resolves to.
//
// Maximising `p × (channelSize - p + 1)` over the partition point p gives
// `⌊(channelSize+1)²/4⌋`, so R = ⌊(channelSize+1)²/4⌋ × (channelSize +
// [ControlSenderSpeakers]). That is still CUBIC in the room — the partition
// divides the naive product by about four (`R / channelSize³ → ¼`), it does not
// drop a degree — and the constant factor is what decides the clamp at ordinary
// caps rather than the growth rate: a 4-seat room bounds at 36 records against
// the naive 96. Neither fits a 200k cap unclamped, which is why the clamp's
// signal ships with the multiplier rather than after it.
//
// It is still a WORST CASE, deliberately: `channelSize` is the fanout's member
// count, which includes observer/operator seats that author no summary and speak
// in no record; the control-sender allowance assumes every persona holds a record
// for BOTH directives when at most one persona receives each per interaction; and
// a real interaction touches only the pairs that actually spoke. Over-sizing
// holds back a larger reserve and trips the close earlier — the safe direction.
// Sizing off the interaction's OBSERVED pairs instead is ISSUE-0138's to decide,
// since it needs per-interaction state the trigger does not carry today.
//
// A non-positive `channelSize` returns 0 (the chair-only reserve). The result is
// saturated at [maxCloseRecords] so an absurd roster cannot overflow the int64
// multiply in [SynthesisReserveTokens]; the clamp has long since taken over by
// then, so the saturation changes no reserve any caller sees.
func CloseRecordUpperBound(channelSize int) int {
	if channelSize <= 0 {
		return 0
	}
	if channelSize > maxCloseRecordChannelSize {
		return maxCloseRecords
	}
	// ⌊(c+1)²/4⌋ = max over p of p × (c-p+1) — the persona/principal partition.
	pairs := ((channelSize + 1) * (channelSize + 1)) / 4
	return pairs * (channelSize + ControlSenderSpeakers)
}

// ControlSenderSpeakers is the number of SYNTHETIC senders that can key a close
// record without holding a seat in the room — the speaker-axis term
// [CloseRecordUpperBound] adds to the member count.
//
// The record key's speaker half is the ingested event's `sender_id`
// (`frozen_open_capture`, agents/persona_runtime/turn_payload.py), and the
// orchestrator authors two directives under senders that are deliberately NOT
// valid participant ids: `channels.ConveneDispatchSenderID` (RFC 0052 §B, the
// opening turn) and `channels.SynthesisDispatchSenderID` (§D, the closing
// synthesis turn). The response gate ADMITS both — they are forced-turn markers,
// not peer messages — so each is ingested and opens a record of its own on the
// persona it was directed at, and the room-wide close fan closes and METERS that
// record like any other.
//
// Without this term the bound is not a bound. A 3-seat room (one authenticated
// operator + two personas) sits exactly at the partition maximum — 2 personas ×
// 2 principals × 3 member speakers = 12 = ⌊4²/4⌋ × 3 — so it has ZERO slack, and
// the convener's `orchestrator:convene` record plus the chair's
// `orchestrator:synthesis` record push the true count past it. The shortfall is
// the silent kind: a denied close-path lease commits the RFC 0020 janitor's
// `"[interaction summary unavailable]"` placeholder and nothing retries it.
//
// The wallet cannot import `channels` (that is the cycle direction), so the count
// is stated here and PINNED against the enumerated senders on the channels side —
// `TestOrchestratorDispatchSenders_MatchTheReserveSpeakerAllowance`. A third
// synthetic sender must raise this constant, and that test is what says so.
const ControlSenderSpeakers = 2

// maxCloseRecordChannelSize / maxCloseRecords saturate [CloseRecordUpperBound].
// The bound is cubic in the member count, so an unbounded roster would overflow
// the `R × unit` multiply; every value at or above the saturation clamps to half
// the cap regardless, so this is arithmetic hygiene rather than a policy choice.
//
// 2046 is the LARGEST channel size whose bound still fits a 32-bit `int`
// (`⌊2047²/4⌋ × 2048 = 2 145 386 496`, just under `math.MaxInt32`; 2047 folds to
// 2 148 532 224 and overflows it). [CloseRecordUpperBound] returns `int`, so a
// saturation past that point is a compile error on 32-bit GOARCH — which is how
// the first shape of this constant shipped: 4096 folds to 17 188 257 792 and
// `GOARCH=386 go build ./internal/wallet` failed on it. The ceiling moved down by
// one when [ControlSenderSpeakers] joined the speaker term, which is the whole
// reason it is derived here rather than written as a literal. Two orders of
// magnitude past [DefaultSalienceMaxChannelMembers] is headroom the clamp
// swallowed long ago, so the lower ceiling costs no caller a token of reserve.
const (
	maxCloseRecordChannelSize = 2046
	maxCloseRecords           = ((maxCloseRecordChannelSize + 1) * (maxCloseRecordChannelSize + 1) / 4) *
		(maxCloseRecordChannelSize + ControlSenderSpeakers)
)

// SynthesisReserveTokens returns the tokens held back from a per-interaction cost
// cap for the bounded close path: the chair synthesis turn + one RFC 0020 summary
// per close-derived RECORD = `1 + closeRecords` close-path LLM calls (RFC 0052
// OQ #6 — the close summary is authored per-agent, and since the v0.3.15 re-key
// per `(principal, speaker)` record within that agent).
//
// `closeRecords` is R — [CloseRecordUpperBound] derives it from the room. It is
// NOT the persona roster: passing a persona count here re-introduces the
// under-sizing the re-size closed. A negative value is clamped to zero (the
// chair-only reserve). An uncapped interaction (`budgetTokens <= 0`) has no
// ceiling to carve and reserves nothing. The reserve is clamped to at most half
// the cap ([maxSynthesisReserve*]) so the soft threshold
// ([SynthesisSoftBudgetTokens]) is always positive —
// [SynthesisReserveClamped] reports when that clamp took effect. A caller that
// needs BOTH — the reserve and whether it was clamped, as the bounded close's
// signal does — should take [SynthesisReserve] instead of asking twice.
func SynthesisReserveTokens(budgetTokens int64, closeRecords int) int64 {
	reserve, _ := SynthesisReserve(budgetTokens, closeRecords)
	return reserve
}

// SynthesisReserveClamped reports whether [SynthesisReserveTokens] held back LESS
// than the close path is sized to need — the half-cap clamp biting.
//
// This is the KNOWN GAP's signal, and it ships WITH the v0.3.15 multiplier
// deliberately: the re-size makes the clamp bite in ordinary configs, and the
// failure it leads to is silent (a denied close-path lease commits the janitor's
// `"[interaction summary unavailable]"` placeholder, which nothing retries). A
// true return means the operator's cap cannot fund this room's close and some
// records will degrade to the placeholder — the input the ISSUE-0138 calibration
// needs, and the one thing the accounting layer can say without guessing a
// constant. False on an uncapped interaction, which reserves nothing and clamps
// nothing.
func SynthesisReserveClamped(budgetTokens int64, closeRecords int) bool {
	_, clamped := SynthesisReserve(budgetTokens, closeRecords)
	return clamped
}

// SynthesisReserve is the ONE evaluation of the sizing rule: it returns the
// reserve [SynthesisReserveTokens] would return AND whether the half-cap clamp
// took effect, from a single pass. [SynthesisReserveTokens] and
// [SynthesisReserveClamped] are projections of it, which is what makes the
// number and the signal describing it structurally incapable of drifting — a
// clamp signal derived from a second spelling of the sizing rule is a signal
// that can be wrong about the number it describes.
//
// Prefer it wherever both halves are wanted at once (the bounded close's warn +
// counter): the two-call spelling evaluates the raw sizing three times and the
// half-cap ceiling twice for one close, all of it eagerly, since a `zap` field
// list is built whether or not the entry is emitted.
func SynthesisReserve(budgetTokens int64, closeRecords int) (reserve int64, clamped bool) {
	if budgetTokens <= 0 {
		return 0, false
	}
	if closeRecords < 0 {
		closeRecords = 0
	}
	if closeRecords > maxCloseRecords {
		closeRecords = maxCloseRecords
	}
	// The UNCLAMPED sizing: 1 chair synthesis turn + R per-record summaries.
	raw := int64(1+closeRecords) * DefaultSynthesisCallReserveTokens
	// The half-cap clamp ceiling ([maxSynthesisReserve*]).
	ceiling := budgetTokens * maxSynthesisReserveNumerator / maxSynthesisReserveDenominator
	if raw > ceiling {
		return ceiling, true
	}
	return raw, false
}

// SynthesisSoftBudgetTokens returns the working budget the DISCUSSION is bounded by
// — the per-interaction cap minus the synthesis reserve. When running spend reaches
// this soft threshold the bounded close (PR 4b-i) synthesizes-and-closes so the
// close-path leases have hard-cap headroom (the reserve holds it back). See the
// package doc's reserve-PRESERVATION note: because [AcquireLease] enforces one
// undifferentiated hard cap, this trigger bounds when the close STARTS, not that
// the reserve survives to fund it. An uncapped interaction (`budgetTokens <= 0`)
// has no soft threshold (returns 0).
//
// `closeRecords` is the SAME R the reserve is carved for — see
// [CloseRecordUpperBound]. The two bases must not diverge: a threshold derived
// from a smaller room than the reserve fires the close at a spend the reserve
// cannot cover, which is the "close leases denied" hole the reserve exists to
// close (bounded_close.go states the rule at the call site).
func SynthesisSoftBudgetTokens(budgetTokens int64, closeRecords int) int64 {
	if budgetTokens <= 0 {
		return 0
	}
	return budgetTokens - SynthesisReserveTokens(budgetTokens, closeRecords)
}

// InteractionSpend returns the running token total the wallet tracks for
// interactionID — the worst-case estimate folded in on each grant
// (recordInteractionGrantLocked), reconciled to actuals on settle. It is the value
// the bounded close (PR 4b) compares against [SynthesisSoftBudgetTokens] to decide
// when to synthesize-and-close. An untracked id (never seen, uncapped, already
// evicted, or the empty id) reads as zero. Takes w.mu for the map read, the same
// (non-reentrant) lock the grant/finalize helpers in interaction_budget.go hold —
// like those helpers, this must never be called while the caller already holds w.mu.
func (w *WalletService) InteractionSpend(interactionID string) int64 {
	if interactionID == "" {
		return 0
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.interactionTokens[interactionID]
}

// EvictInteraction drops interactionID's running-total entry from the wallet, the
// interaction-lifecycle eviction the shipped wallet lacks: recordInteractionGrantLocked
// documents that a capped interaction that settled non-zero spend "keeps its entry
// for the orchestrator's process lifetime — nothing currently evicts it", so a
// standing autonomous channel would leak one map entry per convening (RFC 0052
// PR 7). NOTE it is not yet called by any production path: RFC 0052 PR 7 will call
// this on interaction close — AFTER the close path's leases have settled — so the
// entry is released once the interaction is done. The PR 4b-i bounded close
// deliberately does NOT evict (see bounded_close.go), so the residue accumulates
// until PR 7 lands. Returns whether an entry was removed (idempotent: a second call, an
// untracked id, or the empty id is a no-op returning false). Takes w.mu, the same
// (non-reentrant) lock the grant/finalize helpers hold — must never be called while
// the caller already holds w.mu.
//
// PRECONDITION the caller must uphold: every lease of interactionID must have
// already settled (granted, then either finalized or reversed) before this is
// called — including the close path's own leases, not just the discussion's. A
// lease that grants (recordInteractionGrantLocked) AFTER this eviction re-creates
// the map entry from zero, silently discarding the running total this call just
// dropped and letting that lease's spend (plus everything after it) evade the
// interaction's cost ceiling for the rest of that interaction's life.
//
// PR 4a ships NO barrier to ENFORCE that ordering — and it is not trivially
// upheld: the close-path per-persona summaries are fire-and-forget background tasks
// (agents/persona_runtime/close_path.py) spawned by N independent, CROSS-PROCESS
// persona runtimes that close their views at independent times, so nothing here can
// observe that all their (OQ #6-metered) leases have settled. PR 7 must supply the
// settle ordering — an all-agents-finalized signal, or a settle/refcount barrier —
// before calling this, rather than treating the precondition as a checkable caller
// contract. See the RFC 0052 PR-plan "Deep-review follow-ups" [tracked].
func (w *WalletService) EvictInteraction(interactionID string) bool {
	if interactionID == "" {
		return false
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if _, ok := w.interactionTokens[interactionID]; !ok {
		return false
	}
	delete(w.interactionTokens, interactionID)
	return true
}
