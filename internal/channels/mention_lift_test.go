package channels

// mention_lift_test.go — RFC 0011 display-name-mention-lifting amendment
// (docs/rfcs/0011-amendment-display-name-mention-lifting.md), ML1–ML3. Pins
// the pure resolver [LiftDisplayNameMentions]: the substrate PR 2 lands with
// no call site (PR 3 wires it into the publish handler). The matrix is the
// amendment §C-1 list — multi-word longest-match, case folding, id-vs-name
// precedence, ambiguity → nobody, boundary anchoring (emails, mid-word `@`,
// leading boundary), sender exclusion, dedupe + first-seen order, the
// `@everyone` sentinel, and the empty shapes.

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

// liftRoster is the three-member roster the live MT-CHANNEL-GOV-004 hand-off
// ran against — ids are kebab-case, display names are the `**Iron Fox:**`
// window spellings the chair actually typed.
func liftRoster() []MentionCandidate {
	return []MentionCandidate{
		{ID: "nova-sparrow", DisplayName: "Nova Sparrow"},
		{ID: "ember-owl", DisplayName: "Ember Owl"},
		{ID: "iron-fox", DisplayName: "Iron Fox"},
	}
}

// liftIDs is the ids-only view of the resolver — the half the matrix below
// pins. The ambiguous-name diagnostics ([LiftedMentions.AmbiguousNames], the
// ML5 WARN feed) get their own dedicated assertion in the collision test.
func liftIDs(content string, candidates []MentionCandidate, senderID string) []string {
	return LiftDisplayNameMentions(content, candidates, senderID).IDs
}

// TestLiftDisplayNameMentions_TheLiveHandoff pins the failure ISSUE-0096
// captured verbatim: the chair's prose hand-off names two members by their
// window-rendered display names, and both resolve to their canonical ids in
// first-seen content order. The inbound human ("alex") is named in prose but
// not `@`-prefixed, so it is not lifted (it rides the structured array PR 3
// unions in).
func TestLiftDisplayNameMentions_TheLiveHandoff(t *testing.T) {
	got := liftIDs(
		"@Ember Owl @Iron Fox — alex needs one risk each from all of us on the relay plan.",
		liftRoster(), "nova-sparrow")

	assert.Equal(t, []string{"ember-owl", "iron-fox"}, got,
		"both display-name mentions resolve to canonical ids, in content order; the un-@'d human name is left alone")
}

// TestLiftDisplayNameMentions_MultiWordLongestMatch pins the greedy
// longest-match: when a one-word name is a prefix of a two-word name, the
// full two-word name wins; the bare one-word form still resolves on its own.
func TestLiftDisplayNameMentions_MultiWordLongestMatch(t *testing.T) {
	roster := []MentionCandidate{
		{ID: "iron", DisplayName: "Iron"},
		{ID: "iron-fox", DisplayName: "Iron Fox"},
	}

	assert.Equal(t, []string{"iron-fox"},
		liftIDs("@Iron Fox, take this one.", roster, "sender"),
		"the longest matching name wins — @Iron Fox is iron-fox, not iron")
	assert.Equal(t, []string{"iron"},
		liftIDs("@Iron, you first.", roster, "sender"),
		"the bare one-word name still resolves when nothing longer follows")
}

// TestLiftDisplayNameMentions_CaseInsensitiveName pins ML3's case folding on
// the display-name side: the spelling in content need not match the registry
// casing.
func TestLiftDisplayNameMentions_CaseInsensitiveName(t *testing.T) {
	assert.Equal(t, []string{"ember-owl"},
		liftIDs("hey @ember owl can you take this", liftRoster(), "x"),
		"display-name matching folds case")
	assert.Equal(t, []string{"ember-owl"},
		liftIDs("@EMBER OWL ping", liftRoster(), "x"),
		"upper-case spelling resolves the same member")
}

// TestLiftDisplayNameMentions_IdMatchAndPrecedence pins ML3's "exact id match
// first": a kebab id resolves directly (the web-composer semantics), and the
// id and display-name forms of the same member resolve to the one id.
func TestLiftDisplayNameMentions_IdMatchAndPrecedence(t *testing.T) {
	assert.Equal(t, []string{"iron-fox"},
		liftIDs("@iron-fox you're up", liftRoster(), "x"),
		"an exact participant id resolves directly")
	assert.Equal(t, []string{"iron-fox"},
		liftIDs("@iron-fox and @Iron Fox", liftRoster(), "x"),
		"the id form and the display-name form of one member collapse to a single id")
}

