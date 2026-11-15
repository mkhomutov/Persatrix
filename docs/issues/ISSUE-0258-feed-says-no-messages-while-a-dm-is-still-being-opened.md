---
id: ISSUE-0258
summary: "While the web console is still looking a direct message up, its timeline already shows `No messages yet. Say hello — the persona replies here.` under the `Opening conversation…` line. For a conversation that has messages the text is wrong until the lookup answers."
status: open
severity: low
area: web
created: 2026-11-15
refs:
  - https://github.com/mkhomutov/Persatrix/pull/1129
  - web/src/panels/ConversationFeed.svelte
  - web/src/panels/ChannelTimeline.svelte
  - web/src/lib/dmSession.svelte.js
  - docs/issues/ISSUE-0226-identity-change-leaves-the-old-dm-open.md
---

# ISSUE-0258: The feed says "No messages yet" while a DM is still being opened

## Summary

A DM's channel id is looked up when the DM is opened
(`DmSession.resolve`). Until the answer arrives the conversation feed is
given an empty channel id, which it treats as a settled empty
conversation: it shows the empty state, "No messages yet." with the hint
"Say hello — the persona replies here." The header shows
"Opening conversation…" at the same time.

## Context

- Seen in a browser against a stand-in API. On `main` at `dd2cceb1`, for
  a lookup that failed: the header said "Could not open the
  conversation: …" and the feed said "No messages yet." with its hint.
  With the fix for
  [ISSUE-0226](ISSUE-0226-identity-change-leaves-the-old-dm-open.md), for
  a lookup still in flight: both texts were on screen at once.
- On `main` the in-flight case follows from the same code whenever a DM
  is opened. It was not caught on screen there: the stand-in answers too
  fast. The lookup is one request, so a slow backend is what makes the
  text readable.
- The ISSUE-0226 fix adds a second way to reach the state: changing
  "acting as" with a DM open looks the DM up again.
- The feed cannot tell "no channel yet" from "not known yet": both
  arrive as an empty channel id.
- **The same moment keeps a line of the old identity on screen.** When
  a reply has just arrived, the line above the message box says
  "Waiting for you" for four seconds. Changing "acting as" inside those
  seconds leaves it there under the new identity, over a conversation
  in which nobody is waiting. `DmSession.reidentify` clears that line
  only while a send is still out, and the feed starts it afresh when
  the persona changes (its key is `dm:<persona>`), which is the same
  for both identities. Seen as a test against a stand-in: shown after
  the reply, still shown after the change, gone four seconds later.

## Impact

For a moment the console tells a person that a conversation with history
is empty, and invites a first message.

## Proposed fix / investigation path

Tell the feed that its conversation is still being opened (a prop beside
the channel id), and show the loading placeholder it already has for a
history load. After a failed lookup, show neither the empty state nor
the hint.

For the "Waiting for you" line: let the feed start its presence afresh
when the identity changes too, by putting the user id into the key it
already keeps for the conversation. That is one line in
`ConversationFeed.svelte`.

## Notes

> 2026-11-15 — found while checking the fix for ISSUE-0226 in a browser.
> Not fixed there: the text is in `ConversationFeed.svelte`, which the
> pull request for ISSUE-0222 changes at the same time.

> 2026-11-15 — the review of the fix for ISSUE-0226 added the "Waiting
> for you" line. It is the same moment and the same file, so it is
> recorded here and not fixed in that pull request either.
