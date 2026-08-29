# ISSUE-0082 — PR Implementation Plan (Residuals R-1 / R-2 — the derived and relayed tenant writes)

**Issues**: [ISSUE-0123](ISSUE-0123-per-speaker-interaction-scope.md) (R-1) · [ISSUE-0124](ISSUE-0124-orchestrator-hop-drops-tenant-on-agent-cascade.md) (R-2) · [ISSUE-0131](ISSUE-0131-derived-memory-has-no-speaker-attribution.md) (the speaker axis)
**Status**: 🔄 In progress — Phase 0 resolved (both axes); workstream **A** of the **v0.3.15** *Who said what* milestone, open at PR 3 (PRs 1–2 merged)
**Created**: 2026-08-07
**Branch prefix**: `feature/v0315-issue0123-` / `feature/v0315-issue0124-` (per residual)
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Spawned from**: [ISSUE-0082](ISSUE-0082-orchestrator-per-request-session-principal-emission.md) Part 2 review ([#820](https://github.com/mkhomutov/Persatrix/pull/820))
**RFC**: [RFC 0020 §G](../rfcs/0020-interaction-lifecycle.md#g-per-channel-scoping) — R-1 amends the per-channel scoping table

---

## Overview

v0.3.14 made the caller's verified principal a per-request axis: the orchestrator emits `persatrix-principal` at the `Dispatch` chokepoint and the persona binds a strict-equality `principal_scope` for the handler's lifetime. **That boundary holds for every turn's own write.** It does not hold in two places, both surfaced at the Part 2 review and both deferred here:

* **R-1 — the *derived* write.** The RFC 0020 interaction scope is the ROOM, not the speaker, so a group channel accumulates every speaker's turns into one `InteractionTracker` record. At close, that record is summarised and RFC 0026 facts are extracted from it under whichever principal closed the interaction.
* **R-2 — the *relayed* write.** A persona's reply re-enters through `HTTPChannelPublisher` as a fresh unauthenticated REST publish, so every fanout below it loses the tenant even inside an authenticated person's interaction. **Confirmed live 2026-08-07**: 9 of 15 `channel.dispatch` spans in one interaction descending from an authenticated publish carried no `principal.id`. Note it is invisible in storage — R-1 re-attributes the relayed turns at close — so the wire is the only instrument ([ISSUE-0124](ISSUE-0124-orchestrator-hop-drops-tenant-on-agent-cascade.md)).
* **The speaker axis ([ISSUE-0131](ISSUE-0131-derived-memory-has-no-speaker-attribution.md)).** The two above are *tenant* problems, and the principal is a tenant identifier: only authenticated humans have one. A room full of personas resolves to the single shared `local` principal, so partitioning by principal alone leaves every agent speaker in one bucket — and the Phase 0 evidence below is exactly that case. Folded into this workstream at the v0.3.15 re-point (§Phase 0b), because it shares the record-shape decision rather than because it shares a migration: `principal_id` on `messages` is a Go channel-store change that belongs to ISSUE-0130 shape (b), while the speaker axis lands in the Python persona-memory store.

**This plan ships both.** It adds no new transport, no new storage column, and no new binding mechanism — `principal_id` has been in the storage key since migration v11 and the emission rail shipped in v0.3.14. What it adds is *attribution*: which principal a derived or relayed write belongs to.

### Why they are one workstream, not two

Shipping either alone leaves a defect shaped like the one it closed.

* **R-1 alone.** Agent-origin turns carry no principal, so every group room accumulates a systematic `'local'` record holding all agent turns — and a persona's restatement of A's disclosure lands there, recallable by every agent-origin and autonomous turn in every room.
* **R-2 alone.** Relayed turns get the right principal, but the close-time aggregate still spans speakers and lands under one of them.

Together the relayed turn carries the causal principal *and* lands in that principal's own record. They may ship as separate PRs (below) but must land in the same release, and the release note should not claim either boundary before both are in.

### What is already in place (do not rebuild)

* `channels.WithPrincipal` / `PrincipalFromContext` — the ctx carrier, pinned to survive the fanout's `context.WithoutCancel` hop (v0.3.14 PR 1).
* `grpcmeta.InjectPrincipal` at `GRPCMessageDispatcher.Dispatch` — the single emission site, with the `principal.id` span attribute (v0.3.14 PR 1).
* `Server.authMiddleware` → `withRequestPrincipal` — structural threading of the RFC 0039 §F verified `participant_id`, keyed on `authIdentity.Authenticated` (v0.3.14 PR 2).
* `dispatchOriginClassification` + `principal_route_table_test.go` — the origin enumeration; an unclassified route fails the build.
* Persona-side: `Interaction.session_id` frozen-at-open (the sibling-mislabel guard R-1 copies), `DispatchContext.origin_principal_id` threading across the executor hop (v0.3.13 PR 1).

---

## Phase 0 — the design gate ✅ RESOLVED (both axes)

The gate ran and decided. Its full evidence record — the live run, the option
tables, the decision rule, and the cost each answer carries — lives in
[ISSUE-0082 residuals — the Phase 0 design gate](ISSUE-0082-residuals-phase0-gate.md).

| Axis | Question | Answer | Date |
|---|---|---|---|
| Principal | one record per room, or per tenant? | **Option A** — tracker keyed `(principal, scope)` | 2026-08-07 |
| Speaker ([ISSUE-0131](ISSUE-0131-derived-memory-has-no-speaker-attribution.md)) | …and per speaker within it? | **key-side** — `(principal, speaker, scope)` | 2026-08-21 |

Net: the tracker key is `(principal, speaker, scope)`. PRs 3–4 below are
written for that answer. Two consequences carried forward — the close reserve
becomes `1 + (personas × principals × speakers)` (PR 4 sizes it), and a
persona's close-derived memory of a group discussion fragments per speaker
per tenant (stated in PR 3's release note).

---

## Dependency Graph

```
Phase 0  gate (MT Legs 1–4) ─────────────┐
   └─→ Phase 0b (same evidence,           │ (locks the record shape:
        speaker axis / ISSUE-0131)        │  principal AND speaker)
                                          │
PR 1 (R-2 store, dormant)                 │
   └─→ PR 2 (R-2 re-stamp + gate)         │
          └─→ PR 3 (R-1 scope key) ←──────┘
                 └─→ PR 4 (R-1 close path + reserve)
                        └─→ PR 5 (live MT + closeout)
```

PR 1 is unblocked by the gate and can start immediately after the tag. PR 3 must not start before the gate resolves — both halves of it: Phase 0 fixes the principal dimension, Phase 0b the speaker dimension, and PR 3 installs the key that carries both.

---

## PR Sequence

### PR 1: `feature/v0315-issue0124-attribution-store` — Causal attribution, dormant

#### Scope

`internal/channels/principal_attribution.go`: a per-`(channel, agent)` table recording which principal each dispatch was made under, written from `Dispatch` for every delivered dispatch the router elected a reply from. **No read site** — nothing re-stamps yet. Mirrors the v0.3.14 dormant-rail-then-producer split. Rationale, rules, gates and the two review rounds: the file header and [#844](https://github.com/mkhomutov/Persatrix/pull/844).

---

### PR 2: `feature/v0315-issue0124-restamp` — The re-stamp, and the gate

#### Scope

Read the table on the publish path, before the commit and the fanout, **iff** the ctx carries no principal and the sender is a registered agent. On a live unambiguous hit, `ctx = WithPrincipal(ctx, p)`. This is where R-2's behaviour changes.

#### Key implementation details

* Read in the router, not `handlePublishMessage`, so in-process callers are covered — and specifically in **`publishCommit`**, not `Publish`: the REST seam a persona's reply actually takes has gone through `PublishAsync` since the RFC 0048 latency fix, so reading in `Publish` would have covered the chat façade and missed R-2's own path. Both entry points share the commit path and fan out on the context it returns.
* Read with **`TakeAttribution`, never `Lookup`**: the consuming read retires what the reply answered. `Lookup` leaves those stimuli live, so a room whose forced turns outpace the TTL (the RFC 0052 convener cadence) stays ambiguous forever and R-2 is inert where it is most needed.
* `msg.SenderID` is safe as a key: the executor supplies the agent's registered id and never an LLM-supplied value (RFC 0011 §"DM gate-bypass").
* **Never** accept a principal, or any correlation key, from the agent's request — including the stimulus message id: an agent sees other members' ids in channel history, so a chosen id resolves to a chosen principal, the cross-tenant read primitive one indirection along.
* Deeper cascades inherit transitively (PR 1's write fires again with the principal now on ctx), bounded by `cascade_depth`.

---

### PR 3: `feature/v0315-issue0123-scope-key` — Per-speaker scope (Option A + Phase 0b)

> Written for Phase 0's Option A **and** Phase 0b's key-side speaker answer. Re-scoped only if either gate is re-opened.

#### Scope

RFC 0020 §G amendment (the per-channel scoping table gains a principal dimension **and** a speaker dimension) + `Interaction.principal_id` and `Interaction.speaker_id` frozen at open + `InteractionTracker._open` keyed `(principal, speaker, scope)` + room-wide close fan + the persona-memory migration that adds the column.

#### Key implementation details

* `principal_id: str = DEFAULT_PRINCIPAL_ID` on `Interaction`, resolved from the ambient scope at open, never re-read — the footing `session_id` already sits on.
* `speaker_id: str` on `Interaction`, resolved from the triggering event's `sender_id` at open and frozen the same way. This is the [ISSUE-0131](ISSUE-0131-derived-memory-has-no-speaker-attribution.md) column, and it is a **projection of the key** — recall renders attribution off it, so a fact can distinguish testimony from hearsay without any model-elected attribution.
* **Persona-memory migration 17 → 18** stamps the speaker on the **derived rows** — `episodes` and `facts`, the two tiers a group close writes (`store_episode` → `store_extracted_facts`). Name them explicitly, because two nearby targets are the wrong ones: the `interactions` TABLE is the relationship-tier log, written only by `record_closed_interaction`, which returns early for every non-DM scope ([`agents/persona_runtime/record_close.py`](../../agents/persona_runtime/record_close.py)) and so is never written for the group rooms this release is about; and `Interaction` itself is an in-memory dataclass ([`agents/memory/interaction_types.py`](../../agents/memory/interaction_types.py)) that needs no migration at all. Registry currently tops out at 17 ([`agents/memory/_migration_registry.py`](../../agents/memory/_migration_registry.py)). This is the Python store, distinct from the Go channel-store change `principal_id`-on-`messages` (ISSUE-0130 shape (b)) — two stores, two migrations, sequenced independently.
* **Tuple key, not an encoded scope string.** `scope` is persisted to `episodes.scope`, prefix-matched by `is_group_scope` / `is_thread_scope`, and is the `idx_episodes_scope` surface; principal and speaker each have their own column.
* **DM scope already answers this for one topology.** `scope_for_channel_event` routes a DM to `scope_for_dm(local_agent_id, sender_id)`, so a DM is keyed per-speaker today. The change makes group and thread scopes consistent with the DM case rather than introducing a new idea.
* **Room-wide close is inseparable from the keying** and must land in this PR: a structural close, an end-vote quorum or the close-notification turn is a *room* event, and without the fan a room close closes one `(principal, speaker)` record and leaks the rest open until idle.

#### Tests

One tracker, two principals, one room scope → two records. One tracker, one `local` principal, **three agent speakers**, one room scope → three records (the Phase 0b case: this is the test that fails under plain Option A). A room-wide close closes all of them. The close-notification turn lands as the final turn of each.

---

### PR 4: `feature/v0315-issue0123-close-path` — The `speaker_id` projection

#### Scope

Write the migration-18 column: project the record key's speaker half onto the two close-derived tiers, and discharge the RFC 0020 §G obligation that projection depends on.

**Deviation, 2026-08-29 (revised).** Two deliverables landed early, found by PR 3's review as defects: the **principal binding** shipped in PR 3 (`3e633571`) across both write phases, and the **`speaker_id` projection** was branched here. That inverts this PR — the projection is now the whole of it, and **the reserve re-size and the Go-side asymmetry cleanup move to PR 4b**. Those carry their own sizing analysis and a Go threshold-basis question the milestone keeps off this PR; bundling them repeats what cost PR 3 five review rounds.

#### Key implementation details

* **A projection, never a judgement.** `close_path` stamps `interaction.speaker_id` on the episode row, `fact_extractor` on every tuple that close extracts, the single-turn path on its own row — all `or None`, so a speakerless scope records NULL rather than an attribution to a speaker named nothing. Sound only because `(principal, speaker, scope)` makes each record single-speaker by construction.
* **The §G breach is discharged as EXCLUDE, not tag.** The close-notification fan lands the room-close turn on every sibling record. `close_entries.interaction_to_entries` drops it where its `sender` is not that record's speaker — upstream of the combined summarise+extract call, the one point where a single decision covers both outputs — keyed off the producer's recorded `room_close` stamp, not a reconstruction. The turn stays on the closer's OWN record, where it is native.
* **Three 500-line splits were preconditions, not cleanup**: `interaction_tracker.py` → `interaction_key.py`, `facts.py` → `_facts_write.py`, and (at review) `summarize_close.py` → `close_entries.py`. Each sat exactly at the cap, so the change could not be added at all.

#### PR checklist

- [x] Rebased onto `main` after PR 3's squash-merge, then opened
- [x] Metric shape (`agent.interactions.closed.by_<reason>` fires once per **record**) in the changelog — with PR 3
- [x] Release note states the coherence trade: close-derived memory of a group room is per-person **and per-speaker**; room continuity unaffected — with PR 3
- [x] PR body states what is NOT in it — `1 + N` ships unchanged, so a multi-speaker room's leases can over-commit the metered headroom

---

### PR 4b: reserve re-size and Go-side asymmetry cleanup

#### Scope

Split out of PR 4 on 2026-08-29. Re-size the wallet reserve; retire the Go-side asymmetry.

#### Key implementation details

* **The asymmetry is retired, not resolved.** Once the record names its principal, the trigger's principal no longer selects a tenant, so the four close paths in `internal/channels/synthesis_close.go` need not agree. Audit whether `pendingSynthesisClose.principal` becomes dead and drop it if so.
* **Reserve re-size**: `1 + N` becomes `1 + (personas × principals × speakers)` — the largest single cost here, and under-sizing degrades *silently* into `SUMMARY_UNAVAILABLE_TEXT`. The multiplier, the half-cap clamp, and the signal / threshold-basis obligations are the [reserve-sizing record](ISSUE-0082-residuals-reserve-sizing.md), split out on 2026-08-23 to hold exactly this; file the calibration in the [ISSUE-0109](ISSUE-0109-rfc0052-autonomous-defaults-calibration.md) idiom rather than guessing a constant.

#### PR checklist

- [ ] Re-size ships with its **signal**, and the calibration is filed as its own issue

---

### PR 5: `feature/v0315-issue0082-residuals-close` — Live verification + closeout

#### Scope

Run [MT-MEMORY-GROUP-TENANT-001](../manual-tests/MT-MEMORY-GROUP-TENANT-001.md) end to end on the post-fix column, execution report, issue closures (ISSUE-0123, ISSUE-0124, [ISSUE-0131](ISSUE-0131-derived-memory-has-no-speaker-attribution.md), and ISSUE-0082 itself), and the MT-MEMORY-MULTIUSER-001 Edge Case 2 wording widened back to the now-true claim.

**Amended 2026-08-23 by the [v0.3.15 plan](../v0.3.15-plan.md)**: this is the *extended* MT and runs **once**, at Phase 3, after **PR B2** — which authors the ISSUE-0130(b) restart leg and widens Leg 4. Running earlier burns the paid arc on a column that cannot observe either.

#### PR checklist

- [ ] All **nine** legs (0–8) plus the restart leg green on a live provider, with the post-fix column stated
- [ ] Leg 2's per-dispatch `principal.id` table (storage cannot see R-2) and Leg 4 `(principal_id, speaker_id, summary)` **triples** pasted verbatim
- [ ] ISSUE-0082 closed — its Part 2 residuals note updated. The doc-cap problem this checklist anticipated is **already handled**: the v0.3.14 build log was split into [Part 2](ISSUE-0082-part2-v0314-build-log.md), leaving 1639/3000. Measure with `scripts/checks/file_size.py`, not `wc -w`

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| The Phase 0 + 0b cost multiplier bites a large roster. | The reserve re-size is PR 4b's whole scope, and its calibration is a tracked issue. **Phase 0b makes this materially worse than the principal axis alone**: the multiplier is now `personas × principals × speakers`, and an all-agent room — one `local` principal, N speakers — multiplies where plain Option A did not. A one-human DM is still unchanged. |
| Phase 0b is read as re-opening a resolved gate. | It resolves a second axis on the *same* 2026-08-07 evidence, using the *same* decision rule, and does not disturb Option A. What changed is that [ISSUE-0131](ISSUE-0131-derived-memory-has-no-speaker-attribution.md) named a dimension the original three options did not enumerate — the gate's rule always implied it, since its decisive leak was agent-to-agent. |
| R-2's attribution mis-fires under load and stamps the wrong tenant. | Every ambiguity and every expiry fails closed to `'local'` — today's behaviour. A wrong attribution requires two dispatches under the *same* principal, which is not ambiguous because it is not wrong. |
| The re-stamp is read as "agents can now claim identities". | It is server-held state keyed on facts the orchestrator knows; nothing is accepted from the request. Stated in the PR body and pinned by the only-one-re-stamp-site test. |
| R-1 lands without R-2 (or vice versa) because one slips. | They are one release, not one PR. If one slips, **both** hold — the release note claim is unsafe with either half missing. |
| The RFC 0020 §G amendment re-opens the scoping design. | The amendment records the decision Phase 0 made on evidence; it does not re-derive it. |

---

## Progress Overview

| # | Title | Branch | Status | GitHub PR | Merged |
|---|-------|--------|--------|-----------|--------|
| 0 | Design gate — MT Legs 1–4, lock the record shape (both axes) | — | ✅ Resolved → **`(principal, speaker, scope)`**: Phase 0 (principal) 2026-08-07, Phase 0b (speaker) 2026-08-21 | — | — |
| 1 | R-2 causal attribution store, dormant | `feature/v0315-issue0124-attribution-store` | ✅ Merged | [#844](https://github.com/mkhomutov/Persatrix/pull/844) | `5b740f84` |
| 2 | R-2 re-stamp + end-to-end gate | `feature/v0315-issue0124-restamp` | ✅ Merged | [#845](https://github.com/mkhomutov/Persatrix/pull/845) | `48b4a558` |
| 3 | R-1 + [ISSUE-0131](ISSUE-0131-derived-memory-has-no-speaker-attribution.md) scope key `(principal, speaker, scope)` + RFC 0020 §G amendment | `feature/v0315-issue0123-scope-key` | ✅ Merged | [#846](https://github.com/mkhomutov/Persatrix/pull/846) | `5e23246c` |
| 4 | [ISSUE-0131](ISSUE-0131-derived-memory-has-no-speaker-attribution.md) `speaker_id` projection onto the close-derived rows + the RFC 0020 §G room-close exclusion | `feature/v0315-issue0123-close-path` | 🔀 PR open | [#849](https://github.com/mkhomutov/Persatrix/pull/849) | — |
| 4b | Reserve re-size (until it lands, a cost-trigger room fan over-commits `1 + N`) + Go-side asymmetry cleanup — split out of PR 4 | — | ⬜ Not started | — | — |
| 5 | Live MT + closeout | `feature/v0315-issue0082-residuals-close` | ⬜ Not started | — | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged · ⏭ Deferred

---

## Related Documentation

- [v0.3.15 plan](../v0.3.15-plan.md) — the milestone this workstream rides (workstream A). It delegates PRs 1–5 below whole, and owns the two workstreams this plan does not: ISSUE-0130 shape (b) and ISSUE-0125.
- [ISSUE-0082](ISSUE-0082-orchestrator-per-request-session-principal-emission.md) — the parent; its Part 2 note states both residuals.
- [ISSUE-0082 residuals — the Phase 0 design gate](ISSUE-0082-residuals-phase0-gate.md) — the evidence record for the record-shape decision, both axes. Split out of this plan on 2026-08-21 when the [ISSUE-0131](ISSUE-0131-derived-memory-has-no-speaker-attribution.md) fold-in pushed the combined doc past the 3 000-word cap.
- [ISSUE-0082 residuals — the close-path reserve re-size](ISSUE-0082-residuals-reserve-sizing.md) — PR 4's sizing analysis, the half-cap clamp, and the signal / threshold-basis obligations. Split out 2026-08-23 for the same reason.
- [ISSUE-0082 Part 1 PR plan](ISSUE-0082-part1-session-emission-pr-plan.md) — the session axis, the shape this plan mirrors.
- [RFC 0020 §G](../rfcs/0020-interaction-lifecycle.md#g-per-channel-scoping) · [RFC 0049](../rfcs/0049-memory-consolidation-gradient.md) Phase 1 · [RFC 0011](../rfcs/0011-channels-bridges.md) · [RFC 0039 §F](../rfcs/0039-user-accounts-authentication.md)
- [MT-MEMORY-GROUP-TENANT-001](../manual-tests/MT-MEMORY-GROUP-TENANT-001.md) — the gate; [MT-MEMORY-MULTIUSER-001](../manual-tests/MT-MEMORY-MULTIUSER-001.md) — the per-turn boundary it bounds.
- [docs/memory-scope-axes.md](../memory-scope-axes.md) — the three axes and what each isolates.
