# Manual Test MT-CONSOLE-002: Web Console — `@`-Mention Compose & Fan-Out

**Test ID**: `MT-CONSOLE-002`
**Feature Area**: Web Console (RFC 0048) — `@`-mentions over the channel composer (RFC 0011)
**Version**: 1.0
**Created**: 2026-06-04
**Last Updated**: 2026-06-04
**Status**: Active

---

## Overview

**Purpose**: Verify that the console composer can **originate** an `@`-mention end
to end in a real browser: typing `@` opens a typeahead of the channel's members,
selecting one lifts `@<id>` into the draft, posting drives the RFC 0011 fan-out
(a `when_mentioned` member that would otherwise stay silent is pulled into the
round), and the stored message renders the mention **highlighted** in the
timeline. Before this feature the console could only *display* a fan-out
originated from the CLI; the composer posted `{sender_id, content}` with no
`mentions`.

**Scope**: The live browser-interaction legs that the Vitest unit suite stands in
for but cannot fully exercise — the typeahead renders/filters as you type, the
keyboard + pointer selection moves a real caret and inserts the token, the
operator's own id is excluded, the publish carries a resolved `mentions` array,
the mention drives a real persona fan-out, and the timeline highlights it. Plus
the negative path: an unknown `@token` stays plain text and is **not** sent.

**Out of Scope**: The mention wire contract + cap (server-side; covered by the Go
suite), the `when_mentioned`/`always` response-policy semantics themselves
([MT-CHANNEL-GOV-002](MT-CHANNEL-GOV-002.md)), and floor-control ordering (RFC
0030). DM mode has no mentions (a DM is a two-party channel).

---

## Related Documentation

**Feature Documentation**:
- [docs/rfcs/0048-operator-tester-web-console.md](../rfcs/0048-operator-tester-web-console.md) — console spec
- [docs/rfcs/0011-channels-bridges.md](../rfcs/0011-channels-bridges.md) — mentions + fan-out gate
- [web/src/lib/mentions.js](../../web/src/lib/mentions.js) — parse/resolve/highlight helpers
- [web/src/panels/PublishComposer.svelte](../../web/src/panels/PublishComposer.svelte) — the `@`-typeahead
- [web/src/panels/ChannelMessage.svelte](../../web/src/panels/ChannelMessage.svelte) — mention highlight
- [web/src/panels/ChannelTimeline.svelte](../../web/src/panels/ChannelTimeline.svelte) — members → composer + publish wiring
- [internal/server/channel_handlers.go](../../internal/server/channel_handlers.go) — publish endpoint + `mentions` cap

**Related Automated Tests** (these carry the *logic*; this MT carries the live render/fan-out):
- JS: `web/src/lib/mentions.test.js`, `web/src/panels/PublishComposer.test.js`, `web/src/panels/ChannelMessage.test.js`, `web/src/panels/ChannelTimeline.mentions.test.js`, `web/src/lib/api.publish.test.js`
- Go: `internal/server/channel_handlers_test.go` (`TestChannels_PublishMessage_MentionsCountCap`), `internal/server/channel_publish_fanout_integration_test.go` (`TestChannelPublish_FullChain_RESTToGRPCFanout`)

---

## Preconditions

### System Requirements

**Operating Systems**:
- ☐ Windows 10/11 (x64)
- ☐ macOS 12.0+ (Intel/Apple Silicon)
- ☐ Linux (Ubuntu 22.04+)

**Dependencies Installed**:
- Node 22+ for the local `make ui` build path (the Docker path builds in-image)
- A browser with dev tools (to inspect the publish request body)
- `ANTHROPIC_API_KEY` set for a live fan-out, **or** `make demo-offline` (the mock
  provider still fans out to the mentioned member; the reply text is canned)

### Application State

- ☐ Console built and served with `--enable-ui` ([MT-CONSOLE-001](MT-CONSOLE-001.md) Steps 1–4 green)
- ☐ A **group** channel with at least two persona members, **one of them
  `respond: when_mentioned`** — the shipped `group:planning` works: `ember-owl`
  (`when_mentioned`), `iron-fox` + `nova-sparrow` (`always`)
- ☐ The operator is a **member** of the channel — the composer joins implicitly;
  via CLI run `./bin/persatrix channel join planning --as operator` (a non-member
  `send` returns `403`, per [MT-CHANNEL-GOV-002](MT-CHANNEL-GOV-002.md))

### Test Data

No external fixtures. Uses the shipped `group:planning` channel and its personas.

---

## Test Procedure

### Step 1: Typeahead opens on `@` and filters as you type

**Action**: Open `http://localhost:8080/ui`, select the `group:planning` channel,
and in the publish composer type `@`. Then type `emb`.

**Expected Result**: An `@` opens a member menu listing the channel's members
(decorated `@id — Name — Role`). Typing narrows it; `@emb` leaves only `ember-owl`.

**Verification**:
- [ ] Typing `@` opens a listbox of the channel's members.
- [ ] The list is the channel's **members**, not the full persona roster.
- [ ] Typing a partial id/name filters the list (`@emb` → just `ember-owl`).
- [ ] The operator's **own** id never appears as a candidate.

