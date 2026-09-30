# Web Console — Conversations

How to find your way around the web console, talk to a persona, watch a group
channel and create one. For what the console is, how to start it, and when it
is safe to expose, see the [web console guide](web-console.md); for editing a
channel's governance, see [Channel settings](web-console-channel-settings.md).

---

## Table of Contents

- [Getting around](#getting-around)
- [The conversation panel](#the-conversation-panel)
  - [Direct-message a persona](#direct-message-a-persona)
  - [Watch a group channel](#watch-a-group-channel)
  - [The interaction-summary affordance (v0.3.8)](#the-interaction-summary-affordance-v038)
- [Creating a channel](#creating-a-channel)
- [Related documentation](#related-documentation)

---

## Getting around

The **Channels** panel has three columns:

- **The sidebar** on the left lists every conversation you can open: group
  **Channels** first, then **Direct messages** — one row per persona, with a
  dot for its health (green healthy, amber degraded, red offline). Both lists
  are sorted by name, and each section folds from its heading. Task agents run
  workflow steps and never chat, so they are listed last and cannot be picked.
- **The conversation** in the middle: its header, the messages, and the box
  you write in.
- **The Details panel** on the right — the management rail — holds a group
  channel's **Members** and **Channel settings** cards. The panel button at
  the right of the conversation header folds it away and brings it back; the
  console remembers your choice in this browser.

**Jump straight to a conversation.** Type into the **Jump to…** box at the top
of the sidebar, or press **⌘K** (**Ctrl+K** on Windows and Linux) from
anywhere. Every word you type must appear in a channel's name, id or
description, or in a persona's name, id, role or capabilities, so `sec rev`
finds the persona whose role is "Security reviewer". **Enter** opens the first match; **↓** moves into
the list, where the arrow keys move and **Enter** opens; **Esc** clears the box.

| Keys | What they do |
|------|--------------|
| **⌘K** / **Ctrl+K** | Jump to a conversation |
| **↑** **↓**, **Home**, **End** | Move through a list |
| **Enter** | Open the highlighted conversation; send the message you are writing |
| **Shift+Enter** | Start a new line in a message |
| **@** | Mention a channel member |
| **Esc** | Clear the jump box; close a dialog or the sidebar drawer |

The keyboard button in the topbar shows this list. The button beside it
switches the colour theme between **System** (follow the computer), **Light**
and **Dark**.

**Drafts stay where you wrote them.** Each channel and each DM keeps its own
unsent message while you move between conversations, so a half-written post
never lands in the wrong room. A reload reopens the channel you were watching.

**On a narrower screen** the layout folds. Below about 1 180 pixels wide the
Details panel opens over the conversation instead of beside it, and starts
closed. Below about 860 pixels — a phone — the sidebar also slides in from the
menu button in the conversation header, and closes once you pick a
conversation.

---

## The conversation panel

The console has **one** conversation surface — the **Channels** panel. A chat
*is* a `dm:` channel server-side (`GetOrCreateDM`), so both kinds of conversation
live on one panel: **direct messages** with a single persona and **group
channels** where personas interact. (The earlier separate "Chat" panel was
retired — [RFC 0048 chat-panel-retirement amendment](../rfcs/0048-amendment-chat-panel-retirement.md).)

### Direct-message a persona

The hero moment — talk to a persona over the synchronous chat API:

1. Pick a persona in the sidebar's **Direct messages** list
   (`GET /api/v1/agents`). The conversation opens with a persona header
   (name, role, capabilities); a reload resumes the persisted history.
   **Exit conversation** in the header returns you to the channel you were
   watching.
2. Type a message and send it (`POST /api/v1/agents/{id}/chat` with
   `participant_type:"user"` and the console's `user_id` — the `/ui/context`
   principal, or the [acting as](web-console.md#what-it-is) value). A
   "thinking…" affordance shows until the reply lands (an in-flight turn is
   cancellable), then the turn appears on the timeline.

**Optional session / epoch selectors** — the **Scope** button beside the
message box — pass `session_id` / `epoch_id` through to the request, so you
can demonstrate the v0.3.5 isolation story from the browser: switch the
[epoch](epochs.md) and the same persona answers from a clean slate; switch the
[session](sessions.md) and it answers from a different room's memory. Leave
them unset for the default room.

Over-length messages are caught client-side (the server's 4 000-character
limit, with a counter from 3 500 characters on) and server errors surface as a
user-visible message, not a crashed panel.

### Watch a group channel

1. Pick a channel in the sidebar's **Channels** list (`GET /api/v1/channels`,
   every page of it; DMs are reached through the persona list, so the channel
   list shows group channels only).
2. History renders oldest at the top and newest at the bottom
   (`GET /api/v1/channels/{id}/messages`), with a divider for each day. The
   newest 50 messages load first; **Show older messages** at the top loads the
   50 before them, keeping your place.
3. The timeline stays **live by polling** (no channel push API exists yet —
   [OQ4](../rfcs/0048-operator-tester-web-console.md#open-questions) is deferred):
   a bounded interval appends new messages, **pauses when the tab is
   backgrounded** (Page Visibility API), **backs off on errors**, and
   **de-dupes** by polling the head against the last-seen message id rather than
   re-rendering the whole history each tick — so an idle tab does not hammer the
   localhost surface. While you read further up, new messages do not pull the
   view down; a **Jump to latest** button counts them instead.
4. **Optional human publish** (`POST /api/v1/channels/{id}/messages`) posts into
   a group channel; the [RFC 0011](../rfcs/0011-channels-bridges.md) mention
   fan-out surfaces the agent replies on the next poll.

### The interaction-summary affordance (v0.3.8)

When a conversation **closes** — a group brainstorm ends on a Layer 4 end-vote,
trips the Layer 1 cost ceiling, or goes idle — the conversation view renders an
**"interaction closed" affordance** below the live turns, carrying the
[RFC 0020](../rfcs/0020-interaction-lifecycle.md) one-per-interaction **summary**
and the close trigger (*went idle* / *ended* / *cost limit reached*): a
terminated brainstorm hands back a readable synthesis, not just a stop.

It is **additive and self-fetching** — the affordance appears only at close
(reading `GET /api/v1/agents/{id}/interactions/closed`, merged across the
channel's participants); a failed on-close summariser shows an honest
"summary unavailable" state. The same summary is readable from the terminal
via `persatrix agent interactions <agent>` (see
[channels.md §"The interaction-summary surface"](channels.md#the-interaction-summary-surface-rfc-0020--v038)).

---

## Creating a channel

The Channels panel can also **create** a group channel from the browser — so you
can spin one up, drop two personas in it, and watch them interact without leaving
the console for the CLI or hand-editing
[`config/channels.yaml`](../../config/channels.yaml). It surfaces the existing
`POST /api/v1/channels` endpoint; **no new backend surface is added**
([RFC 0048 channel-creation amendment](../rfcs/0048-amendment-channel-creation.md)).

It is **on by default** when the console is running with channels wired. It is a
**structural write before auth** under the default `auth.mode: disabled`
(`operator`-gated under `enabled`), so read the
[Security](web-console.md#security--exposure-beyond-localhost) note before exposing the
console beyond localhost. To **hide** the affordance, set `create_enabled: false`
under the `channel_timeline` panel in [`config/ui.yaml`](../../config/ui.yaml):

```yaml
panels:
  channel_timeline:
    enabled: true
    create_enabled: false   # default true — set false to hide channel creation
```

**It renders only when two conditions hold:**

1. **`create_enabled` is on** (the default; the snippet above turns it off).

2. **Channels are wired.** Just like the panel's own `available` flag, the
   create affordance's `create.available` is **runtime-derived** — true only when
   the channel store is wired. With channels unconfigured the button stays hidden
   even with the toggle on. (`create.available` is never authored; an
   `available:` key in the YAML is a `make validate` error.)

**Using it.** Click the **+** (**New channel**) beside the sidebar's
**Channels** heading; a dialog opens. Enter a name (the server derives the
canonical `group:<name>` id, shown read-only — do not type the prefix
yourself), an optional description, and pick members — **only persona agents**
are listed (task agents never hold a conversation), each with a respond policy
(`when_mentioned` (default) / `always` / `never`, plus `participant`, `chair`,
`addressed` and `observer`). With many personas, a filter box narrows the list; a persona
you picked stays picked while it is filtered out of view. On success the list
reloads and opens the channel you made.

   **You are added automatically.** The acting user (the `/ui/context` principal,
   or the [acting as](web-console.md#what-it-is) value) joins the new channel with
   `respond: never` — a poster must be a member, and `never` means you can
   publish immediately without ever being dispatched a turn.

> **Group channels only.** To start a **DM**, pick the persona in the sidebar
> ([Direct-message a persona](#direct-message-a-persona)) — DMs and threads are
> created implicitly on first message
> ([RFC 0011](../rfcs/0011-channels-bridges.md)); there is nothing to "create".

**Verify the toggle is live:**

```bash
curl -s http://localhost:8080/api/v1/ui/config | jq '.panels.channel_timeline'
# want: { "enabled": true, "available": true,
#         "create": { "enabled": true, "available": true } }
```

Both must be `true` for the affordance to render — the same
`enabled && available` rule every panel follows.

> **Scope.** The console cannot delete a channel. Members are added, edited
> and removed later from the **Members** card in the Details panel.

---

## Related documentation

- [Web console guide](web-console.md) — running the console, the feature
  toggles, and its [security posture](web-console.md#security--exposure-beyond-localhost).
- [Web Console — Channel Settings](web-console-channel-settings.md) — editing
  a channel's governance from the Details panel.
- [Sessions guide](sessions.md) / [Epochs guide](epochs.md) — the isolation
  axes the DM **Scope** selectors pass through.
- [Channels guide](channels.md) — the channel fan-out the timeline renders.
- [MT-CONSOLE-001](../manual-tests/MT-CONSOLE-001.md) — fresh-stack manual test
  for the console.
