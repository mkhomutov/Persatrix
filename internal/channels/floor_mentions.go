package channels

// floor_mentions.go — RFC 0030 floor-capable-directedness amendment
// (docs/rfcs/0030-amendment-floor-capable-directedness.md). A mention only
// directs the floor if the named party could actually take it: the Tier A
// directed-elsewhere decision (the receiver gate's suppression of un-named
// `always`/`participant` members, mirrored by [orderResponders]'s candidate
// split) must not fire for a message whose mentions name only parties that
// can never reply — the human operator (a `respond: never` member by the
// documented join convention), an `observer`, or a non-member. Suppression
// exists to yield the floor to the addressee; an addressee that cannot take
// the floor makes suppression a guaranteed-silence rule.
//
// Kept in its own file (sibling of reply_budget.go / router_metrics.go) to
// keep floor_control.go under the 500-line review cap.

// resolveFloorMentions returns the subset of `mentions` naming
// *floor-capable* members: current channel members whose normalized respond
// policy ([RespondPolicy.Normalize]) is not [RespondNever], excluding the
// sender (amendment §C item 1 — a self-mention cannot direct the floor: the
// sender never replies to itself, so counting it would suppress everyone
// else for an addressee that cannot take the turn; the exclusion keeps the
// subset sender-relative but still recipient-independent, so per-publish
// stamping is unaffected). Mention order is preserved and duplicates collapse
// to the first occurrence — the subset is a contract-bearing wire field that
// receivers reason about as a set, and the publish path caps `mentions` at 10
// without deduping. The [MentionEveryone] sentinel is never a member id
// ([ValidateParticipantID] forbids `@`) so it never appears in the result —
// broadcast handling stays on the raw mentions list.
//
// Normalize matches every other policy read at this seam — [orderResponders]'
// candidate loop, [ChannelRouter.dispatchConcurrent]'s `never` short-circuit,
// and the Python gate's `_DISPOSITION_ALIASES` — identity for store-canonical
// rows (the membership CHECK constraint admits only the legacy triple), but a
// non-canonical spelling that ever does reach a [Member] must classify the
// same way everywhere, because this subset becomes the cross-language wire
// suppression basis (`ChannelMessageEvent.floor_mentions`).
//
// Called twice per publish — by [orderResponders] for the candidate split
// (the §C item 3 basis flip, paired with the Python gate's) and by
// [ChannelRouter.fanout] for the envelope stamp. Both calls see the same
// (members, mentions) pair, so the two results agree by construction; the
// duplicate O(N+M) walk over two small lists is cheaper than threading the
// slice through the [orderResponders] signature and every existing call site.
func resolveFloorMentions(members []Member, mentions []string, senderID string) []string {
	if len(mentions) == 0 {
		return nil
	}
	capable := make(map[string]bool, len(members))
	for _, m := range members {
		if m.ParticipantID != senderID && m.RespondPolicy.Normalize() != RespondNever {
			capable[m.ParticipantID] = true
		}
	}
	var out []string
	for _, id := range mentions {
		if capable[id] {
			delete(capable, id) // dedupe: first occurrence wins
			out = append(out, id)
		}
	}
	return out
}