---

### Step 2: Keyboard navigation selects and inserts the token

**Action**: With the menu open from `@`, press `ArrowDown` to move the highlight,
then `Enter` (or `Tab`) on a member.

**Expected Result**: The highlighted member is inserted as `@<id> ` (trailing
space), the menu closes, and the caret lands after the inserted token. The
`Enter` that selected the member did **not** post the draft.

**Verification**:
- [ ] `ArrowDown`/`ArrowUp` move the highlighted candidate (wrapping at the ends).
- [ ] `Enter`/`Tab` insert `@<id> ` and close the menu — the draft is **not** posted.
- [ ] `Escape` closes the menu and leaves the draft text untouched.
- [ ] Clicking a candidate row inserts it the same way (focus is retained).

---

### Step 3: Publish a mention → the `when_mentioned` member is pulled in

**Action**: Compose a question that mentions the `when_mentioned` advisor, e.g.
`Postgres or SQLite for the v0.4 event log? @ember-owl your read?`, and post it.
(Open the browser network tab to inspect the request.)

**Expected Result**: The `POST /api/v1/channels/group:planning/messages` body
carries `"mentions": ["ember-owl"]`. On a later poll, `ember-owl` — which stays
silent on an un-mentioned prompt — appears in the round with a reply.

**Verification**:
- [ ] The publish request body contains `mentions: ["ember-owl"]` (and the
      `/ui/context`-derived `sender_id`, never a free-text sender).
- [ ] `ember-owl` replies in the timeline on a subsequent poll (no manual refresh).
- [ ] **Contrast**: a follow-up post with **no** `@ember-owl` mention shows
      `ember-owl` staying silent (only the `always` members reply).

---

### Step 4: The mention renders highlighted in the timeline

**Action**: Observe your just-posted message in the timeline.

**Expected Result**: The `@ember-owl` token renders visually highlighted (distinct
weight/colour) while the surrounding prose is plain.

**Verification**:
- [ ] The `@ember-owl` token in the stored message is highlighted.
- [ ] The rest of the message text is unaffected.

---

### Step 5: Unknown `@token` stays plain text and is not sent

**Action**: Post a message mentioning a non-member, e.g. `ping @nobody and the team`.

**Expected Result**: `@nobody` is posted as ordinary text; the request body carries
**no** `mentions` key (or omits `nobody`); no spurious fan-out occurs.

**Verification**:
- [ ] The publish body for an all-unknown mention has **no** `mentions` key.
- [ ] `@nobody` is **not** highlighted in the timeline (it resolved to no member).
- [ ] No persona is pulled in by the unknown token.

---

### Step 6: Plain post keeps the pre-feature shape (regression)

**Action**: Post a message with no `@` at all (e.g. `status update`).

**Expected Result**: The request body is exactly `{ sender_id, content }` — no
`mentions` key — identical to the pre-feature wire.

**Verification**:
- [ ] A mention-free publish sends no `mentions` key.

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | `@` opens a members-only typeahead that filters; self excluded | ☐ |
| 2 | Arrow/Enter/Tab/click select inserts `@id `; menu-open Enter does not post | ☐ |
| 3 | Publish carries `mentions`; the `when_mentioned` member is pulled into the round | ☐ |
| 4 | The resolved mention renders highlighted in the timeline | ☐ |
| 5 | An unknown `@token` stays plain text and is not sent | ☐ |
| 6 | A plain post keeps the `{sender_id, content}` shape (no `mentions` key) | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: `@` inside an email / mid-word

**Scenario**: Type `mail me at local@ember-owl.io`.

**Expected**: No menu opens and nothing is lifted — the `@` is not at a word
boundary, so it never reads as a mention.

### Edge Case 2: Caret leaves / re-enters a token

**Scenario**: With a half-typed `@emb`, arrow the caret out of the token, then back in.

**Expected**: The menu closes when the caret leaves the token and re-opens when it
re-enters one (caret-only moves fire no input event but still resync the menu).

### Edge Case 3: Re-picking a completed token

**Scenario**: Place the caret inside an already-inserted `@ember-owl ` and pick a
different member.

**Expected**: The whole token is rewritten cleanly to `@<new-id> ` with no doubled
space.

### Edge Case 4: Mention cap

**Scenario**: Author more than 10 distinct `@`-mentions in one post.

**Expected**: The composer lifts at most `MAX_MENTIONS` (10); the server rejects an
over-cap array with `400` (`channelMaxMentionsPerPublish`). *(Optional to drive by
hand — pinned by `TestChannels_PublishMessage_MentionsCountCap`.)*

---

## Security Posture (verify, do not skip)

| Check | Expected |
|-------|----------|
| Sender identity | The publish `sender_id` is the `/ui/context` principal, never a free-text field (RFC §F rule 1) — mentions do not introduce a sender override. |
| Mention targets | Only ids resolving to a current channel member are sent; unknown tokens stay inert text (no participant probing via the composer). |
| Render safety | Mention highlighting renders via escaped text segments (no `{@html}`), so message content cannot inject markup. |
