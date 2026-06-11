// Package channels implements the internal communication layer (RFC 0011).
//
// v0.3.0 scope (Phase 1a — this package):
//   - canonical channel model (group | dm | thread) per RFC 0011 §A
//   - SQLite-backed [ChannelStore] per RFC 0011 §B (this file's interface plus
//     [NewSQLiteStore])
//   - DM channel-ID canonicalization via [ChannelStore.GetOrCreateDM]
//   - per-channel message cap with thread-FK cascade pruning
//   - global named-channel cap loaded from config/channels.yaml
//
// External bridges (Slack, Discord, etc.) remain reserved for v0.5.0 — see
// internal/bridges. Routing, REST endpoints, and the response gate land in
// later RFC 0011 PRs.
package channels

import (
	"errors"
	"fmt"
	"regexp"
	"time"
)

// ChannelType is the canonical vocabulary across the SQL schema, the proto
// `channel_type` field, the JSON Schema, and Go callers (RFC 0011 §A).
type ChannelType string

const (
	ChannelTypeGroup  ChannelType = "group"
	ChannelTypeDM     ChannelType = "dm"
	ChannelTypeThread ChannelType = "thread"
)

// Valid reports whether ct is one of the canonical channel types.
func (ct ChannelType) Valid() bool {
	switch ct {
	case ChannelTypeGroup, ChannelTypeDM, ChannelTypeThread:
		return true
	}
	return false
}

// RespondPolicy is the per-membership response-gate policy (RFC 0011 §D).
//
// The canonical internal/wire representation is the legacy triple
// (`when_mentioned`/`always`/`never`). RFC 0030's relevance amendment
// (v0.3.7) reframes the same three intents as a **disposition**
// vocabulary (`participant`/`addressed`/`observer`); the disposition
// values are accepted at config load and collapsed back to the legacy
// triple by [RespondPolicy.Normalize] so the fanout candidate set, floor
// control, and the Python response gate keep reading the canonical three
// values unchanged. The vocabulary addition is therefore behaviourally
// inert — see docs/rfcs/0030-amendment-relevance-gated-response-pr-plan.md.
type RespondPolicy string

const (
	RespondWhenMentioned RespondPolicy = "when_mentioned"
	RespondAlways        RespondPolicy = "always"
	RespondNever         RespondPolicy = "never"

	// Disposition vocabulary (RFC 0030 relevance amendment, v0.3.7).
	// Accepted at config load and normalized to the legacy triple above;
	// never the canonical internal value.
	RespondParticipant RespondPolicy = "participant"
	RespondAddressed   RespondPolicy = "addressed"
	RespondObserver    RespondPolicy = "observer"

	// RespondChair is the v0.3.8 Tier B facilitator disposition: a
	// `participant` carrying a low default salience `threshold` so it clears
	// the cheap relevance bid readily and keeps an open-floor discussion
	// moving. It normalizes to the legacy `always` wire value (so every
	// downstream reader of `respond_policy` is unchanged); its "chair-ness"
	// rides instead on the low [MemberConfig.Threshold]
	// ([DefaultChairThreshold]) plus [MemberConfig.SalienceGated], both derived
	// from the *declared* disposition via [ResolveSalienceSignal] at every write
	// boundary — config load ([MemberConfig.UnmarshalYAML]) and the REST
	// add/create paths alike. As of PR 2b those signals round-trip end-to-end
	// (the `memberships.threshold`/`salience_gated` columns persist them and the
	// `ChannelMessageEvent` wire fields deliver them to the agent-side bid), so
	// a `chair` is recoverable past the store/wire boundary — the PR-1
	// persistence/wire gap noted on [MemberConfig.Threshold] is closed. A
	// v0.3.8 `chair` CANNOT close an interaction
	// — convergence is owned by the deterministic governance layers — and its
	// Layer 5 moderator hooks are reserved/inert until v0.4.0.
	RespondChair RespondPolicy = "chair"
)

