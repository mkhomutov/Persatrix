---
id: ISSUE-0257
summary: "The web console disables a control while the request it started is in flight, and the browser then takes keyboard focus away from it. After every posted message the message box is no longer focused, and after every page of `Show older messages` neither is that button, so a keyboard user has to find the control again each time."
status: open
severity: low
area: web
created: 2026-11-15
refs:
  - web/src/panels/PublishComposer.svelte
  - web/src/panels/DmComposer.svelte
  - web/src/panels/ConversationFeed.svelte
  - web/src/panels/TimelineGap.svelte
  - docs/issues/ISSUE-0222-console-skips-messages-past-one-page-between-polls.md
---

# ISSUE-0257: The console's controls drop keyboard focus while they are busy

## Summary

Several console controls set the HTML `disabled` attribute on themselves
while their request is in flight, so that a second press does nothing. A
browser that follows the HTML rule for a focused element that stops being
focusable moves focus to the page when that happens, and does not give
it back when the control is enabled again.

Seen on `main` at `dd2cceb1`, in a Chromium-based browser against a
stand-in API:

1. **The message box of a group channel.** Type a message and press
   **Enter**. The message is posted and the box is emptied, but it is no
   longer focused (`document.activeElement` is the page body). The next
   message cannot be typed until the box is clicked.
2. **Show older messages.** Focus the button and press **Enter**. The
   older page loads and the button is still there, offering the next
   page, but focus is on the page body.

## Context

- `PublishComposer.svelte` disables its text box while `publishing`, and
  `DmComposer.svelte` disables its box while `sending`.
- `ConversationFeed.svelte` disables **Show older messages** while a
  page loads.
- The **Load missing messages** marker added for
  [ISSUE-0222](ISSUE-0222-console-skips-messages-past-one-page-between-polls.md)
  follows the same pattern, and was seen to lose focus the same way. Its
  button also moves with the marker when a hole is only partly loaded.
- The same attribute is used on the buttons of the channel settings, the
  member list and the session selector. Those were not tried.
- Only the two cases above and the marker were tried, in one browser.
  The DM message box was read, not tried. Browsers differ here: some
  keep focus on a disabled control.

## Impact

An operator who posts several messages in a row has to click the message
box before each one. A keyboard-only operator has to tab back to it from
the top of the page. Paging far back through a long channel by keyboard
means finding the button again after every page.

## Proposed fix / investigation path

Keep the control focusable while it is busy: mark it `aria-disabled` and
ignore a second activation in the handler, or leave a text box enabled
and ignore **Enter** while the request is in flight. Where a button
moves or goes away (the marker, the last older page), hand focus to what
replaces it. Check each control in at least two browsers, and add a test
that the message box is still the focused element after a post.

## Notes

> 2026-11-15 — found while checking the fix for ISSUE-0222 in a browser:
> the marker's button lost focus on a failed load although it had not
> moved. The message box and **Show older messages** were then tried on
> `main` and behave the same.
