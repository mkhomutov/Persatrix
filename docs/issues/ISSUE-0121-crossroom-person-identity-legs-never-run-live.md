---
id: ISSUE-0121
summary: "MT-MEMORY-CROSSROOM-001 Legs 1b/2b (the person-identity half of the headline promise) have never been executed live. They were added at MT v1.1 after ISSUE-0119 — every human publishing into a group channel arriving untyped — reached the v0.3.12 release candidate on a green run of the v1.0 legs, which all name a topic and so structurally cannot observe an identity-tier break. v0.3.12 shipped on the v1.0 bar by maintainer call (#801): the wiring half is pinned deterministically in CI (the #799 wire test drives REST → router → dispatcher → a real gRPC receiver; #800 folds split rows at migration v17), so what stays unverified is the qualitative half — whether a real persona records an introduction via store_note(contact:<id>) at all, and then uses the identity in a room where it was never told. That capture step is model-elected and has no deterministic CI analogue. Standing deliverable for the next memory-touching release."
status: open
severity: medium
area: memory
created: 2026-08-01
refs:
  - docs/manual-tests/MT-MEMORY-CROSSROOM-001.md
  - docs/manual-tests/v0.3.12-execution-report.md
  - docs/v0.3.12-release-checklist.md
  - docs/issues/ISSUE-0093-person-identity-cross-room-tier.md
  - docs/issues/ISSUE-0119-channel-publish-drops-human-participant-type.md
  - docs/issues/ISSUE-0120-backfill-split-participant-type-relationship-rows.md
  - internal/server/channel_participant_type_test.go
  - agents/tools/identity_write_through.py
---

## Summary

The headline promise has two halves that fail independently: *what* the persona
knows (the facts tier) and *who it is talking to* (the RFC 0031 F-7 person
identity on the relationship row). `MT-MEMORY-CROSSROOM-001` v1.0 covered only
the first. Legs 1b/2b were added at v1.1 to cover the second and **have never
been run**.

## Context

v1.0 passed live at the v0.3.12 candidate on 2026-07-30 while
[ISSUE-0119](ISSUE-0119-channel-publish-drops-human-participant-type.md) rode
the same green run. That was not bad luck: identity seeds from the **sender**,
not the trigger text, and every v1.0 leg names a topic — so no v1.0 leg could
observe an identity-tier break no matter how it failed. Only a leg that names
*no* topic exercises the tier at all.

The deferral call is recorded in the [release checklist
§4](../v0.3.12-release-checklist.md) and taken on the same grounds as the
[ISSUE-0118](ISSUE-0118-tool-recall-bypasses-epoch-session-scopes.md) row: what
actually broke is now pinned deterministically, so the tag is not held.

- **Wire** — [`internal/server/channel_participant_type_test.go`](../../internal/server/channel_participant_type_test.go)
  drives REST → router → dispatcher → a real gRPC receiver with no
  `participant_type` in the request body and reads the type off the delivered
  proto event ([#799](https://github.com/mkhomutov/Persatrix/pull/799)).
- **Store** — migration v17 folds a human's split relationship rows back into
  one person ([#800](https://github.com/mkhomutov/Persatrix/pull/800),
  [ISSUE-0120](ISSUE-0120-backfill-split-participant-type-relationship-rows.md)).

## Impact

What remains unverified is the qualitative half, and one part of it has no
deterministic CI analogue at all:

1. **Capture is model-elected.** Identity lands only if the persona chooses to
   call `store_note(topic="contact:<id>")` — unlike the close-path fact
   extractor, which runs deterministically. A prompt or model change that makes
   personas tool-shy would silently stop identity capture, and nothing in CI
   would notice.
2. **The row key comes from the note's topic**, only the type from the sender
   ([`identity_write_through.py`](../../agents/tools/identity_write_through.py)
   — `other_id = topic[len("contact:"):]`). A persona told *"I'm Maksim"* may
   write `contact:maksim`, producing a row that exists, is correctly typed, and
   is still invisible to a read keyed on the sender id. The write-through's own
   docstring flags this as a prompt-enforced contract, not an enforced one.
3. **Recall-to-use.** Whether an admitted identity line is actually used in the
   reply is the same recall-vs-reasoning distinction the facts legs draw, and
   is only observable live.

The wiring guarantee is strong; the behavioural one is untested. `EVAL`-tier
CI replays the runtime deterministically and cannot substitute — the point of
the legs is a real provider electing (or not electing) to record a person.

## Proposed fix / investigation path

Run the **whole** `MT-MEMORY-CROSSROOM-001` arc at the next memory-touching
release and record Legs 1b/2b explicitly in that release's execution report —
not just the legs that changed. The MT's Status line carries this as its
standing deliverable.

Two things to watch on the first live run, both documented in the MT:

- **Leg 1b's row must be keyed on the sender id** (`alex`), typed `user`. A
  row keyed on a name is Edge Case 3, a capture-path finding — not a
  cross-room recall failure.
- **A `agent`-typed row at Leg 1b** is an
  [ISSUE-0068](ISSUE-0068-chat-peer-recorded-as-agent-participant-type.md)
  regression of the DM/chat stamp; ISSUE-0119 is the channel-publish path that
  Leg 2b exercises. Different files, different fixes.

If capture proves unreliable across providers, the durable fix is upstream of
this MT — a deterministic capture path for identity, the way fact extraction
already has one — rather than more MT legs.

## Notes

> 2026-08-01 — filed at [#801](https://github.com/mkhomutov/Persatrix/pull/801)
> alongside the MT v1.1 legs themselves, so the coverage gap is tracked from
> the moment it is created rather than discovered later. Deferral is a
> maintainer call, not an oversight; this issue exists to keep the call
> visible after the v0.3.12 tag ships.

> 2026-08-02 — **v0.3.12 is released** on the MT v1.0 bar as the deferral
> intended ([v0.3.12 — Memory that travels](https://github.com/mkhomutov/Persatrix/releases/tag/v0.3.12));
> the legs remain unrun. Listed in the published release body's Known Gaps.
> Carried to the **next memory-touching release** — whichever of v0.3.13 /
> v0.4.0 first moves the cross-room or identity surfaces is the one that
> must run legs 1b/2b before its own sign-off.

> 2026-08-02 (later) — the
> [sequencing Amendment 2026-08-02](../v0.3.x-sequencing.md#amendment-2026-08-02--v0313--v0314-the-two-release-tail-to-v040)
> resolves the "whichever release first" clause above: **v0.3.13
> (Deferred calls closed) is that release**, and this issue is one of its
> three named scope items. Sequencing inside the release per the
> amendment's dependency chain: run the whole arc **after** the
> [ISSUE-0118](ISSUE-0118-tool-recall-bypasses-epoch-session-scopes.md)
> executor-hop fix lands, so one live run verifies both the threading fix
> and legs 1b/2b. A red leg becomes in-release fix work, not a further
> deferral (the amendment's risk table says this explicitly).

> 2026-08-03 — **v0.3.13 plan opened**
> ([v0.3.13-plan.md](../v0.3.13-plan.md)): this issue is scoped as the
> release-prep live deliverable, not an implementation PR — the whole
> `MT-MEMORY-CROSSROOM-001` v1.1 arc runs at release-prep **after** the
> [ISSUE-0118](ISSUE-0118-tool-recall-bypasses-epoch-session-scopes.md)
> fix merges, with legs 1b/2b recorded explicitly in the execution report
> per the MT's run contract (Leg 1b row keyed on the sender id, typed
> `user`; wiring-vs-reasoning triage before filing).
