---
id: ISSUE-0260
summary: "The web console draws a message the operator posts at the newest end of the timeline as soon as the server stores it. A message another member wrote just before that post, which the console reads only on its next poll, is then drawn below the post although it was written first, and stays there until the conversation is loaded again."
status: open
severity: low
area: web
created: 2026-11-15
refs:
  - https://github.com/mkhomutov/Persatrix/pull/1128
  - web/src/panels/ConversationFeed.svelte
  - web/src/lib/backfill.svelte.js
  - docs/issues/ISSUE-0222-console-skips-messages-past-one-page-between-polls.md
---

# ISSUE-0260: The console draws the operator's post above a message written before it

## Summary

When the operator posts into a group channel, `ConversationFeed.svelte`
puts the stored message at the newest end of its list at once, without
reading the channel (its `echo`). Each poll then puts the messages it
has not seen at the newest end too, whatever their timestamps. The feed
never sorts its rows.

So a message that another member wrote before the post, and that the
console had not read yet, ends up below the post. The timeline is drawn
oldest first, which makes that message look as if it came after the
post, like an answer to it.

Seen on `main` at `dd2cceb1` and on the branch of the pull request in
`refs`, as a test against a stand-in for the history endpoint:

1. Three messages are shown.
2. Another member's message is stored at 11:59:01.
3. Before the next poll the operator posts; the post is stored at
   11:59:02.
4. After the next poll the timeline shows the post and then the other
   member's message. The store has them the other way round. A minute
   later the order is the same.

## Context

- The window is the time since the console's last read: up to three
  seconds between polls, and up to 30 seconds while the poll is backed
  off after a failed read.
- The same happens when a poll was asked before the post was stored
  and answers after it: every message on that page was written before
  the post and is drawn below it.
- One case is in order since the fix for
  [ISSUE-0222](ISSUE-0222-console-skips-messages-past-one-page-between-polls.md):
  when a whole page of unread messages arrives, the console takes the
  post off the screen and the reads bring it back in its place. Any
  smaller page leaves the order as described here.
- A row shows the time it was written, beside the sender or in the
  margin, so the order on screen contradicts the times next to it.
- Not tried in a browser by the author of this file, and not tried in
  a DM: a DM draws the operator's message from a read, not at once.

## Impact

An operator reading a busy channel sees an exchange in an order that
did not happen. Nothing is missing, and loading the conversation again
(a page reload, or opening another channel and coming back) shows the
store's order. Personas are not affected: they read the store, not the
console.

## Proposed fix / investigation path

When a read returns a post that no read had returned before, put the
post where that read has it, as the catch-up already does for a whole
page of unread messages. Or keep the list ordered by timestamp when
rows are added; that needs a rule for messages that share a timestamp
(see
[ISSUE-0262](ISSUE-0262-console-paging-cannot-pass-messages-that-share-a-timestamp.md)).
Add a test with the case above.

## Notes

> 2026-11-15 — found in the review of the fix for ISSUE-0222, and
> confirmed there on `main`. Severity `low` is proposed, not ruled:
> nothing is lost and a reload corrects the order. ISSUE-0222, where
> messages went missing, was `medium`.

> 2026-11-28 — severity ruled `low`, as proposed: the maintainer's
> ruling at the
> [strategy review of 2026-11-28](../project-strategy-review-2026-11-28.md#3-the-maintainers-rulings),
> ratified by ruling (ab) of the
> [sequencing Amendment 2026-11-28](../v0.4.x-sequencing-2026-11-28.md#rulings).
