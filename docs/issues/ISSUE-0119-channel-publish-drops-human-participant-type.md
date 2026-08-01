---
id: ISSUE-0119
summary: "A human publishing into a group channel is delivered to personas with no `participant_type`, so the sender resolves to the `agent` default: cross-room person identity (RFC 0031 F-7) is queried on the agent-typed relationship row and misses everything learned in DMs — the persona greets a stranger — while the close path writes group-room trust/identity onto that same second row, permanently splitting one human across two participant records. ISSUE-0068 fixed exactly this for the REST chat surface; the group-channel publish path was never given the same stamp."
status: open
severity: high
area: memory
created: 2026-08-01
refs:
  - docs/issues/ISSUE-0068-chat-peer-recorded-as-agent-participant-type.md
  - docs/issues/ISSUE-0093-person-identity-cross-room-tier.md
  - docs/rfcs/0011-amendment-participant-type-wire-propagation.md
  - docs/rfcs/0031-per-session-namespacing-channels.md
  - docs/memory-scope-axes.md
  - internal/server/channel_handlers.go
  - internal/server/chat_handler.go
  - agents/persona_runtime/relationship_section.py
  - agents/sender_type.py
---

## Summary

Introduce yourself to a persona in a DM and it remembers you. Add that same
persona to a group channel and it does not — it has no idea who you are.

The room boundary is not the cause (cross-room identity is exactly what
RFC 0031 F-7 / [ISSUE-0093](ISSUE-0093-person-identity-cross-room-tier.md)
built). The cause is that the group-channel publish path never tells the
persona the sender is a **human**, so the identity read lands on the wrong
relationship row.

## Context

Person identity is stored on the relationship row keyed
`(other_participant_id, other_participant_type)`, and
[`recall_relationship_summary`](../../agents/persona_runtime/relationship_section.py)
resolves that type from `event.metadata["sender_participant_type"]`,
defaulting to `"agent"` when absent
([`agents/sender_type.py:43`](../../agents/sender_type.py)). The type reaches
the agent only if the publisher put `participant_type` into the message
metadata, which the dispatcher lifts onto the typed wire field
([`grpc_dispatcher_proto.go:85`](../../internal/channels/grpc_dispatcher_proto.go),
the [participant-type wire-propagation amendment](../rfcs/0011-amendment-participant-type-wire-propagation.md)).

**Exactly one publisher stamps it — the chat handler:**

| Path | Stamps `participant_type`? |
|------|----------------------------|
| REST chat / DM ([`chat_handler.go:346`](../../internal/server/chat_handler.go)) | ✅ `"user"` — fed by [`cli/src/commands/chat.rs:115`](../../cli/src/commands/chat.rs) and [`web/src/lib/api.js:241`](../../web/src/lib/api.js) |
| Channel publish ([`channel_handlers.go:307`](../../internal/server/channel_handlers.go)) | ❌ passes `req.Metadata` through untouched |
| `persatrix channel send --as <human>` | ❌ nothing auto-stamped (only the opt-in `--metadata` escape hatch) |
| Console channel composer ([`web/src/lib/api.js:422`](../../web/src/lib/api.js)) | ❌ body is `{sender_id, content, mentions?}` |

`handlePublishMessage` already knows the sender is the authenticated
principal (RFC 0039 §F rule 1 — the console passes the `/ui/context`-derived
id, never free text), so the information needed to stamp it is present at
the boundary and is simply not written.

## Reproduction

Deterministic, at the real recall path (no provider, no stack). Legs mirror
the operator report; Leg 3 is the control that isolates the participant-type
axis from the room axis:

| Leg | Room | `sender_participant_type` | Result |
|-----|------|---------------------------|--------|
| 1 | DM (`store_note` write-through, then recall) | `"user"` (chat path) | `Relationship with alex (Human user): Identity: Name: Maksim; Role: maintainer of Persatrix` |
| 2 | `group:planning` — **event shaped as the publish path delivers it** | absent | **`None`** — no relationship section at all |
| 3 | `group:planning`, everything else identical | `"user"` | identity recalled — the room was never the problem |
| 4 | re-introduce in the group, then read both rows | absent | two identity rows now exist: `(alex, "user")` and `(alex, "agent")` |

Leg 4 is the second-order damage: the close path stashes the same defaulted
type ([`episode_routing.py:368`](../../agents/persona_runtime/episode_routing.py)
→ `record_close`), so every group-room interaction accumulates trust,
notes, and identity on a second participant record for the same human, and
the two never merge.

## Impact

- **The v0.3.12 headline promise fails on its most visible surface.** "Tell a
  persona something in one room and it knows it in every room" holds for
  topic facts (the facts tier keys on the subject string, no participant
  type — `MT-MEMORY-CROSSROOM-001` passes live) but not for *who you are*,
  because person identity is the one tier keyed by participant type. A user
  who introduces themselves and opens a group minutes later gets nothing:
  no fact has been extracted yet (extraction runs at interaction close,
  ≥ 10 min idle + a following event), so identity rides the relationship
  tier alone — the tier this breaks.
- **Multi-party rooms are the v0.5.0 mainline** ([memory-scope-axes
  §Why this doc exists](../memory-scope-axes.md)) and the console's channel
  composer is the primary human surface today. Both are affected.
- **Data-shape damage, not just a missed read**: split rows require a
  backfill/merge to repair, and the split widens for as long as the stamp
  is missing.
- Humans in group channels are also not framed as human input in the prompt
  ([`prompt_assembly.py:438`](../../agents/persona_runtime/prompt_assembly.py)
  — no user-delimiter wrapping), and the reply-budget exemption reads the
  same key ([`reply_budget.go:177`](../../internal/channels/reply_budget.go)).

Severity `high` is proposed on the strength of the wrong-by-default person
record plus the primary-surface reach; downgrade to `medium` (matching
ISSUE-0068) if the split-row repair turns out trivial.

## Why the test suite is green

[`tests/unit/python/test_identity_render.py:153`](../../tests/unit/python/test_identity_render.py)
covers the cross-room leg with a hand-built event carrying
`metadata={"sender_participant_type": "user"}` — metadata the real
group-channel path never supplies. The harness stamps what production
drops, so the test asserts the design and not the wire.

## Proposed fix / investigation path

1. **Stamp at the publish boundary, server-side.** In `handlePublishMessage`,
   resolve the sender's participant type from the authenticated principal /
   channel roster (member ids are already validated there) and write
   `metadata["participant_type"]` before `PublishAsync` — rather than
   trusting caller-supplied metadata, which would let a client mislabel a
   peer. `readParticipantType` and the wire lift then work unchanged.
2. **Do not fix it in the CLI/console only** — a client-side stamp leaves
   every other publisher (bridges, A2A, programmatic callers) on the broken
   default, which is how ISSUE-0068's chat-only fix left this gap behind.
3. **Backfill/merge the split rows**: a migration folding `(id, "agent")`
   identity + relationship state into `(id, "user")` where the id is a known
   human principal. Merge semantics already exist for the identity JSON
   (`merge_identity`); trust/interaction counts need a decision.
4. **Regression pin at the wire, not the harness**: a test that drives the
   REST publish → dispatch → agent path and asserts the persona resolves a
   human sender as `"user"` without any test-supplied metadata.

## Notes

> 2026-08-01 — captured from an operator report ("agent remembered me in the
> chat, forgot me after I made a group channel"), reproduced deterministically
> at the recall path with the 4-leg arc above.
