# ISSUE-0082 — PR Implementation Plan (Residuals R-1 / R-2 — the derived and relayed tenant writes)

**Issues**: [ISSUE-0123](ISSUE-0123-per-speaker-interaction-scope.md) (R-1) · [ISSUE-0124](ISSUE-0124-orchestrator-hop-drops-tenant-on-agent-cascade.md) (R-2)
**Status**: 📋 Draft — **opens after the v0.3.14 tag**, as a workstream inside the v0.4.0 master plan
**Created**: 2026-08-07
**Branch prefix**: `feature/v040-issue0123-` / `feature/v040-issue0124-` (per residual)
**Target**: `main`
**Merge strategy**: Squash merge per [BRANCHING.md](../BRANCHING.md)
**Spawned from**: [ISSUE-0082](ISSUE-0082-orchestrator-per-request-session-principal-emission.md) Part 2 review ([#820](https://github.com/mkhomutov/Persatrix/pull/820))
**RFC**: [RFC 0020 §G](../rfcs/0020-interaction-lifecycle.md#g-per-channel-scoping) — R-1 amends the per-channel scoping table

---

## Overview

v0.3.14 made the caller's verified principal a per-request axis: the orchestrator emits `persatrix-principal` at the `Dispatch` chokepoint and the persona binds a strict-equality `principal_scope` for the handler's lifetime. **That boundary holds for every turn's own write.** It does not hold in two places, both surfaced at the Part 2 review and both deferred here:

* **R-1 — the *derived* write.** The RFC 0020 interaction scope is the ROOM, not the speaker, so a group channel accumulates every speaker's turns into one `InteractionTracker` record. At close, that record is summarised and RFC 0026 facts are extracted from it under whichever principal closed the interaction.
* **R-2 — the *relayed* write.** A persona's reply re-enters through `HTTPChannelPublisher` as a fresh unauthenticated REST publish, so every fanout below it writes `'local'` even inside an authenticated person's interaction.

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

## Phase 0 — the design gate (no PR)

**R-1's shape is deliberately NOT locked by this plan.** [ISSUE-0123](ISSUE-0123-per-speaker-interaction-scope.md) proposes per-speaker records; that is one of three answers, and the choice turns on evidence nobody has yet.

**Action**: run [MT-MEMORY-GROUP-TENANT-001](../manual-tests/MT-MEMORY-GROUP-TENANT-001.md) Legs 1–4 against the v0.3.14 tag and read *where the leak concentrates*.

| Option | Shape | Cost | Residual |
|---|---|---|---|
| **A — per-speaker records** | Tracker keyed `(principal, scope)`; N records, N summaries, N extractions | `1 + (personas × principals)` close summaries | Persona's memory of a group discussion becomes N partial views |
| **B — room record, per-principal extraction** | One room `Interaction` and one summary; RFC 0026 extraction runs once per principal over that principal's turn subset | One summary + N extractions | The episode summary still narrates everyone under one principal |
| **C — B + episode to `'local'`** | As B, with the group episode written to the shared tenant | Same as B | A person's own group-room episode summaries are unreachable from their authenticated turns |

**Decision rule.** Read the Leg 4 `turn_count > 1` episode summary alongside the extracted facts:

* If the **summary text** materially carries another speaker's disclosure — not just "the team discussed scheduling" — the episode is a real leak vector and **Option A** is required.
* If the leak is concentrated in `facts` while the summary stays generic, **Option B** closes the actual cross-room vector (facts are cross-room by default per [RFC 0049](../rfcs/0049-memory-consolidation-gradient.md) Phase 1) at a fraction of the cost, and the episode residual is stated rather than paid for.
* **Option C** only if B's episode residual is judged unacceptable *and* the recall loss is judged cheaper than A's cost.

Do **not** attribute facts to speakers by asking the model. Per-turn membership is structural; LLM-elected attribution is not a boundary, and this repo does not ship model-elected boundaries.

**Output**: a dated scope-lock note appended to ISSUE-0123, naming the option and the evidence. PRs 3–4 below are written for Option A and are re-scoped if the gate selects B or C.

---

## Dependency Graph

```
Phase 0 gate (MT Legs 1–4) ──────────────┐
                                          │ (locks R-1 shape only)
PR 1 (R-2 store, dormant)                 │
   └─→ PR 2 (R-2 re-stamp + gate)         │
          └─→ PR 3 (R-1 scope key) ←──────┘
                 └─→ PR 4 (R-1 close path + reserve)
                        └─→ PR 5 (live MT + closeout)
```

PR 1 is unblocked by the gate and can start immediately after the tag. PR 3 must not start before the gate resolves.

---

## PR Sequence

### PR 1: `feature/v040-issue0124-attribution-store` — Causal attribution, dormant

#### Scope

`internal/channels/principal_attribution.go`: a per-`(channel, agent)` table recording the principal a dispatch was made under. Written from `Dispatch` when `PrincipalFromContext(ctx)` is non-empty. **No read site** — nothing re-stamps yet, so behaviour is unchanged everywhere. Mirrors the v0.3.14 PR 1 / PR 2 dormant-rail-then-producer split.

#### Key implementation details

* Key `(msg.ChannelID, env.Recipient.ParticipantID)`, value `{principal, dispatchedAt}`.
* **Ambiguity**: a second dispatch under a *different* principal marks the entry ambiguous. Same-principal re-dispatch refreshes rather than ambiguates.
* **TTL** sized on the persona's worst realistic turn — the same budget `defaultSynthesisReplyTimeout` (120s) is sized against.
* In-memory only, lazy expiry on read plus a periodic sweep. Bound is `channels × members`.

#### Tests

Two principals → ambiguous; same principal → refreshed, not ambiguous; TTL expiry → miss; empty ctx principal → no write. Dormancy pinned, not assumed: a dispatch with the table populated emits the same header bytes as one without.

#### PR checklist

- [ ] `cargo test` / `go test ./...` green (note: CI runs `go test` since v0.3.13 #813)
- [ ] No behaviour delta — the table has no reader

---

### PR 2: `feature/v040-issue0124-restamp` — The re-stamp, and the gate

#### Scope

Read the table in `ChannelRouter.Publish`, before fanout, **iff** the ctx carries no principal and the sender is a registered agent. On a live unambiguous hit, `ctx = WithPrincipal(ctx, p)`. This is where R-2's behaviour changes.

#### Key implementation details

* Read in `Publish`, not `handlePublishMessage`, so in-process callers are covered.
* `msg.SenderID` is safe as a key: the executor supplies the agent's framework-known registered id and never forwards an LLM-supplied value (the RFC 0011 §"DM gate-bypass" invariant).
* **Never** accept a principal, or any correlation key, from the agent's request — including the stimulus message id. An agent sees other members' ids in channel history, so echoing a chosen id resolves to a chosen principal: the same cross-tenant read primitive one indirection along.
* Deeper cascades inherit transitively (PR 1's write fires again with the principal now on ctx), bounded by `cascade_depth`.

#### Tests

Integration: an authenticated publish → persona reply → the second-hop persona's rows carry the human's principal, not `'local'`. Negative: autonomous/tick publishes stay `'local'`; ambiguous and expired entries degrade to `'local'`; `auth.mode: disabled` is byte-identical. A route-table-style pin that `Publish` is the **only** re-stamp site.

#### PR checklist

- [ ] Accepted-risk statement in the PR body: this grants an agent a bounded **write** into the causally-implicated principal's tenant — never a read
- [ ] Known Gap: single-orchestrator only

---

### PR 3: `feature/v040-issue0123-scope-key` — Per-speaker scope (Option A)

> Re-scoped if Phase 0 selects Option B or C.

#### Scope

RFC 0020 §G amendment (the per-channel scoping table gains the principal dimension) + `Interaction.principal_id` frozen at open + `InteractionTracker._open` keyed `(principal, scope)` + room-wide close fan.

#### Key implementation details

* `principal_id: str = DEFAULT_PRINCIPAL_ID` on `Interaction`, resolved from the ambient scope at open, never re-read — the footing `session_id` already sits on.
* **Tuple key, not an encoded scope string.** `scope` is persisted to `episodes.scope`, prefix-matched by `is_group_scope` / `is_thread_scope`, and is the `idx_episodes_scope` surface; principal has its own column.
* **Room-wide close is inseparable from the keying** and must land in this PR: a structural close, an end-vote quorum or the close-notification turn is a *room* event, and without the fan a room close closes one principal's record and leaks the rest open until idle.

#### Tests

One tracker, two principals, one room scope → two records. A room-wide close closes both. The close-notification turn lands as the final turn of each.

#### PR checklist

- [ ] RFC 0020 §G amendment merged in the same PR
- [ ] `open_scopes()` and every "one scope, one record" caller audited

---

### PR 4: `feature/v040-issue0123-close-path` — The close binding and its consequences

#### Scope

Bind `principal_scope(interaction.principal_id)` around the close pipeline (`summarize_closed_interaction` → `update_episode_summary` → `store_extracted_facts` → `record_closed_interaction`), re-size the wallet reserve, and clean up the Go-side asymmetry.

#### Key implementation details

* **This is the part that holds on request-less paths.** `idle_check` runs from the janitor with no scope active; the close-notification path runs under the closing turn's principal. Both must write under the record's own frozen value.
* **The asymmetry is retired, not resolved.** Once the record names its principal, the trigger's principal no longer selects a tenant, so the four close paths in `internal/channels/synthesis_close.go` need not agree. Audit whether `pendingSynthesisClose.principal` becomes dead and drop it if so.
* **Reserve re-size**: the RFC 0052 PR 4a `1 + N` close-path reserve assumes one summary per persona. Under-sizing turns extra summaries into `budget_denied` → `SUMMARY_UNAVAILABLE_TEXT`, and the janitor never retries a committed unavailable row — a *silent* quality regression. File the calibration as its own issue in the [ISSUE-0109](ISSUE-0109-rfc0052-autonomous-defaults-calibration.md) idiom rather than guessing a constant.
* `agent.interactions.closed.by_<reason>` now fires once per record: a metric-shape change dashboards must be told about.

#### PR checklist

- [ ] Metric-shape change called out in the changelog
- [ ] Release note states the coherence trade: a persona's close-derived memory of a group discussion is per-person; room continuity is unaffected (transcript and RFC 0036 verbatim history are not principal-scoped)

---

### PR 5: `feature/v040-issue0082-residuals-close` — Live verification + closeout

#### Scope

Run [MT-MEMORY-GROUP-TENANT-001](../manual-tests/MT-MEMORY-GROUP-TENANT-001.md) end to end on the post-fix column, execution report, issue closures (ISSUE-0123, ISSUE-0124, and ISSUE-0082 itself), and the MT-MEMORY-MULTIUSER-001 Edge Case 2 wording widened back to the now-true claim.

#### PR checklist

- [ ] All eight legs green on a live provider, with the post-fix column stated
- [ ] Leg 2 row counts and Leg 4 `(principal_id, summary)` pairs pasted verbatim
- [ ] ISSUE-0082 closed — its Part 2 residuals note updated, and the file split or allowlisted (it sits at 2962/3000 words; the next note breaks the doc cap)

---

## Risk and Mitigations

| Risk | Mitigation |
|------|------------|
| Phase 0 selects Option A and the cost multiplier bites a large roster. | The reserve re-size is in PR 4's scope, not a follow-up, and its calibration is a tracked issue. A room with one authenticated speaker is unchanged — the multiplier is in principals, not personas. |
| R-2's attribution mis-fires under load and stamps the wrong tenant. | Every ambiguity and every expiry fails closed to `'local'` — today's behaviour. A wrong attribution requires two dispatches under the *same* principal, which is not ambiguous because it is not wrong. |
| The re-stamp is read as "agents can now claim identities". | It is server-held state keyed on facts the orchestrator knows; nothing is accepted from the request. Stated in the PR body and pinned by the only-one-re-stamp-site test. |
| R-1 lands without R-2 (or vice versa) because one slips. | They are one release, not one PR. If one slips, **both** hold — the release note claim is unsafe with either half missing. |
| The RFC 0020 §G amendment re-opens the scoping design. | The amendment records the decision Phase 0 made on evidence; it does not re-derive it. |

---

## Progress Overview

| # | Title | Branch | Status | GitHub PR | Merged |
|---|-------|--------|--------|-----------|--------|
| 0 | Design gate — MT Legs 1–4, lock R-1's shape | — | ⬜ Not started | — | — |
| 1 | R-2 causal attribution store, dormant | `feature/v040-issue0124-attribution-store` | ⬜ Not started | — | — |
| 2 | R-2 re-stamp + end-to-end gate | `feature/v040-issue0124-restamp` | ⬜ Not started | — | — |
| 3 | R-1 per-speaker scope + RFC 0020 §G amendment | `feature/v040-issue0123-scope-key` | ⬜ Not started | — | — |
| 4 | R-1 close binding, reserve re-size, asymmetry cleanup | `feature/v040-issue0123-close-path` | ⬜ Not started | — | — |
| 5 | Live MT + closeout | `feature/v040-issue0082-residuals-close` | ⬜ Not started | — | — |

**Status legend**: ⬜ Not started · 🔄 In progress · 🔀 PR open · ✅ Merged · ⏭ Deferred

---

## Related Documentation

- [ISSUE-0082](ISSUE-0082-orchestrator-per-request-session-principal-emission.md) — the parent; its Part 2 note states both residuals.
- [ISSUE-0082 Part 1 PR plan](ISSUE-0082-part1-session-emission-pr-plan.md) — the session axis, the shape this plan mirrors.
- [RFC 0020 §G](../rfcs/0020-interaction-lifecycle.md#g-per-channel-scoping) · [RFC 0049](../rfcs/0049-memory-consolidation-gradient.md) Phase 1 · [RFC 0011](../rfcs/0011-channels-bridges.md) · [RFC 0039 §F](../rfcs/0039-user-accounts-authentication.md)
- [MT-MEMORY-GROUP-TENANT-001](../manual-tests/MT-MEMORY-GROUP-TENANT-001.md) — the gate; [MT-MEMORY-MULTIUSER-001](../manual-tests/MT-MEMORY-MULTIUSER-001.md) — the per-turn boundary it bounds.
- [docs/memory-scope-axes.md](../memory-scope-axes.md) — the three axes and what each isolates.
