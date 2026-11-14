---
id: ISSUE-0226
summary: "In the web console's local-testing mode, changing the `acting as` identity while a DM is open leaves the previous identity's conversation on screen. The next message is sent as the new identity into a different DM channel, and its reply never appears in the feed."
status: resolved
severity: low
area: web
created: 2026-11-10
closed: 2026-11-15
closed_pr: 1129
refs:
  - web/src/panels/ChannelTimeline.identity.test.js
  - docs/issues/ISSUE-0258-feed-says-no-messages-while-a-dm-is-still-being-opened.md
  - web/src/lib/dmSession.svelte.js
  - web/src/panels/ChannelTimeline.svelte
  - web/src/App.svelte
  - web/src/App.identity.test.js
---

# ISSUE-0226: Changing "acting as" leaves the old identity's DM open

## Summary

`DmSession` (`web/src/lib/dmSession.svelte.js`) resolves the DM channel
id once, in `resolve`, using the user id at that moment. It resolves
again only after a send that found no channel yet. Nothing re-resolves
when the user id changes.

Sending always uses the current user id. So after a change the two
disagree: the feed keeps polling the old identity's channel while
messages go to the new identity's.

## Context

Steps, with auth off:

1. Open a DM with a persona as `local` and exchange a message.
2. Change "acting as" to `bob`.
3. Send a message.

The POST goes as `bob`, into the DM channel of `bob` and that persona.
The feed still shows and polls the DM channel of `local` and that
persona. The draft clears and no reply shows.

A comment in `App.svelte` says the panels reseed history when the
identity changes, and `App.identity.test.js` says the same. The panel
that stays mounted does not.

This affects the local-testing override only. Once a person is signed
in, the override control is hidden.

## Impact

The identity override exists to demonstrate that two people get separate
conversations and memory with one persona. With a DM open, the
demonstration shows the opposite: one person's history under another's
name, and a message that seems to get no answer.

## Proposed fix / investigation path

Re-resolve the open DM in an effect that depends on the user id, and
clear the feed while it resolves. Add a test that changes the identity
with a DM open.

## Notes

> 2026-11-10 — found by a whole-repository quality review of `main` at
> `ee9cfaac`; confirmed by reading the session code, not in a browser.

> 2026-11-15 — fixed, following the proposed fix. Reproduced first on
> `main` at `dd2cceb1`, as tests and in a browser against a stand-in
> API, with the three steps above: acting as `bob`, the screen still
> showed `local`'s two messages, the POST went into `bob`'s DM, the
> console kept reading `local`'s, and no reply showed. The Summary and
> Context hold as written.
>
> - **The open DM** is looked up again when the user id changes
>   (`DmSession.reidentify`, called from an effect in the panel). Its
>   channel id is empty until the lookup answers, so the timeline is
>   empty in between; a lookup answered for the old identity is dropped.
> - **An unsent draft stays in the message box.** It is what the person
>   at the keyboard typed, whoever they act as, and the console shows
>   who that is before they send. Group-channel drafts already behaved
>   so.
> - **A reply still on its way** for the old identity is left to
>   arrive: its request is not cancelled, so that identity's
>   conversation has the reply when the tester changes back. On screen
>   it is cut loose at once: no "thinking" line, a free message box,
>   and nothing changes when the answer, or an error, comes in. The box
>   still holds that message's text, as it does after **Cancel**.
> - **A group channel** is not reloaded. It is the same channel for
>   everyone; only which messages read "You" follows the identity.
> - **Signed in**, the control does not exist and the user id never
>   changes, so nothing here runs.
> - The comment in `App.svelte` and the statement in
>   `App.identity.test.js` now say what happens: an open DM is opened
>   again, a group channel is not reloaded.
>
> Found on the way and filed:
> [ISSUE-0258](ISSUE-0258-feed-says-no-messages-while-a-dm-is-still-being-opened.md).
