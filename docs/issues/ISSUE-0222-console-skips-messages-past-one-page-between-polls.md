---
id: ISSUE-0222
summary: "The web console's conversation feed fetches only the newest 50 messages on each poll and stops polling while its tab is hidden. If more than 50 messages arrive in between, the older ones are never fetched, no gap is shown, and `Show older messages` cannot fill the hole."
status: resolved
severity: medium
area: web
created: 2026-11-10
closed: 2026-11-15
refs:
  - web/src/panels/ConversationFeed.svelte
  - web/src/lib/backfill.svelte.js
  - web/src/panels/TimelineGap.svelte
  - web/src/panels/ChannelTimeline.catchup.test.js
  - docs/issues/ISSUE-0257-console-controls-drop-keyboard-focus-while-busy.md
  - docs/rfcs/0048-operator-tester-web-console.md
---

# ISSUE-0222: The console skips messages when more than one page arrives between two polls

## Summary

`ConversationFeed.svelte` polls the channel's history every three
seconds with `limit: 50`, keeps the rows it has not seen, and puts them
at the head of its list. It pauses polling while `document.hidden` is
true and polls once when the tab is shown again.

That one poll returns the newest 50 messages. If an autonomous channel
wrote 80 while the tab was hidden, the 30 oldest of those are never
requested.

## Context

- Nothing marks the gap in the timeline.
- `loadOlder` pages backwards from the oldest message the feed holds, so
  it starts below the hole and cannot reach it.
- Only a page reload, or selecting another channel and coming back,
  loads the missing rows.
- No test covers a head poll in which every row is new.

The same loss can happen without hiding the tab if a channel produces
more than 50 messages inside one poll interval or during a backoff of up
to 30 seconds, though that is much less likely.

## Impact

An operator who leaves a standing autonomous channel in a background tab
comes back to a transcript with a silent hole in it. The console is the
main way to watch such a channel.

## Proposed fix / investigation path

When every row of a head page is new, keep paging backwards until a row
already seen comes back. If that is too much to load at once, show a gap
marker that loads the missing span when clicked. Add a test with a full
page of fresh rows.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; confirmed by reading the component, not in a browser.

> 2026-11-15 — fixed, following the proposed fix. Reproduced first on
> `main` at `dd2cceb1`, as a test and in a browser against a stand-in
> API: with three messages shown and 120 more written while the tab was
> hidden, the console showed 53 of 123 on return, with no sign that 70
> were missing. The Summary and Context hold as written. One more case
> was found: a conversation that was empty when opened and then filled
> past 50 messages showed the newest 50 under "Start of the
> conversation", with no way to reach the rest.
>
> - **Catch-up.** A full head page with no message the feed has seen
>   now makes the feed read back from it, 200 messages a request, until
>   a message it already shows comes back or a short page shows that the
>   channel's first message was reached.
> - **The bound.** One poll reads at most three such pages, 597
>   messages. What is still missing waits behind a **Load missing
>   messages** marker in the timeline; a click reads up to three more
>   pages and keeps the reader's place. Later polls do not read a marked
>   hole again on their own.
> - **Cost.** A return to a hidden tab costs at most three history
>   requests on top of the poll's own, once. A page that fails ends the
>   catch-up, keeps what was read, leaves the marker and backs the poll
>   off as a failed head read does.
> - **Nothing older on screen.** There is nothing to read back to, so
>   the feed offers **Show older messages** and makes no extra request.
> - **Where the code is.** The paging, with the older-messages page
>   that was already there, moved to `web/src/lib/backfill.svelte.js`.
>
> Not changed: a run of 200 or more messages that share one timestamp
> cannot be read past (50 or more for **Show older messages**), because
> the history endpoint pages by timestamp alone. The marker then says
> that the server returned only messages already shown. The store stamps
> each message when it is published, so such a run is not expected.
> Found on the way and filed:
> [ISSUE-0257](ISSUE-0257-console-controls-drop-keyboard-focus-while-busy.md).
