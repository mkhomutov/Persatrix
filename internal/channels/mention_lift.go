package channels

import "strings"

// mention_lift.go — RFC 0011 display-name-mention-lifting amendment, ML1–ML3
// (docs/rfcs/0011-amendment-display-name-mention-lifting.md). The pure
// resolver that turns the prose `@`-mentions personas actually write
// ("@Iron Fox") into the canonical participant ids the wire already carries
// ("iron-fox"). PR 2 lands the substrate with NO call site; PR 3 wires it into
// the REST publish handler ([Server.handlePublishMessage]), unioning the
// result into the structured `mentions` array before persist and fanout
// (ML1) — at which point [resolveFloorMentions], both response gates, history,
// and web highlighting start seeing the ids the prose always meant, with no
// proto/wire/schema change.
//
// Kept in its own file (sibling of floor_mentions.go) so floor_control.go and
// the publish handler stay under the 500-line review cap.

// mentionCandidate is one channel member as the lift sees it: its canonical
// participant id and its registry display name (empty when the member has no
// registry row — the human's `respond: never` seat, ML3/OQ 3). PR 3 builds the
// slice by joining the channel's members with the agent registry directory
// (`AgentInfo.Name`); this file never touches the registry itself, keeping the
// resolver pure and unit-testable.
type mentionCandidate struct {
	ID          string
	DisplayName string
}

// liftDisplayNameMentions resolves the in-text `@`-mentions in `content` to
// canonical member ids, returning them in first-seen content order, deduped to
// the first occurrence, with the sender excluded (a self-mention cannot direct
// the floor — the [resolveFloorMentions] posture). Resolution is
// membership-scoped and deterministic:
//
//   - An `@` directs a mention only at a *boundary* — start-of-content or
//     immediately after whitespace (the web composer's `TOKEN_RE` `(^|\s)@`
//     anchoring), so an email's `@` and a mid-word `@` never resolve.
//   - At a boundary, an **exact participant id** is matched first (the id
//     namespace is canonical — the composer's `valid.has(id)` semantics,
//     case-sensitive). Failing that, the **display name** is matched
//     case-insensitively with greedy **longest-match** across its words, and
//     only when it ends on a word boundary (so "@Iron Foxes" does not resolve
//     "Iron Fox").
//   - A folded display name shared by two members is **ambiguous** and lifts
//     nobody — misdirecting the floor is worse than the silence it replaces
//     (ML3). The colliding span is consumed, not re-scanned.
//
// `@everyone` falls out for free (no member is named "everyone"), so the
// broadcast sentinel stays on the raw-mentions path. The fold is ASCII-case;
// participant display names are ASCII by construction (kebab ids, latinate
// persona names).
func liftDisplayNameMentions(content string, candidates []mentionCandidate, senderID string) []string {
	if content == "" || len(candidates) == 0 {
		return nil
	}

	// Index the non-sender candidates once: the exact-id set, and the folded
	// display name → distinct ids map (a name mapping to >1 id is the ambiguous
	// config smell). The sender is filtered out entirely, so neither its id nor
	// its name can match and it cannot be a party to a collision.
	ids := make(map[string]struct{}, len(candidates))
	folded := make(map[string][]string, len(candidates))
	for _, c := range candidates {
		if c.ID == "" || c.ID == senderID {
			continue
		}
		ids[c.ID] = struct{}{}
		name := strings.ToLower(strings.Join(strings.Fields(c.DisplayName), " "))
		if name == "" {
			continue
		}
		existing := folded[name]
		seen := false
		for _, id := range existing {
			if id == c.ID {
				seen = true
				break
			}
		}
		if !seen {
			folded[name] = append(existing, c.ID)
		}
	}

	var out []string
	emitted := make(map[string]struct{})
	n := len(content)
	for i := 0; i < n; {
		if content[i] != '@' || (i > 0 && !isMentionSpace(content[i-1])) {
			i++
			continue
		}
		start := i + 1
		resolved := ""
		end := start

		// Exact-id match first (canonical namespace).
		j := start
		for j < n && isMentionIDChar(content[j]) {
			j++
		}
		if j > start {
			if _, ok := ids[content[start:j]]; ok {
				resolved, end = content[start:j], j
			}
		}

		// Display-name match: greedy longest. Distinct folded names cannot tie
		// on the same end position (equal matched spans imply the same folded
		// string), so `> bestEnd` alone is order-independent and deterministic.
		if resolved == "" {
			bestEnd := -1
			var bestIDs []string
			for name, nameIDs := range folded {
				if e := matchDisplayNameAt(content, start, name); e > bestEnd {
					bestEnd, bestIDs = e, nameIDs
				}
			}
			if bestEnd >= 0 {
				end = bestEnd
				if len(bestIDs) == 1 { // unambiguous; a collision lifts nobody
					resolved = bestIDs[0]
				}
			}
		}

		if resolved != "" {
			if _, dup := emitted[resolved]; !dup {
				emitted[resolved] = struct{}{}
				out = append(out, resolved)
			}
		}
		if end > i {
			i = end
		} else {
			i++
		}
	}
	return out
}

// matchDisplayNameAt attempts to match the already-folded `name` against
// `content` starting at `start`, case-insensitively. Each space in the name
// matches a run of one-or-more whitespace characters; every other character
// matches case-folded. On success it returns the exclusive end index, but only
// when the match ends on a word boundary (the next character is not an id
// char) — so a name is never matched into the middle of a longer word. Returns
// -1 on any mismatch.
func matchDisplayNameAt(content string, start int, name string) int {
	ti, n := start, len(content)
	for ni := 0; ni < len(name); ni++ {
		if name[ni] == ' ' {
			if ti >= n || !isMentionSpace(content[ti]) {
				return -1
			}
			for ti < n && isMentionSpace(content[ti]) {
				ti++
			}
			continue
		}
		if ti >= n || asciiLower(content[ti]) != name[ni] {
			return -1
		}
		ti++
	}
	if ti < n && isMentionIDChar(content[ti]) {
		return -1
	}
	return ti
}

// isMentionIDChar reports whether b is a participant-id body character —
// `participantIDPattern`'s `[A-Za-z0-9_-]`. Doubles as the word-boundary test
// for the end of a display-name match.
func isMentionIDChar(b byte) bool {
	return b >= 'A' && b <= 'Z' ||
		b >= 'a' && b <= 'z' ||
		b >= '0' && b <= '9' ||
		b == '-' || b == '_'
}

// isMentionSpace reports whether b is ASCII whitespace — the `\s` half of the
// composer's `(^|\s)@` boundary anchor and the inter-word gap in a display
// name.
func isMentionSpace(b byte) bool {
	return b == ' ' || b == '\t' || b == '\n' || b == '\r' || b == '\f' || b == '\v'
}

// asciiLower folds an ASCII upper-case byte; other bytes pass through.
func asciiLower(b byte) byte {
	if b >= 'A' && b <= 'Z' {
		return b + ('a' - 'A')
	}
	return b
}
