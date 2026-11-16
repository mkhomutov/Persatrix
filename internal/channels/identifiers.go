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

// participantIDPattern is the one rule for a participant id (ISSUE-0209):
// lowercase letters, digits and hyphens, at least two characters, and a
// letter or digit first and last. It is the rule an agent id has always had
// (`schemas/agent.schema.json`), and the one the agents' own check
// (`agents/channel_validation.py`, `agents/participant.py`), the accounts
// package and the CLI hold. A drift test keeps the copies equal
// (`tests/unit/python/test_cross_language_participant_id_drift.py`).
//
// Every write goes through it ([ValidateParticipantID]): a message's sender
// and mentions, a new member, a new channel's members, a new DM.
//
// Until ISSUE-0209 this package held a looser rule
// ([legacyParticipantIDPattern]), so that a person could keep an id such as
// `User_1`. The orchestrator then stored a message from such a member and
// sent it to every persona, and each persona refused it, because the agents
// never held the looser rule. The id is now refused where it enters.
var participantIDPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]*[a-z0-9]$`)

// participantIDRule is [participantIDPattern] in words, for the message a
// caller is shown when an id is refused.
const participantIDRule = "lowercase letters, digits and hyphens, at least two characters, starting and ending with a letter or digit"

// legacyParticipantIDPattern is the rule this package held until ISSUE-0209.
// Nothing is written under it any more. It is kept for one purpose: a store
// written by an earlier orchestrator can hold members and DMs with such an
// id, and those must stay readable. So the paths that only read what is
// stored accept it ([validateStoredParticipantID]): the config loader, whose
// channel may already be in the store, and the DM address, which a history
// read derives.
var legacyParticipantIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]*$`)

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

// ValidateParticipantID refuses an id that does not fit the participant id
// rule. It guards every write: [ChannelStore.PublishMessage] (the sender and
// each mention), [ChannelStore.AddMember], the create paths, and the create
// half of [ChannelStore.GetOrCreateDM]. The error names the rule, and the
// REST layer shows it to the caller with a 400.
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
		return fmt.Errorf("%w: %q does not match %s (%s)",
			ErrInvalidParticipantID, id, participantIDPattern.String(), participantIDRule)
	}
	return nil
}

// IsLegacyParticipantID reports whether id fits only the rule held before
// ISSUE-0209: an id an earlier orchestrator stored and this one would refuse.
func IsLegacyParticipantID(id string) bool {
	return !participantIDPattern.MatchString(id) && legacyParticipantIDPattern.MatchString(id)
}

// validateStoredParticipantID is the check for a path that reads what may
// already be stored: it accepts an id that fits the rule, and one that fits
// only the earlier rule ([legacyParticipantIDPattern]). It never guards a
// write.
func validateStoredParticipantID(id string) error {
	if IsLegacyParticipantID(id) {
		return nil
	}
	return ValidateParticipantID(id)
}

// CanonicalDMID returns the canonical address for a DM between a and b.
//
// The pair is lexicographically sorted before joining with `:` so
// `CanonicalDMID("agent-b", "agent-a")` and `CanonicalDMID("agent-a",
// "agent-b")` both produce `dm:agent-a:agent-b`. Callers should never
// build DM ids by hand — the store's `GetOrCreateDM` is the single source
// of truth and routes through here.
//
// It only names an address, so it accepts an id of the earlier, looser shape
// too ([validateStoredParticipantID]): the DM an earlier orchestrator stored
// for `User_1` must still be found, to read its history. Creating a DM is a
// write, and [ChannelStore.GetOrCreateDM] holds the pair to the rule itself.
func CanonicalDMID(a, b string) (string, error) {
	if err := validateStoredParticipantID(a); err != nil {
		return "", err
	}
	if err := validateStoredParticipantID(b); err != nil {
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
