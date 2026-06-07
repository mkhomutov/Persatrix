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
	// downstream reader is unchanged); its "chair-ness" survives only as the
	// low [MemberConfig.Threshold] applied at config load
	// ([DefaultChairThreshold]). A v0.3.8 `chair` CANNOT close an interaction
	// — convergence is owned by the deterministic governance layers — and its
	// Layer 5 moderator hooks are reserved/inert until v0.4.0.
	RespondChair RespondPolicy = "chair"
)

// MentionEveryone is the broadcast sentinel for the RFC 0030 relevance
// amendment Tier A directed-elsewhere filter (v0.3.7, decision D3). Its
// presence in a message's `Mentions` list marks the message as addressed
// to the whole room, disabling the directed-elsewhere suppression so every
// `always`/`participant` member stays a candidate responder. The value
// carries an `@`, which [validateParticipantID] forbids, so it can never
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
	// (RFC 0030 Tier B, v0.3.8) fell outside the `[0, 1]` range. The JSON
	// schema's `minimum`/`maximum` catches this at `make validate`; this
	// Go-side check is the belt-and-suspenders for operators who skipped that
	// step. An absent threshold is NOT an error — it is the unset (bias-to-
	// silence) default.
	ErrInvalidThreshold = errors.New("channels: invalid member threshold")
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
)

// participantIDPattern is the single source of truth for legal participant
// ids across all three RFC 0011 validation surfaces:
//
//   - schemas/channel.schema.json (config-time validation via `make validate`)
//   - LoadConfig→Validate (loader-time, calls validateParticipantID below)
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

// validateParticipantID enforces the runtime half of the §A constraint set.
// The other half lives at config-load and registration boundaries; both now
// share `participantIDPattern` so a value that passes one passes all three.
func validateParticipantID(id string) error {
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
	if err := validateParticipantID(a); err != nil {
		return "", err
	}
	if err := validateParticipantID(b); err != nil {
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