// TestLiftDisplayNameMentions_IdMatchShadowsLongerName pins the documented
// "exact id first" precedence at its sharp edge: when a short valid id is also
// the first word of a *longer* display name, the id wins and the trailing
// words are left as prose — the resolver never falls through to the longer
// display-name match once an id has matched. This locks the spec choice (id
// before longest-name), so a future "prefer the longest overall span" refactor
// trips here first instead of silently changing who the floor is directed at.
func TestLiftDisplayNameMentions_IdMatchShadowsLongerName(t *testing.T) {
	roster := []MentionCandidate{
		{ID: "ok", DisplayName: "Ok"},
		{ID: "team-lead", DisplayName: "Ok Then"},
	}

	assert.Equal(t, []string{"ok"},
		liftIDs("@ok then, ship it", roster, "x"),
		"an exact id match wins over a longer display-name match — id-first precedence, trailing words stay prose")
}

// TestLiftDisplayNameMentions_AmbiguousLiftsNobody pins ML3's collision rule:
// two members whose folded display names collide make that name unresolvable
// (misdirecting the floor is worse than the silence it replaces) — the
// colliding token lifts neither — while an unambiguous token in the same
// content still lifts.
func TestLiftDisplayNameMentions_AmbiguousLiftsNobody(t *testing.T) {
	roster := []MentionCandidate{
		{ID: "river-heron", DisplayName: "River Heron"},
		{ID: "river-finch", DisplayName: "River Heron"}, // the collision
		{ID: "iron-fox", DisplayName: "Iron Fox"},
	}

	assert.Equal(t, []string{"iron-fox"},
		liftIDs("@River Heron and @Iron Fox — split the review.", roster, "x"),
		"the ambiguous display name lifts nobody; the unambiguous token still lifts")
	assert.Nil(t,
		liftIDs("@River Heron, you take it.", roster, "x"),
		"a content whose only mention is the ambiguous name lifts no one")

	// ML5: the skipped collision is reported (folded, content-ordered, deduped)
	// so the publish handler can WARN — but only when the ambiguous name was
	// actually mentioned, so a standing roster collision never spams a channel
	// whose traffic never names it.
	res := LiftDisplayNameMentions("@River Heron and @Iron Fox — and @River Heron again.", roster, "x")
	assert.Equal(t, []string{"iron-fox"}, res.IDs,
		"the unambiguous token still lifts")
	assert.Equal(t, []string{"river heron"}, res.AmbiguousNames,
		"the colliding name is reported once, folded, for the ML5 WARN")
}

// TestLiftDisplayNameMentions_NoAmbiguityWhenRosterIsClean pins the silent
// case: a clean roster reports no ambiguous names, so the handler stays quiet.
func TestLiftDisplayNameMentions_NoAmbiguityWhenRosterIsClean(t *testing.T) {
	res := LiftDisplayNameMentions("@Ember Owl @Iron Fox go", liftRoster(), "nova-sparrow")
	assert.Equal(t, []string{"ember-owl", "iron-fox"}, res.IDs)
	assert.Nil(t, res.AmbiguousNames,
		"no collision in the roster means nothing to warn about")
}

// TestLiftDisplayNameMentions_BoundaryAnchoring pins ML3's "TOKEN_RE posture":
// an `@` directs a mention only at a boundary (start-of-content or after
// whitespace), so an email's `@` and a mid-word `@` never resolve.
func TestLiftDisplayNameMentions_BoundaryAnchoring(t *testing.T) {
	assert.Nil(t,
		liftIDs("mail me at local@iron-fox.example", liftRoster(), "x"),
		"an email's `@` is not a mention boundary")
	assert.Nil(t,
		liftIDs("foo@iron-fox", liftRoster(), "x"),
		"a mid-word `@` is not a mention boundary")
	assert.Nil(t,
		liftIDs("(@iron-fox)", liftRoster(), "x"),
		"a non-whitespace lead (the TOKEN_RE `(^|\\s)@` posture) is not a boundary")
	assert.Equal(t, []string{"iron-fox"},
		liftIDs("@iron-fox at start", liftRoster(), "x"),
		"a start-of-content `@` is a boundary")
}

