// identifiers.go — the channel/participant identifier grammar: the shared
// id patterns, participant-id validation, the canonical DM address, and the
// channel-id prefix → type derivation. Verbatim moves from channels.go and
// router.go when RFC 0037 PR 2's classification additions pushed both past
// the 500-line review cap (the ISSUE-0008 extraction pattern); the grammar
// is one cohesive surface — every rule here mirrors
// schemas/channel.schema.json.
package channels

import (
	"fmt"
	"regexp"
	"strings"
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

// channelTypeFromID derives the canonical channel type from a channel id's
// prefix. Returns [ErrInvalidChannelType] if the prefix is unknown.
func channelTypeFromID(id string) (ChannelType, error) {
	switch {
	case strings.HasPrefix(id, "group:"):
		return ChannelTypeGroup, nil
	case strings.HasPrefix(id, "dm:"):
		return ChannelTypeDM, nil
	case strings.HasPrefix(id, "thread:"):
		return ChannelTypeThread, nil
	default:
		return "", fmt.Errorf("%w: unknown channel_id prefix in %q", ErrInvalidChannelType, id)
	}
}
