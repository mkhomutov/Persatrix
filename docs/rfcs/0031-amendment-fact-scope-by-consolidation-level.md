# RFC 0031 Amendment — Fact Scope Follows Consolidation Level, Not Subject

**Type**: amendment to [RFC 0031](0031-per-session-namespacing-channels.md) §C (Storage Model) + §D (Recall Semantics), and to [RFC 0026](0026-declarative-facts-tier.md) (facts-tier scope boundary)
**Status**: ✅ Implemented — **LIVE**, v0.3.12 ([RFC 0049](0049-memory-consolidation-gradient.md) Phase 1: shadow in PR 2, promoted by PR 4's green measurement verdict — see [Promotion](#promotion-v0312-pr-4--the-measurement-gated-flip); [0049-pr-plan.md](0049-pr-plan.md)). Decision ratified ([RFC 0049 §D](0049-memory-consolidation-gradient.md#d-reconciliation-with-memory-scope-axesmd-and-the-one-decision-reopened), 2026-06-06); stub expanded to the implementation amendment 2026-07-27; promoted 2026-07-28.
**Author**: Maksim Khomutov
**Date**: 2026-06-06 (stub) / 2026-07-27 (implementation)
**Target**: v0.3.12 — lands with [RFC 0049](0049-memory-consolidation-gradient.md) Phase 1 (this amendment *is* the Phase-1 L2 widening), **behind the RFC 0037 keystone** (see [Sequencing](#sequencing--dependencies)). *(Retargeted from v0.4.0 2026-07-20, following the 2026-07-15 pull-forward of RFC 0049 Phases 0–1; its capture-half companion is the [RFC 0026 topic-predicate amendment](0026-amendment-topic-subject-predicates.md).)*
**Authoritative model**: [RFC 0049 — Memory Consolidation Gradient & Scope Reconciliation](0049-memory-consolidation-gradient.md)
**Supersedes**: [memory-scope-axes.md](../memory-scope-axes.md) decision 4 ("fact scope follows subject"). Re-roots [ISSUE-0084](../issues/ISSUE-0084-fact-scope-by-subject-not-uniform-session.md).

---

## Context

[memory-scope-axes.md](../memory-scope-axes.md) decision 4 scoped declarative facts by their **subject**: a fact *about a person* is person-scoped (cross-room); a fact *about a topic/room* stays room-scoped. [ISSUE-0084](../issues/ISSUE-0084-fact-scope-by-subject-not-uniform-session.md) tracked the implementation of that subject classifier.

[RFC 0049](0049-memory-consolidation-gradient.md) re-roots this: scope follows a fact's **consolidation level**, not its subject. A consolidated, decontextualised fact (gradient level **L2 — semantic**) is **cross-room regardless of subject** — topic facts included. The reason topic facts were walled was *leakage* (a fact from a private room surfacing in a public one), and leakage is now [RFC 0037](0037-memory-confidentiality-channel-classification.md)'s job — a deterministic classification gate at *egress*, not a recall wall.

This is what makes a persona carry project knowledge like a colleague: "Atlas ships Friday" learned in a DM is recallable in the standup (the [scenario-2 failure](0049-memory-consolidation-gradient.md#worked-example-the-two-test-scenarios) RFC 0049 traces).

**What the wall concretely is.** The facts tier has never carried a *channel* filter (`source_channel_id` is provenance, not a predicate — the [0026 amendment](0026-amendment-topic-subject-predicates.md)'s corrected claim); the room wall is the RFC 0031 §D **session** filter. Channel dispatch resolves the `persatrix-session` id per `(agent, channel)` (`internal/channels/session_binding.go` — room continuity), the interaction freezes it at open, and the close-consolidation extractor stamps every fact row with it — so default recall's `session_id IN (active, legacy)` clause *is* the room wall this amendment drops.

## Decision

1. **An L2 (semantic) fact is cross-room.** The RFC 0026 facts tier's default recall stops applying the `session_id` room filter to consolidated facts; the relationship/identity tier's existing cross-room-by-construction shape ([RFC 0031 identity amendment](0031-amendment-person-identity-cross-room-tier.md)) is the precedent, generalised from "person identity" to "any L2 fact".
2. **Subject classification is dropped.** No person-vs-topic classifier is built (ISSUE-0084's original mechanism is *not* implemented). Scope is intrinsic to the tier/rung, never a query-time discriminator — the same cure the identity amendment applied to the F-7 seam.
3. **Visibility is the RFC 0037 protection level**, inherited from the fact's source channel(s) at consolidation time. Cross-room never means cross-classification: a fact distilled from a `restricted` channel cannot be recalled into a lower-classified channel's prompt.
4. **Cross-room is still never cross-`epoch` or cross-`principal`.** Those PK axes are unchanged (memory-scope-axes.md decisions 5–6 stand).

## Implementation (v0.3.12 PR 2 — the SHADOW slice)

The widening ships **shadow-first** (the [measurement gate](#sequencing--dependencies)): each sender-bearing turn computes what the widened recall *would* have injected and records it, while the live prompt keeps the room-scoped recall byte-for-byte.

- **The shadow pass** — new [`agents/persona_runtime/facts_shadow.py`](../../agents/persona_runtime/facts_shadow.py), invoked from `_inject_memory_context` beside the live facts recall. It re-derives the live seed set (person seeds: `self` + canonical sender, every predicate class; topic seeds: stimulus-matched, `TOPIC_PREDICATES`-scoped — the PR 1 subject-reachability bound holds unweakened) with **both** the topic-subject enumeration (`FactStore.topic_subjects(sessions="*")`) and the per-seed recall (`FactStore.recall(sessions="*")`) widened past the session wall. The **cross-room delta** — widened rows whose `fact_id` the live recall did not return — passes through a dedicated RFC 0037 §D `TurnInjectionGate` at the turn's acting classification; gate-admitted rows become the trace's `candidates`, the rest its withhold counts, split by cause (`withheld` above-rank vs `unknown_label` rule-(c)). Per-seed recall failures log-and-continue, the live path's idiom — one bad seed must not blank a turn's trace and skew the measurement against the live prompt's partial recall.
- **The trace** — one structured INFO record per turn with a non-empty delta, on the `agents.persona_runtime.facts_shadow` logger: `tier: "facts"` (added in PR 3, which merges the L1 episodic and L2 facts shadow streams into one harness capture — the key is how the PR 4 measurement partitions them), `agent_id`, `acting`, per-candidate `{fact_id, subject, predicate, protection_level, session_id, source_channel_id}` (provenance for the PR 3/4 ranking work), and the split withhold counts — `withheld` (clean above-rank) and `unknown_label` (rule (c): the stored label failed to parse), separated so the PR 4 measurement can tell "gate working" from "labels corrupt". Quiet turns — empty delta, sender-less events, `off` mode — emit nothing, so single-room deployments see zero log volume. The pass shares `_inject_memory_context`'s never-fail contract (any failure degrades to a WARNING).
- **The knob** — `memory.facts.cross_room: off | shadow` (schema-gated enum, default `shadow`; resolved at agent construction). Shadow-first is the shipped v0.3.12 posture, so traces accumulate everywhere for the PR 4 verdict; `"live"` is rejected at both the schema and the resolver until PR 4 lands the promotion.
- **Harness recording** — the RFC 0044 driver (`evaluators/persona_driver.py::capture_shadow_traces`) captures the run's shadow records into `EvalRun.shadow_traces`, and the runner threads them into the report artifact (a `shadow_traces` key, present only when non-empty). That artifact is the PR 4 measurement's read surface; landed single-room goldens replay byte-identically (the shadow pass never shifts a request hash).

## Promotion (v0.3.12 PR 4 — the measurement-gated flip)

The shadow verdict ran **green** and the widening is LIVE: `memory.facts.cross_room: live` is the shipped default (schema + resolver; `shadow` and `off` remain configurable — `shadow` is the documented rollback lever, still trace-emitting).

- **The verdict.** `evaluators/shadow_measurement.py` partitions the report artifact's `tier`-keyed `shadow_traces` and renders three criteria, each able to go red: `label_integrity` (zero rule-(c) `unknown_label` withholds — clean above-rank `withheld` counts are the gate *working* and do not fail promotion), `bounded_volume` (no turn's gate-admitted delta exceeds the tier recall limits), and `continuity` (the full golden suite replays green — the dementia bar EVAL-MEMORY-001 byte-identical, un-re-recorded, under the live default). Measured over the new cross-room seed **EVAL-MEMORY-002** (the scenario-2 arc, shadow-pinned via the recipe's `setup.memory` override): 2 gate-admitted cross-room candidates (one topic-seeded, one person-seeded), 0 withheld, 0 unknown-label, goldens 4/4. The run is committed and reproducible — `tests/integration/test_cross_room_seed_replay.py` re-executes replay → traces → verdict on every CI run, with the tier bounds taken from the live runtime constants.
- **The live read.** `recall_facts_for_event` gained a `sessions=` passthrough (topic enumeration + every per-seed recall); `memory_context` passes `"*"` in live mode. ONE widened read per turn — the shadow pass does not run in live mode (the #783 no-doubled-read follow-up) — and the §D gate + RFC 0017 budget apply to it exactly as they did to the walled read. Topic seeds stay `TOPIC_PREDICATES`-scoped at every width (the PR 1 reachability bound).
- **The integration eval.** **EVAL-MEMORY-003** pins the promoted posture: the room-B request hash carries the DM-taught facts, and a shadow-pinned replay of its golden *must miss the cassette* — the strip test that makes the seed load-bearing at the request level, not the (mock-authored) transcript level.

## Security considerations

- **Gate before trace.** Every cross-room candidate passes the §D gate *before* it is recorded — a `restricted`-stamped fact on a turn acting below `restricted` appears only as a withheld count, exactly the live gate's posture (Decision 3).
- **Log-egress bound.** The trace never carries the fact **object** text — the process log is its own egress surface, and dumping restricted objects into it would undo at the log what the §D gate enforces at the prompt. The PR 4 measurement joins `fact_id` back against the store when it needs content.
- **The `sessions="*"` F-3 pin.** RFC 0031 §Security pins the persona-runtime *prompt-context* path never reaching `"*"`. The shadow module is a documented carve-out, not a weakening: it reads all-sessions but feeds nothing to WorkingMemory, the RFC 0017 budget, the §G manifest, or the reinforcement write — its sole output is the log record. The no-prompt-leak property is pinned end-to-end (`test_facts_shadow.py::TestShadowNeverEntersPrompt`); the source-scan pin (`test_session_recall_default_path.py`) documents the exception.
- **Absolute walls unchanged.** `epoch` and `principal` remain strict-equality SQL clauses on every branch of the widened read (Decision 4, pinned by test).

## What changes

- **RFC 0031 §D (Recall Semantics)** — the blanket room-scoping default no longer applies to the L2 facts tier. Episodes (L1) are out of this amendment's scope; their room posture is owned by the [RFC 0049 L1 amendment](0049-amendment-l1-cross-room-availability.md) (cross-room *available* behind the 0037 gate, room-first-ranked — *updated 2026-07-20; originally "room-scoped default recall unchanged"*).
- **RFC 0031 §C (Storage Model) / RFC 0026** — facts carry provenance (source session(s)) as a tag, not a filter, plus an RFC 0037 protection level. *(Already landed: `session_id` has been a row tag since RFC 0031 Phase 2; `protection_level` + `source_channel_id` landed in the RFC 0037 PR 3 v16 migration.)*
- **memory-scope-axes.md** — decision 4 already annotated superseded (PR #559).

## Sequencing / dependencies

- **Hard dependency: RFC 0037 lands first — SATISFIED.** The §D gate + §F filter merged in [RFC 0037 PR 5](0037-pr-plan.md) (#778, 2026-07-26) before this widening; the capture-half companion ([0026 amendment](0026-amendment-topic-subject-predicates.md)) merged as RFC 0049 PR 1 (#781).
- **Measurement gate — SATISFIED (green, 2026-07-28).** Cross-room L2 recall shipped in *shadow* (PR 2) and promoted to the live prompt on the green [RFC 0044](0044-eval-set-golden-traces.md) golden-trace verdict under the [RFC 0017](0017-persona-memory-injection-budget.md) injection budget — see [Promotion](#promotion-v0312-pr-4--the-measurement-gated-flip).
- Pairs with the [RFC 0027 cross-scope consolidation amendment](0027-amendment-cross-scope-consolidation.md) (the pump that *produces* cross-room L2 facts — v0.4.0; until it lands, cross-room candidates are facts a persona itself extracted in another room).

## Non-goals

- Unifying **episodic** (L1) recall *here* — the L1 axis is owned by the [RFC 0049 L1 amendment](0049-amendment-l1-cross-room-availability.md); this amendment touches only the L2 facts tier. *(Reworded 2026-07-20 — originally "episodes stay room-scoped", which the L1 amendment reverses.)*
- Removing the capture-time "is this worth consolidating?" judgment.
- Cross-room **ranking** (same-room boost / provenance-aware ordering) — the shadow trace carries the provenance for it, but ranking is the PR 3 (L1) / PR 4 (promotion) surface.
- ~~The live-prompt flip itself — PR 4, behind the measurement gate.~~ *(Landed — see [Promotion](#promotion-v0312-pr-4--the-measurement-gated-flip).)*

## Related documentation

- [RFC 0049 — Memory Consolidation Gradient](0049-memory-consolidation-gradient.md) — the authoritative model
- [RFC 0037 — Memory Confidentiality & Channel Classification](0037-memory-confidentiality-channel-classification.md) — the egress keystone
- [RFC 0031 identity amendment](0031-amendment-person-identity-cross-room-tier.md) — the cross-room-by-tier precedent
- [RFC 0026 — Declarative Facts Tier](0026-declarative-facts-tier.md) — the tier amended
- [RFC 0026 topic-predicate amendment](0026-amendment-topic-subject-predicates.md) — the capture half (RFC 0049 PR 1)
- [RFC 0044 — Eval Set & Golden Traces](0044-eval-set-golden-traces.md) — the shadow-trace recorder + the promotion gate
- [ISSUE-0084](../issues/ISSUE-0084-fact-scope-by-subject-not-uniform-session.md) — re-rooted by this amendment
- [memory-scope-axes.md](../memory-scope-axes.md) — decision 4, superseded
