---
id: ISSUE-0263
summary: "In the web console's local-testing mode, changing `acting as` while a DM reply is on its way and changing back before it arrives does not show that turn as in flight again: no `thinking` line, no Cancel, a live Send, and the sent text still in the message box. A Send in that window is refused by the orchestrator (409), and the box keeps the sent text after the reply shows."
status: open
severity: low
area: web
created: 2026-11-15
refs:
  - https://github.com/mkhomutov/Persatrix/pull/1129
  - web/src/lib/dmSession.svelte.js
  - web/src/panels/ChannelTimeline.identity.test.js
  - internal/server/chat_handler.go
  - docs/issues/ISSUE-0226-identity-change-leaves-the-old-dm-open.md
---

# ISSUE-0263: Changing back to an identity does not pick its turn up again

## Summary

Since the fix for
[ISSUE-0226](ISSUE-0226-identity-change-leaves-the-old-dm-open.md), a
message whose reply is still on its way is cut loose from the screen
when the tester changes who the console acts as. The request is left to
finish, so the reply lands in the first identity's conversation.

If the tester changes back to that identity before the reply arrives,
the console does not take the turn up again. `DmSession` matches a turn
by a count of identity changes, not by who sent it, and changing back
is one more change.

Seen as a test against a stand-in for the backend, on the branch of the
pull request in `refs`. Acting as `local`, send a message and hold the
answer; change to `bob`; change back to `local`:

1. The timeline shows `local`'s earlier messages. There is no
   "… is thinking" line and no **Cancel**, **Send** is live, and the
   message box holds the text that was sent.
2. When the answer arrives, nothing on screen changes.
3. Up to three seconds later the next poll shows the message and the
   reply. The box still holds the sent text.

## Context

- The orchestrator allows one chat in flight for a DM. A second one is
  answered `409` with "another chat is in flight for this DM; retry
  shortly" (`internal/server/chat_handler.go`). So a **Send** during
  step 1 or 2 fails with that text. This was read from the handler,
  not provoked.
- After step 3 one click on **Send**, with nothing typed, sends the
  same text a second time as the same identity.
- The window is as long as the persona takes to answer: seconds, or
  minutes for a slow model.
- Only under `auth.mode: disabled`. A signed-in person has no
  **acting as** box.
- The description of the pull request listed its choices for a turn in
  flight, and this case was not among them.

## Impact

A tester who switches away and straight back sees a console that looks
idle while a turn is still out. They can get an error for a message
they did not know was in the way, or send the same message twice.
Nothing is lost: the first turn and its reply are stored and shown.

## Proposed fix / investigation path

Remember who sent a turn that is still out (the identity and the
persona), not only how many changes ago it was sent. When the console
acts again as that identity with that DM open, show the turn as in
flight again: the "thinking" line, **Cancel**, a busy **Send**. Let its
answer finish as an ordinary turn does, which empties the box. Add a
test for changing away and back inside one turn.

## Notes

> 2026-11-15 — found in the review of the fix for ISSUE-0226. Severity
> `low` is proposed, not ruled: it needs two identity changes inside
> one turn, in a mode meant for local testing, and loses nothing.
> ISSUE-0226 itself was `low`.

> 2026-11-28 — severity ruled `low`, as proposed: the maintainer's
> ruling at the
> [strategy review of 2026-11-28](../project-strategy-review-2026-11-28.md#3-the-maintainers-rulings),
> ratified by ruling (ab) of the
> [sequencing Amendment 2026-11-28](../v0.4.x-sequencing-2026-11-28.md#rulings).
