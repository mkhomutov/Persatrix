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
type RespondPolicy string

const (
	RespondWhenMentioned RespondPolicy = "when_mentioned"
	RespondAlways        RespondPolicy = "always"
	RespondNever         RespondPolicy = "never"
)

// Valid reports whether p is one of the canonical respond policies.
func (p RespondPolicy) Valid() bool {
	switch p {
	case RespondWhenMentioned, RespondAlways, RespondNever:
		return true
	}
	return false
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