// MentionEveryone is the broadcast sentinel for the RFC 0030 relevance
// amendment Tier A directed-elsewhere filter (v0.3.7, decision D3). Its
// presence in a message's `Mentions` list marks the message as addressed
// to the whole room, disabling the directed-elsewhere suppression so every
// `always`/`participant` member stays a candidate responder. The value
// carries an `@`, which [ValidateParticipantID] forbids, so it can never
// collide with a real participant id — a safe in-band sentinel reusing the
// existing `mentions` plumbing with no new wire field. Mirrors the Python
// gate's `MENTION_EVERYONE` (agents/response_gate.py); the two must stay in
// lockstep. v0.3.7 wires the sentinel through the transport (this candidate
// set, the receiver gate, and a persist-validation exemption in
// sqlite_messages.go so it survives the wire); only the *producer* — the
// console composer expanding a typed `@everyone`/`@here` into the sentinel —
// is a follow-on.
const MentionEveryone = "@everyone"

// Normalize collapses the disposition vocabulary to the canonical legacy
// triple (`participant→always`, `addressed→when_mentioned`,
// `observer→never`). A legacy value is returned unchanged; an unknown
// value is returned as-is so the caller surfaces it via
// [RespondPolicy.Valid].
//
// The Python response gate keeps a mirror of this mapping
// (`_DISPOSITION_ALIASES` in agents/response_gate.py) as defence-in-depth
// for a disposition value that reaches the gate un-normalized; the two
// encode the same disposition→legacy mapping in different languages and
// must be kept in lockstep.
//
// Normalize is applied at every external write boundary so the membership
// store, the wire value, and every downstream reader (fanout candidate
// set, floor control, the Python response gate) see only the legacy
// triple: the config loader normalizes in [MemberConfig.UnmarshalYAML],
// and the REST/store write path normalizes in the [ChannelStore] write
// methods (AddMember/SetMemberPolicy/CreateChannelWithMembers) before the
// membership-table CHECK constraint, which only accepts the legacy three.
func (p RespondPolicy) Normalize() RespondPolicy {
	switch p {
	case RespondParticipant, RespondChair:
		// `chair` is a `participant` with a low default threshold; on the wire
		// it is indistinguishable from `always` (the low threshold rides on
		// the config struct, not the membership row). See [RespondChair].
		return RespondAlways
	case RespondAddressed:
		return RespondWhenMentioned
	case RespondObserver:
		return RespondNever
	}
	return p
}

// Valid reports whether p is one of the canonical respond policies or an
// accepted disposition alias. Callers that need the canonical value
// should call [RespondPolicy.Normalize] first (the loader does this at
// config-load time), but Valid accepts both vocabularies so a value that
// passes the JSON schema's widened enum also passes the loader.
func (p RespondPolicy) Valid() bool {
	switch p {
	case RespondWhenMentioned, RespondAlways, RespondNever,
		RespondParticipant, RespondChair, RespondAddressed, RespondObserver:
		return true
	}
	return false
}

// canonicalRespondPolicy is the single normalize-then-validate choke point
// every store write path uses before persisting a membership row. It
// collapses the disposition vocabulary to the legacy triple
// ([RespondPolicy.Normalize]) and rejects an unknown value with
// [ErrInvalidRespondPolicy].
//
// Centralizing the pair keeps the store's back-compat guarantee — that a
// disposition value never reaches the membership-table CHECK constraint,
// which only permits the legacy three — from depending on each write path
// remembering to call Normalize before Valid. Because [RespondPolicy.Valid]
// deliberately accepts both vocabularies (so a schema-valid value also
// passes the loader), a forgotten Normalize would slip past Valid and then
// surface as an opaque CHECK-constraint failure (HTTP 500) instead of
// working. A new write path now has one obvious helper to reach for.
func canonicalRespondPolicy(p RespondPolicy) (RespondPolicy, error) {
	p = p.Normalize()
	if !p.Valid() {
		return "", fmt.Errorf("%w: %q", ErrInvalidRespondPolicy, p)
	}
	return p, nil
}