// TestLiftDisplayNameMentions_TrailingBoundary pins the end-of-name boundary:
// a name matches only when it ends on a non-id-char, so "@Iron Foxes" does NOT
// resolve "Iron Fox" — the match cannot run into the middle of a longer word.
func TestLiftDisplayNameMentions_TrailingBoundary(t *testing.T) {
	assert.Nil(t,
		liftIDs("@Iron Foxes are clever", liftRoster(), "x"),
		"a name is not matched into the middle of a longer word")
	assert.Equal(t, []string{"iron-fox"},
		liftIDs("@Iron Fox! over to you", liftRoster(), "x"),
		"trailing punctuation is a boundary — the name still resolves")
}

// TestLiftDisplayNameMentions_ExcludesSender pins ML3's sender exclusion: a
// self-mention by id or display name cannot direct the floor (the
// resolveFloorMentions posture), so the sender's own token lifts nothing
// while the co-addressee still resolves.
func TestLiftDisplayNameMentions_ExcludesSender(t *testing.T) {
	assert.Equal(t, []string{"ember-owl"},
		liftIDs("@Iron Fox @Ember Owl — your turn", liftRoster(), "iron-fox"),
		"the sender's own display name is not lifted; the co-addressee is")
	assert.Nil(t,
		liftIDs("@iron-fox thinking out loud", liftRoster(), "iron-fox"),
		"a sender self-mention by id lifts nothing")
}

// TestLiftDisplayNameMentions_DedupeAndOrder pins ML1's result shape: lifted
// ids are de-duplicated to the first occurrence and preserve first-seen
// content order, regardless of how a member is spelled across repeats.
func TestLiftDisplayNameMentions_DedupeAndOrder(t *testing.T) {
	got := liftIDs(
		"@Iron Fox @Ember Owl then @iron-fox again", liftRoster(), "x")

	assert.Equal(t, []string{"iron-fox", "ember-owl"}, got,
		"first-seen order, de-duplicated — the repeated id form collapses into the first occurrence")
}

// TestLiftDisplayNameMentions_EveryoneSentinelNotLifted pins ML3's carve-out:
// `@everyone` is a raw-array broadcast sentinel, never a member, so the
// resolver leaves it alone (no member is named "everyone").
func TestLiftDisplayNameMentions_EveryoneSentinelNotLifted(t *testing.T) {
	assert.Nil(t,
		liftIDs("@everyone please weigh in", liftRoster(), "x"),
		"the @everyone sentinel resolves to no member — it stays on the raw mentions path")
}

// TestLiftDisplayNameMentions_EmptyShapes pins the trivial returns: empty
// content, no roster, and content with no `@` token all lift nothing.
func TestLiftDisplayNameMentions_EmptyShapes(t *testing.T) {
	assert.Nil(t, liftIDs("", liftRoster(), "x"),
		"empty content lifts nothing")
	assert.Nil(t, liftIDs("@Iron Fox", nil, "x"),
		"an empty roster lifts nothing")
	assert.Nil(t, liftIDs("just a plain sentence, no addressing", liftRoster(), "x"),
		"content with no `@` lifts nothing")
}

// TestLiftDisplayNameMentions_RegistrylessMemberStillIdMatches pins ML3/OQ3: a
// member with no registry display name ("" — the human's row) has no
// display-name form, but its id still resolves when `@`-mentioned directly.
func TestLiftDisplayNameMentions_RegistrylessMemberStillIdMatches(t *testing.T) {
	roster := []MentionCandidate{
		{ID: "alex", DisplayName: ""}, // the human — no registry row
		{ID: "iron-fox", DisplayName: "Iron Fox"},
	}

	assert.Equal(t, []string{"alex"},
		liftIDs("@alex what's the call", roster, "x"),
		"a display-name-less member still id-matches")
	assert.Nil(t,
		liftIDs("alex what's the call", roster, "x"),
		"its empty display name matches nothing (no spurious bare-name lift)")
}
