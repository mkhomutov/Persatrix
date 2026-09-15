---
id: ISSUE-0158
summary: "The RFC 0037 §F recall filter is audience-blind: the persona's recall_channel_messages tool (POST /api/v1/personas/{id}/recall) binds the acting channel's classification but nothing about who is in the acting room, so on a group-room turn it returns a DM's transcript verbatim — the very entries the §D audience gate (ISSUE-0132, v0.3.16) just withheld from the same turn. Observed live at the v0.3.16 release-prep arc under the shipped auth.mode: disabled: Alice asked in planning, with Bob a member, about a fact she taught in her DM; the injection gate recorded both DM-taught facts withhold-disjoint and injected nothing, the model elected a recall round, the tool returned her two DM messages, and the reply repeated the fact in front of Bob while declining to. Under auth.mode: enabled the same tool 401s (ISSUE-0140), which is the only reason the enabled run did not leak."
status: resolved
severity: high
area: memory
created: 2026-09-15
closed: 2026-09-15
closed_pr: 954
refs:
  - docs/manual-tests/v0.3.16-execution-report.md
  - docs/manual-tests/MT-PERSONA-CONFIDENTIALITY-001.md
  - docs/rfcs/0037-memory-confidentiality-channel-classification.md
  - docs/rfcs/0037-amendment-audience-egress.md
  - docs/issues/ISSUE-0132-memory-egress-gate-blind-to-room-audience.md
  - docs/issues/ISSUE-0140-agent-fleet-401-on-roster-fetch-under-auth.md
  - docs/issues/ISSUE-0118-tool-recall-bypasses-epoch-session-scopes.md
  - agents/tools/recall.py
  - agents/persona_runtime/audience.py
  - internal/server/persona_recall_handlers.go
  - internal/channels/recall.go
---

# ISSUE-0158: The §F recall filter is audience-blind — `recall_channel_messages` hands a DM's transcript to a group-room turn

## Summary

v0.3.16 makes the persona weigh **who is in the room** before it speaks from
memory: the [RFC 0037 §D injection gate](../rfcs/0037-memory-confidentiality-channel-classification.md#d-the-hard-gate-at-memory-injection)
gained an audience condition ([the amendment](../rfcs/0037-amendment-audience-egress.md),
[ISSUE-0132](ISSUE-0132-memory-egress-gate-blind-to-room-audience.md)), so a
fact taught in Alice's DM is withheld in a room that adds Bob. The
[§F recall filter](../rfcs/0037-memory-confidentiality-channel-classification.md#f-recall-classification-filter)
did not: the persona's `recall_channel_messages` tool
([`agents/tools/recall.py`](../../agents/tools/recall.py)) calls
`POST /api/v1/personas/{id}/recall`, whose handler binds
`ActingClassification` ([`persona_recall_handlers.go`](../../internal/server/persona_recall_handlers.go))
and the RFC 0035 membership filter — the persona may read any channel it is a
member of, at or below the acting level — and nothing about the acting
room's members. A DM is `internal`; `planning` is `internal`; the DM's
messages come back.

## Context

Observed live at the v0.3.16 release-prep arc
([execution report](../manual-tests/v0.3.16-execution-report.md), Leg 5d — the
[MT](../manual-tests/MT-PERSONA-CONFIDENTIALITY-001.md)'s audience leg under
`auth.mode: disabled`, the shipped default). Alice taught "the Helix rollout is
paused until the security review clears" in her DM; the interaction closed and
consolidated two `internal` facts with `source_channel_id = dm:alice:ember-owl`;
Alice then asked in `planning`, where Bob is a member. The persona's own log
shows the two halves side by side:

- the injection gate: `audience egress (live) — 3 entries judged at
  acting='internal': 1 admit, 2 disjoint … 2 withheld`, both DM-taught facts at
  `withhold-disjoint`, nothing about Helix in the prompt;
- the model's tool round: `recall_channel_messages(query="Helix rollout")` →
  both DM messages, channel `dm:alice:ember-owl`, verbatim, wrapped as
  untrusted external data;
- the reply, in front of Bob: "You told me directly in a private DM — marked
  'between us for now' — that the Helix rollout is paused pending a security
  review. That was shared in confidence, so I'm not going to surface it in this
  group channel."

Under `auth.mode: enabled` the same turn drew `channels: recall ember-owl
returned HTTP 401` ([ISSUE-0140](ISSUE-0140-agent-fleet-401-on-roster-fetch-under-auth.md)
— the route is `policyAuthenticated` and personas hold no accounts) and the
reply carried nothing. So the enabled run passed **for a reason unrelated to
the audience gate**, and ISSUE-0140's eventual fix reopens this door under
`enabled` too.

This is the [ISSUE-0118](ISSUE-0118-tool-recall-bypasses-epoch-session-scopes.md)
shape one axis over: a model-elected tool round reaches storage on a path the
per-turn gate does not cover. ISSUE-0118 closed the epoch/session axis for the
*memory* tools; the *channel* recall tool sits on the RFC 0037 §F axis, which
the audience amendment scoped to §D only.

## Impact

The release headline — "the persona does not repeat what I told it in front of
someone I did not tell" — holds for the injection path and fails end-to-end
whenever the model elects a channel recall round on a group-room turn. The
tool is granted by the `channels:recall` permission the stock personas carry;
the model calls it exactly when memory injection gave it nothing, i.e.
precisely on the turns the audience gate withheld. Severity **high** — the
same class as ISSUE-0132, on the shipped default posture.

## Proposed fix / investigation path

Give §F the audience condition §D has, server-side: the recall request carries
the **acting channel id** (the persona knows it from the event; the tool
already binds the participant and the acting classification), and the handler
admits a message only if its channel's current member set is a subset of the
acting channel's — the same comparison [`audience.py`](../../agents/persona_runtime/audience.py)
makes, computed from the two member lists the channel store already holds.
Narrowing-only, like the existing membership filter. The
[amendment](../rfcs/0037-amendment-audience-egress.md) gains a §F paragraph.
Whether this ships in v0.3.16 as an in-release fix or as a stated Known Gap is
the maintainer's call, flagged on release-prep PR 1.

## Notes

> 2026-09-15 — filed from the v0.3.16 release-prep arc (F-2 in the execution
> report). Not a regression — the tool predates the audience gate — but until
> v0.3.16 the injection path leaked the same fact, so the side door was moot.
>
> 2026-09-15 — **Resolved in release-prep PR 1** ([#954](https://github.com/mkhomutov/Persatrix/pull/954)),
> the in-release fix the maintainer chose over a Known Gap. The recall
> request carries `acting_channel_id`, bound by the tool from the turn's
> channel (`agents/acting_channel.py`, the acting-classification seam's
> twin) and sent only when `memory.egress.audience` resolves `live`; the
> orchestrator's recall query gains the member-subset clause
> (`audienceScope` in `internal/channels/sqlite_search.go`) and the audit
> names the acting room. TDD both sides: `sqlite_search_audience_test.go`,
> `persona_recall_handlers_audience_test.go`, `test_recall_tool_audience.py`.
> The [amendment](../rfcs/0037-amendment-audience-egress.md#f--the-recall-filter-carries-the-same-condition-issue-0158)
> gains its §F section; Leg 5d re-ran on the fixed image — see the report.