// ResolveSalienceSignal derives the persisted RFC 0030 Tier B per-member signals
// from a member's *declared* disposition (before [RespondPolicy.Normalize]
// collapses it to the legacy triple). It is the derivation half of
// [ResolveMemberPolicy], which the validating write boundaries (the store's
// AddMember/SetMemberPolicy, the REST create handler) go through; the config
// loader ([MemberConfig.UnmarshalYAML]) calls it directly because it defers
// validation to [Config.Validate]. Either way the participant→bid and
// chair→low-threshold mappings live in one place rather than being re-derived
// per call site.
//
//   - salienceGated is true for the open-floor participant dispositions
//     (`participant`/`chair`): they run the salience bid. A legacy `always`
//     keeps replying unconditionally (false), so the feature stays additive.
//   - threshold defaults to `explicit` (the operator's value, possibly nil for
//     unset → bias-to-silence). A `chair` with no explicit value picks up the
//     low [DefaultChairThreshold] — its whole facilitator identity, since on
//     the wire a chair is just a participant.
//
// `explicit` lets a caller that already parsed an operator-supplied threshold
// (the config object form) thread it through; pass nil where no explicit value
// is available (the REST paths, which today carry only the disposition).
func ResolveSalienceSignal(p RespondPolicy, explicit *float64) (salienceGated bool, threshold *float64) {
	threshold = explicit
	if p == RespondChair && threshold == nil {
		d := DefaultChairThreshold
		threshold = &d
	}
	// `participant`/`chair` always run the bid. A legacy `always` opts in iff
	// the operator set an explicit threshold — the only reason to put a
	// salience bar on a member is to gate it — so `always` + `threshold` bids
	// while a *bare* `always` keeps replying unconditionally (v0.3.7
	// back-compat). `addressed`/`observer`/`when_mentioned`/`never` never reach
	// the open-floor admit, so their bid-ness is moot (and a threshold on them
	// is rejected at config load by [Config.Validate]).
	salienceGated = p == RespondParticipant || p == RespondChair ||
		(explicit != nil && p.Normalize() == RespondAlways)
	return salienceGated, threshold
}

// boolToInt maps a Go bool to the 0/1 SQLite stores in an INTEGER column
// (SQLite has no native boolean). Used for `memberships.salience_gated`.
func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

// Channel is a row in the `channels` table (RFC 0011 §B).
//
// RFC 0031 Phase 1: `SessionID` tags the row with the operator namespace
// active at create time. An empty value at the store boundary is rewritten
// to the synthetic `legacy` carve-out so older / session-unaware callers
// produce queryable rows. Recall-side filtering ships in Phase 2.
type Channel struct {
	ID          string      // canonical address: "group:planning" / "dm:a:b" / "thread:<msg-id>"
	Name        string      // group: declared name; dm/thread: empty
	Type        ChannelType // group | dm | thread
	Description string
	CreatedAt   time.Time
	SessionID   string // RFC 0031 Phase 1 — defaults to "legacy" at the store boundary
}

// Member is a row in the `memberships` table (RFC 0011 §B).
type Member struct {
	ParticipantID string
	RespondPolicy RespondPolicy
	JoinedAt      time.Time

	// SalienceGated marks this member as an open-floor *participant* subject to
	// the RFC 0030 Tier B salience bid (v0.3.8). It is true when the member opts
	// into the bid: declared with the participant vocabulary
	// (`participant`/`chair`), OR a legacy `always` carrying an explicit
	// `threshold` (the operator-set bar is itself the opt-in — see
	// [ResolveSalienceSignal]). It is false for a *bare* legacy `always`, which
	// keeps replying unconditionally. Because the disposition vocabulary
	// collapses to the legacy `always` wire value on [RespondPolicy.Normalize],
	// this boolean is the *only* thing that survives to distinguish a
	// salience-gated participant from a bare-always replier past the store/wire
	// boundary; it rides the `ChannelMessageEvent.salience_gated` proto field to
	// the agent-side seam.
	SalienceGated bool
	// Threshold is the member's per-disposition salience `threshold` for the
	// Tier B bid (the score it must clear to reach the quality turn). A
	// `*float64` tri-state mirroring [MemberConfig.Threshold]: nil → unset →
	// bias-to-silence; &0..1 → an explicit bar (a `chair` carries a low
	// default). Persisted in the nullable `memberships.threshold` column and
	// carried to the agent on the `optional double threshold` proto field.
	Threshold *float64
}

// ChannelMessage is a row in the `messages` table (RFC 0011 §B).
//
// `Mentions` is stored as a JSON array text column; `Metadata` as a JSON
// object text column. Both marshal/unmarshal at the store boundary so callers
// see typed values.
//
// RFC 0031 Phase 1: `SessionID` tags the row with the operator namespace
// active at publish time. The store rewrites empty to `legacy`. Phase 1
// ships no recall changes — the column exists so Phase 2 has a column to
// filter on without a follow-up migration.
type ChannelMessage struct {
	ID        string
	ChannelID string
	SenderID  string
	Content   string
	Timestamp time.Time
	ThreadID  string // empty when the message is not a reply
	Mentions  []string
	Metadata  map[string]any
	SessionID string // RFC 0031 Phase 1 — defaults to "legacy" at the store boundary
}

