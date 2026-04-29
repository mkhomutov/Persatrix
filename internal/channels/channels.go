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
	"strings"
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
type Channel struct {
	ID          string      // canonical address: "group:planning" / "dm:a:b" / "thread:<msg-id>"
	Name        string      // group: declared name; dm/thread: empty
	Type        ChannelType // group | dm | thread
	Description string
	CreatedAt   time.Time
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
type ChannelMessage struct {
	ID        string
	ChannelID string
	SenderID  string
	Content   string
	Timestamp time.Time
	ThreadID  string // empty when the message is not a reply
	Mentions  []string
	Metadata  map[string]any
}

// Errors surfaced by [ChannelStore]. Callers should compare with [errors.Is].
var (
	// ErrChannelNotFound — GetChannel/PublishMessage against a missing id.
	ErrChannelNotFound = errors.New("channels: channel not found")
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
)

// validateParticipantID enforces the runtime half of the §A constraint set.
// The other half lives at config-load and registration boundaries.
func validateParticipantID(id string) error {
	if id == "" {
		return fmt.Errorf("%w: empty", ErrInvalidParticipantID)
	}
	if strings.ContainsAny(id, ": \t\r\n") {
		return fmt.Errorf("%w: %q contains forbidden character", ErrInvalidParticipantID, id)
	}
	for _, r := range id {
		if r > 0x7E || r < 0x21 { // printable ASCII only
			return fmt.Errorf("%w: %q is not printable ASCII", ErrInvalidParticipantID, id)
		}
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
