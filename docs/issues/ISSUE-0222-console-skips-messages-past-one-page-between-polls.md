---
id: ISSUE-0222
summary: "The web console's conversation feed fetches only the newest 50 messages on each poll and stops polling while its tab is hidden. If more than 50 messages arrive in between, the older ones are never fetched, no gap is shown, and `Show older messages` cannot fill the hole."
status: open
severity: medium
area: web
created: 2026-11-10
refs:
  - web/src/panels/ConversationFeed.svelte
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