// MaxMessageContentBytes is the soft byte cap on [ChannelMessage.Content]
// at the [ChannelStore.PublishMessage] boundary (ISSUE-0050).
//
// Sized at 4× the upstream agent codepoint cap
// (`agents/channel_validation.py::_CHANNEL_CONTENT_MAX_CHARS = 4000`) so a
// well-formed agent submission near the codepoint limit (UTF-8 worst case
// 4 bytes/codepoint) still passes. Rejecting in bytes — not codepoints —
// at the store boundary is intentional: the upstream codepoint cap is the
// canonical user-facing contract, while this byte cap measures the actual
// SQLite + per-recipient gRPC fanout cost an unauthenticated REST publish
// can impose.
const MaxMessageContentBytes = 16_384

// Errors surfaced by [ChannelStore]. Callers should compare with [errors.Is].
var (
	// ErrChannelNotFound — GetChannel/PublishMessage against a missing id.
	ErrChannelNotFound = errors.New("channels: channel not found")
	// ErrMessageNotFound — GetMessage against an unknown message id. Distinct
	// from ErrChannelNotFound so callers (and the future REST layer) can map
	// 404-on-message vs. 404-on-channel without re-parsing error strings.
	ErrMessageNotFound = errors.New("channels: message not found")
	// ErrChannelExists — CreateChannel collided with an existing name (group)
	// or canonical DM id.
	ErrChannelExists = errors.New("channels: channel already exists")
	// ErrNotMember — publish from a non-member of the target channel (the
	// store-side guard; REST layer surfaces this as 403).
	ErrNotMember = errors.New("channels: sender is not a member of the channel")
	// ErrInvalidParticipantID — participant id violated the registration-time
	// constraints (no `:`, no whitespace, ASCII only) per RFC 0011 §A.
	ErrInvalidParticipantID = errors.New("channels: invalid participant id")
	// ErrInvalidChannelType — a Channel or ChannelMessage was supplied with a
	// `channel_type` outside the canonical vocabulary.
	ErrInvalidChannelType = errors.New("channels: invalid channel_type")
	// ErrInvalidRespondPolicy — a Member was supplied with a respond policy
	// outside the canonical vocabulary.
	ErrInvalidRespondPolicy = errors.New("channels: invalid respond_policy")
	// ErrInvalidThreshold — a member's per-disposition salience `threshold`
	// (RFC 0030 Tier B, v0.3.8) was not a finite value in the `[0, 1]` range.
	// The JSON schema's `minimum`/`maximum` catches an out-of-range bound at
	// `make validate`; this Go-side check is the belt-and-suspenders for
	// operators who skipped that step, and additionally rejects a non-finite
	// `.nan` — which slips past a bare range comparison (every comparison
	// against NaN is false) and which the schema's numeric bound likewise
	// fails to catch. An absent threshold is NOT an error — it is the unset
	// (bias-to-silence) default.
	ErrInvalidThreshold = errors.New("channels: invalid member threshold")
	// ErrThresholdNotApplicable — a member carried a per-disposition
	// `threshold` on a disposition that does not run the open-floor salience
	// bid (RFC 0030 Tier B, v0.3.8). The bid only gates open-floor speakers —
	// `participant`/`chair`/legacy `always`, all normalizing to RespondAlways.
	// A threshold on an `addressed`/`observer` (or the default
	// `when_mentioned`) member is a silent no-op, so the loader rejects it
	// loudly rather than let an operator believe a bar is in force where no
	// bid ever runs. This is a cross-field invariant the JSON schema cannot
	// express, so unlike the range check it has no `make validate` mirror.
	ErrThresholdNotApplicable = errors.New("channels: threshold not applicable to disposition")
	// ErrChannelCapExceeded — CreateChannel would push named-group count past
	// the configured `max_channels` cap. DMs and threads are not counted.
	ErrChannelCapExceeded = errors.New("channels: max_channels exceeded")
	// ErrMessageContentTooLarge — PublishMessage rejected because
	// `len(msg.Content) > MaxMessageContentBytes` (ISSUE-0050). The REST
	// layer surfaces this as 413 Payload Too Large.
	ErrMessageContentTooLarge = errors.New("channels: message content exceeds size cap")
	// ErrInvalidMaxCascadeDepth — `channels.yaml` carried a negative
	// `max_cascade_depth:`. PR #319 deep review finding 5.2: belt-and-
	// suspenders for the operator who skipped `make validate` (the JSON
	// schema's `minimum: 0` rejects this earlier). Zero is NOT an error
	// — it is the loader's documented "use the default" sentinel
	// honored by [ChannelRouter.SetMaxCascadeDepth].
	ErrInvalidMaxCascadeDepth = errors.New("channels: invalid max_cascade_depth")
	// ErrInvalidFloorTurnTimeout — a declared channel carried a negative
	// `floor_turn_timeout_seconds:` (RFC 0030 amendment). Belt-and-
	// suspenders for the operator who skipped `make validate` (the JSON
	// schema's `minimum: 1` rejects this earlier). Zero is NOT an error —
	// it is the loader's "use the default" sentinel normalized to
	// [DefaultFloorTurnTimeoutSeconds] at load time.
	ErrInvalidFloorTurnTimeout = errors.New("channels: invalid floor_turn_timeout_seconds")
	// ErrInvalidSalienceMaxChannelMembers — a declared channel carried a negative
	// `salience_max_channel_members:` (RFC 0030 Tier B, v0.3.8). Belt-and-
	// suspenders for the operator who skipped `make validate` (the JSON
	// schema's `minimum: 1` rejects this earlier). Zero is NOT an error — it
	// is the loader's "use the default" sentinel normalized to
	// [DefaultSalienceMaxChannelMembers] at load time.
	ErrInvalidSalienceMaxChannelMembers = errors.New("channels: invalid salience_max_channel_members")
	// ErrInvalidInteractionBudgetTokens — a declared channel (or the
	// top-level `default_interaction_budget_tokens`) carried a negative
	// `interaction_budget_tokens:` (RFC 0030 Layer 1 cost ceiling, v0.3.8).
	// Belt-and-suspenders for the operator who skipped `make validate` (the
	// JSON schema's `minimum: 0` rejects this earlier). Zero is NOT an error
	// — it is the opt-in default meaning "uncapped", so the ceiling is
	// additive and existing channels are unaffected.
	ErrInvalidInteractionBudgetTokens = errors.New("channels: invalid interaction_budget_tokens")
	// ErrInvalidInteractionIdleTimeout — a declared channel (or the top-level
	// `default_interaction_idle_timeout_seconds`) carried a negative
	// `interaction_idle_timeout_seconds:` (the RFC 0030 interaction-id
	// producer's idle window, IP3). Zero is NOT an error — it is the explicit
	// "idle rotation off" value, distinct from absent (inherit the default).
	ErrInvalidInteractionIdleTimeout = errors.New("channels: invalid interaction_idle_timeout_seconds")
	// ErrInvalidEscalationChair — a declared channel's `escalation_chair_id`
	// (the chair-stall-escalation amendment, CE2) names someone who is not
	// one of the channel's declared members, or names an `observer` (legacy
	// `never`) member. The forced turn dispatches to a member's envelope; a
	// non-member chair could never receive it, and an observer's gate
	// suppresses every turn before any LLM, so it could never speak — both
	// misconfigurations fail loudly at load rather than as permanent
	// per-stall futility (`dispatch_error`, or `dispatched` with no turn
	// ever possible).
	ErrInvalidEscalationChair = errors.New("channels: invalid escalation_chair_id")
	// ErrInvalidMaxRepliesPerParticipant — a declared channel (or the
	// top-level `default_max_replies_per_participant`) carried a negative
	// `max_replies_per_participant_per_interaction:` (RFC 0030 Layer 2 reply
	// budget, v0.3.8). Belt-and-suspenders for the operator who skipped
	// `make validate` (the JSON schema's `minimum: 0` rejects this earlier).
	// Zero is NOT an error — it is the opt-in default meaning "uncapped".
	ErrInvalidMaxRepliesPerParticipant = errors.New("channels: invalid max_replies_per_participant_per_interaction")
	// ErrParticipantBudgetExhausted — the publish was rejected by the RFC 0030
	// Layer 2 reply budget (§F, v0.3.8): the sender already published its
	// allotted `max_replies_per_participant_per_interaction` replies in this
	// interaction. Surfaced pre-persistence so the dropped message never enters
	// channel history; the REST publish handler maps it to HTTP 429.
	ErrParticipantBudgetExhausted = errors.New("channels: participant reply budget exhausted for this interaction")
	// ErrInvalidEndVoteThreshold — a declared channel carried a negative
	// `end_vote_threshold:` (RFC 0030 Layer 4 end-of-interaction signal, §H,
	// v0.3.8). Belt-and-suspenders for the operator who skipped `make validate`
	// (the JSON schema's `minimum: 1` rejects this earlier). Zero is NOT an
	// error — it is the loader's "use the default" sentinel normalized to
	// [DefaultEndVoteThreshold] at load time (K=2).
	ErrInvalidEndVoteThreshold = errors.New("channels: invalid end_vote_threshold")
	// ErrInvalidEndVoteWindow — a declared channel carried a negative
	// `end_vote_window:` (RFC 0030 Layer 4 end-of-interaction signal, §H,
	// v0.3.8). Belt-and-suspenders for the operator who skipped `make validate`
	// (the JSON schema's `minimum: 1` rejects this earlier). Zero is NOT an
	// error — it is the loader's "use the default" sentinel normalized to
	// [DefaultEndVoteWindow] at load time (W=3).
	ErrInvalidEndVoteWindow = errors.New("channels: invalid end_vote_window")
)

