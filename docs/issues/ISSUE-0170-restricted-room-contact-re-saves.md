---
id: ISSUE-0170
summary: "In a restricted or secret room a persona keeps a person's name only as a room-scoped contact note, because the identity write-through is withheld above internal. Nothing it can see confirms the save: the name never reaches its Relationship block, and store_note returns only a note id. So while the introduction is still in its window it saves the same note again, and on some of those turns the tool call is followed by no text, so the user gets the DM fallback reply '…'. Seen live on claude-sonnet-4-6: four identical contact notes over the three turns after the introduction, two of them answered with '…'."
status: open
severity: low
area: memory
created: 2026-09-25
refs:
  - https://github.com/mkhomutov/Persatrix/pull/995
  - agents/tools/memory_tools.py
  - agents/tools/identity_write_through.py
  - agents/persona_runtime/relationship_section.py
  - agents/persona_runtime/channel_reply.py
  - prompts/runtime/safety/memory-tool-usage.md
---

# ISSUE-0170: A persona re-saves a contact note in restricted rooms, and some of those turns reply "…"

## Summary

When a user tells a persona their name in a room classified `restricted` or
`secret`, the persona saves it as an ordinary contact note that stays in that
room. It then has no sign that the save happened: the name does not appear in
its Relationship block, and the tool result looks like any other note's. While
the introduction is still in view it saves the same note again. On some of
those turns it ends with the tool call and no reply text, so the user gets the
DM fallback "…" instead of an answer.

## Context

Found in the behaviour probes run after
[#995](https://github.com/mkhomutov/Persatrix/pull/995) merged, on `main` at
`054f0de2`. The probe used `make demo-anthropic` (`quality` =
`claude-sonnet-4-6`) with `dm_default_classification: restricted` set for the
run, so a new DM was stamped `restricted`.

**What happened.** The user said "Hi! I'm Dana, the CFO here. Please remember
my name and role." The persona called `store_note` with topic
`contact:<user_id>`. The identity write-through was withheld, as designed
([`identity_write_through.py`](../../agents/tools/identity_write_through.py)
returns False when the acting level outranks `internal`), so the content was
saved as a room note stamped `restricted`. The user then sent ten short,
unrelated questions:

- On each of the first three, the persona called `store_note` again with the
  same topic and content. The agent log shows "Identity write-through withheld
  for topic contact:…" three more times, and the store held four identical
  notes, written 26, 30 and 34 seconds after the first.
- On the first and third of those turns, the visible reply was "…". That is
  `_DM_EMPTY_REPLY_FALLBACK` in
  [`channel_reply.py`](../../agents/persona_runtime/channel_reply.py), used
  when a DM turn ends with no text. The model had made the tool call and then
  returned nothing.
- From the fourth question on, it answered normally and saved nothing.

After the introduction had left the 20-message window, "do you remember who I
am?" made the persona call `recall_notes`, which returned all four notes, and
it answered "Yes — you're Dana, CFO." So the fallback that #995 added works;
the duplicates and the empty replies are the problem.

**Why it repeats.** The memory snippet
([`memory-tool-usage.md`](../../prompts/runtime/safety/memory-tool-usage.md))
tells the persona to call `store_note` with topic `contact:<user_id>` as soon
as a user gives their name or role, and says that what it knows about a person
shows under "Relationship with <user_id>". On an `internal` channel the
identity reaches the relationship tier, the block shows it, and the persona
can see the save took effect. On a `restricted` or `secret` channel the block
never shows it
([`relationship_section.py`](../../agents/persona_runtime/relationship_section.py)
renders the relationship tier only). `store_note` returns `note_id` and
`topic`, the same as any note
([`memory_tools.py`](../../agents/tools/memory_tools.py)). The per-turn notes
lookup runs on the words of the incoming message, so it does not bring the
note back for an unrelated question. Each turn the persona sees the
introduction in its transcript and no record of it, and saves again.

## Impact

Bounded, and not a leak: the duplicates stay in the room at the room's own
level. It is visible to the user, though. A person who introduces themselves in
a restricted room gets "…" instead of an answer on some of the next few turns,
and each repeat costs an extra tool round. The duplicates also fill note slots
(`max_notes`, 500 by default) and come back several times from every
`recall_notes`. When `dm_default_classification` is `restricted`, every DM
behaves this way.

## Proposed fix / investigation path

Any one of these stops the loop; the first two are small.

1. **Say what happened in the tool result.** On the fallback path, have
   `store_note` return that the contact was kept as a note in this
   conversation only, for example `kept_in_this_conversation: true` next to
   `note_id`.
2. **Don't write a duplicate.** When a note with the same topic and content
   already exists in the session, return its id instead of writing another.
   This helps every note, not only contacts.
3. **Show the room's own contact note for the sender** in the prompt when the
   identity was withheld. The note carries the room's level, so the §D gate
   admits it in that room.

The empty replies are a separate question: whether a DM turn that ends with a
tool call and no text should ask the model once more for a reply rather than
posting "…".

Confirm with the same probe: a restricted DM, an introduction, ten unrelated
questions. Count the contact notes and the "…" replies.

## Slot

Not slotted. No release plan is open: ruling (a) of the
[sequencing Amendment 2026-09-12](../v0.3.x-sequencing.md#amendment-2026-09-12--close-v0316-small-then-measure-before-any-train-opens)
opens none before EXP-001 reports and the first strategy review logs its
result. EXP-001 is not affected: its channel is declared `internal` in
[`panel.yaml`](../../evaluators/experiments/EXP-001/panel.yaml), and its
deployments never set `dm_default_classification`, so any DM stays `internal`.

## Notes

> 2026-09-25 — filed from the post-merge behaviour probes for #995 (the
> restricted-room remember-a-name probe). One run on `claude-sonnet-4-6`; not
> yet tried on other models. The same run passed the check it was designed for:
> the persona found the contact note with `recall_notes` once the introduction
> was out of view.