// participantIDPattern is the single source of truth for legal participant
// ids across all three RFC 0011 validation surfaces:
//
//   - schemas/channel.schema.json (config-time validation via `make validate`)
//   - LoadConfig→Validate (loader-time, calls ValidateParticipantID below)
//   - the runtime store guards (PublishMessage, CanonicalDMID, AddMember)
//
// Keeping the schema, loader, and runtime in lock-step closes the
// PR-#231-review gap where the three layers had drifted apart. The pattern
// matches schemas/channel.schema.json `definitions.member` and accepts both
// agent ids (e.g. `code-writer`) and human/CLI participants (e.g. `User_1`)
// while excluding the `:` reserved by the canonical-address grammar and any
// whitespace.
var participantIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]*$`)

// channelNamePattern mirrors `schemas/channel.schema.json` →
// `definitions.channel.properties.name.pattern`. Compiled here so the loader
// (`Config.Validate`) and any future runtime call can enforce the same
// predicate the JSON Schema does at `make validate`. Closes Should-Fix #6
// of PR #231 review: previously a `config/channels.yaml` with `name:
// "Planning"` (or `name: "x"`) parsed cleanly through `LoadConfig` and only
// failed at `make validate`. The pattern is intentionally stricter than the
// participant-id pattern: channel names are user-visible canonical-address
// segments (`group:<name>`), so we lock them to the same lowercase-kebab
// shape used for agent ids in `schemas/agent.schema.json`.
var channelNamePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]*[a-z0-9]$`)

// ValidateParticipantID enforces the runtime half of the §A constraint set.
// The other half lives at config-load and registration boundaries; both now
// share `participantIDPattern` so a value that passes one passes all three.
// Exported for the REST wire boundary (the create handler's member
// translation, resolveMemberRequests), which judges member identity
// alongside the respond policy before the store transaction opens — so a
// malformed body 400s ahead of any state conflict. The store write paths
// keep their own calls for the non-REST callers.
func ValidateParticipantID(id string) error {
	if id == "" {
		return fmt.Errorf("%w: empty", ErrInvalidParticipantID)
	}
	if !participantIDPattern.MatchString(id) {
		return fmt.Errorf("%w: %q does not match %s",
			ErrInvalidParticipantID, id, participantIDPattern.String())
	}
	return nil
}

// CanonicalDMID returns the canonical address for a DM between a and b.
//
// The pair is lexicographically sorted before joining with `:` so
// `CanonicalDMID("agent-b", "agent-a")` and `CanonicalDMID("agent-a",
// "agent-b")` both produce `dm:agent-a:agent-b`. Callers should never
// build DM ids by hand — the store's `GetOrCreateDM` is the single source
// of truth and routes through here.
func CanonicalDMID(a, b string) (string, error) {
	if err := ValidateParticipantID(a); err != nil {
		return "", err
	}
	if err := ValidateParticipantID(b); err != nil {
		return "", err
	}
	if a == b {
		return "", fmt.Errorf("%w: dm requires two distinct participants (got %q twice)", ErrInvalidParticipantID, a)
	}
	if a > b {
		a, b = b, a
	}
	return "dm:" + a + ":" + b, nil
}
