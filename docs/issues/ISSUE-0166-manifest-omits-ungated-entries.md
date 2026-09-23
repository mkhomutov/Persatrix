---
id: ISSUE-0166
summary: "The RFC 0037 §G per-turn injection manifest lists only the entries the §D gate itself judged. `TurnInjectionGate.manifest()` walks the RFC 0017 budget's admitted ids but keeps an entry only when the gate holds a record of passing it, and only `admit()` and `record_projection()` write that record. So if a tier's candidates ever reach the budget without going through `gate.filter_entries(...)`, they reach the prompt and the manifest says nothing about them — while `MemoryInjectionResult.manifest` and `InjectionManifestEntry` both call themselves the record of what reached the prompt. Confirmed by bypassing the episodic gate call: a `restricted` episode was rendered into the prompt on an `internal`-acting turn and the manifest held no entry for it. A missed injection path is one of the three defects §G names the tripwire as the detection path for, and this is the audit record's response to it: silence. Nothing in production reads the manifest today, so this is an audit-record gap, not a live leak. Suggested fix: have `manifest()` emit a budget-admitted entry that carries no gate record under an explicit `ungated` label instead of dropping it."
status: open
severity: low
area: memory
created: 2026-09-23
refs:
  - https://github.com/mkhomutov/Persatrix/pull/976
  - agents/persona_runtime/injection_gate.py
  - agents/persona_runtime/memory_context.py
  - agents/persona_runtime/memory_budget.py
  - tests/unit/python/test_cross_room_live.py
  - docs/rfcs/0037-memory-confidentiality-channel-classification.md
  - docs/rfcs/0037-pr-plan.md
  - docs/rfcs/0037-amendment-audience-egress.md
---

# ISSUE-0166: The injection manifest drops an entry that reached the prompt without passing the gate

## Summary

Every turn a persona agent takes, the runtime writes down which remembered
entries it put into the prompt and how protected each one was. That list is
the [RFC 0037](../rfcs/0037-memory-confidentiality-channel-classification.md)
§G **injection manifest**, and both places that describe it call it "the
record of what reached the prompt".

It is not quite that. The manifest is built from two separate registries and
keeps only the rows that appear in both: the budget's list of what it
admitted into the prompt, and the gate's list of what it personally let
through. An entry in the first but not the second is dropped from the
manifest without a word. That is exactly the shape a bypassed gate has — an
entry reaches the prompt, the gate never saw it — so the one case where the
record matters most is the case it stays quiet about.

## Context

**Where.** [`TurnInjectionGate.manifest()`](../../agents/persona_runtime/injection_gate.py)
(lines 429–449) loops over the four gated memory tiers and, for each id the
budget admitted, looks up the level the gate recorded:

```python
for tier in _MANIFEST_TIERS:
    for entry_id in budget.admissions_by_tier(tier):
        level = self._passed_levels.get((tier, entry_id))
        if level is not None:
            entries.append(InjectionManifestEntry(...))
```

`_passed_levels` is written in exactly two places, both inside the gate:
`admit()`, on an entry it passes, and `record_projection()`, for a §E
projection served in place of a withheld entry. Nothing else can fill it.

**Why the two registries can drift.** `budget.admissions_by_tier(tier)` is
the RFC 0026 MQ-11 admission registry, filled by the allocate-loop when a
tier's renderer actually charges tokens for a row
([`memory_budget.py`](../../agents/persona_runtime/memory_budget.py)). The
gate's record is filled earlier, and by a different call. One code path keeps
them in step: [`_inject_memory_context`](../../agents/persona_runtime/memory_context.py)
calls `gate.filter_entries(...)` once per gated tier (lines 433–436) before
it builds the budget. That pairing is a convention, not a rule the gate can
enforce — `manifest()` has no way to tell "the gate withheld this" from "the
gate was never asked".

**Confirmed** at `68336102`, by removing one of those four calls —
`episodes = gate.filter_entries("episodic", episodes)` — and running the
real prompt path from `tests/unit/python/test_cross_room_live.py`'s harness
on an `internal`-acting turn with a `restricted` cross-room episode in the
store:

| | Episode in the rendered prompt | Episode in the manifest |
|---|---|---|
| Gate call in place | no | no |
| Gate call removed | **yes** | **no** |

The episode reached the prompt and the manifest listed only the one admitted
fact. The production file was restored afterwards; nothing in this issue's
branch changes it.

**What the existing tests catch, and what they do not.** The manifest
assertions in `test_gate_withholds_restricted_on_internal_turn` check that
the withheld ids are *absent*, so they pass whether the gate withheld the
entry or never ran. Under the bypass that test still failed, but on the
gate-decision fixture [#976](https://github.com/mkhomutov/Persatrix/pull/976)
added (`assert _judged(gates, "episodic").get(secret_ep) is False`, line 265)
and on the rendered-prompt assertion — not on the manifest. So the bypass is
caught today, by the prompt and by the gate's own decision record. The
manifest is the one instrument that stays silent.

**What it contradicts.** Both docstrings describe the manifest as a complete
record: `InjectionManifestEntry` is "One injected (budget-admitted) memory
entry — the record of what reached the prompt", and
`MemoryInjectionResult.manifest` repeats the phrase. §G itself names the
three defects its detection path exists for: an entry stamped with the wrong
protection level, a projection that copied source text verbatim, **or a
missed injection path**. The third is this one, and the manifest answers it
with an absence indistinguishable from a clean turn.

**One absence that is deliberate and must stay.** The relationship tier
records budget admissions (since [ISSUE-0122](ISSUE-0122-relationship-tier-emits-no-provenance.md),
v0.3.16 PR B1) but is not §D-gated at all, so it is left out of
`_MANIFEST_TIERS` on purpose and the docstring says so. A fix must not start
emitting it: the loop already walks only the four gated tiers, which keeps
that absence intact.

## Impact

- **No leak, and no live detection is weakened.** The manifest gates nothing
  and blocks nothing. The §D gate still withholds; §G's shipped tripwire
  reads the *withheld* complement, not the manifest.
- **Nothing in production reads the manifest today.** It is built in
  `_inject_memory_context`, returned on `MemoryInjectionResult`, and read
  only by tests. So the cost is to a future reader — the operator or agent
  who opens the record to answer "what did this turn's prompt carry?" and
  gets an answer that is silently short.
- **The silence points the wrong way for an audit record.** A record that
  drops what it cannot explain reads as a clean turn. Reporting the unknown
  is the conservative direction, and it is the direction the rest of RFC 0037
  already takes: rule (c) withholds an entry whose label will not parse and
  logs it at WARNING rather than assuming a level, and the tripwire watch
  carries such an entry at the sentinel level `unknown`.
- **Pre-existing, and not caused by any recent change.** The `if level is
  not None` filter has been the manifest's shape since RFC 0037 PR 4
  (v0.3.12). It was noticed while reviewing #976, which touched tests only.

## Proposed fix / investigation path

Make the gap visible rather than silent. Two options, in the order they are
worth trying:

1. **Emit the entry under an explicit label.** Drop the `if level is not
   None` filter and give a gate-record-less admission a sentinel
   `protection_level` — `ungated` reads plainly and collides with nothing:
   the §A vocabulary is `public` / `internal` / `restricted` / `secret`, and
   `unknown` is already taken by the tripwire watch for the rule-(c) case,
   which is a different story (a label that would not parse, not a gate that
   never ran). An audit reader then sees the bypass in the record itself.
   Keep the loop over `_MANIFEST_TIERS` so the relationship tier stays out.
2. **Log it, or assert none exists.** Cheaper, and it leaves the manifest's
   type alone, but it puts the evidence somewhere other than the record that
   claims to be complete. Worth it only if option 1 turns out to break a
   consumer — and today there is no production consumer to break.

Either way the two docstrings should say what the manifest does with an
admission the gate never judged, instead of describing a completeness it does
not have.

## Slot

Not slotted. No release plan is open: ruling (a) of the
[sequencing Amendment 2026-09-12](../v0.3.x-sequencing.md#amendment-2026-09-12--close-v0316-small-then-measure-before-any-train-opens)
opens none before EXP-001 reports and the first strategy review logs its
result.

The fix is observability-only. It changes no gate decision, no recall, no
budget, and no prompt bytes, and the manifest feeds nothing an
[EXP-001](../experiments/EXP-001-preregistration.md) arm measures — so it
does not touch a frozen arm, unlike [ISSUE-0163](ISSUE-0163-withheld-episodes-reinforced-before-the-gate.md)'s
held fix.

Ruling (b) of the same amendment is the neighbourhood, not the hold: it keeps
the RFC 0037 §D gate as built, and this fix leaves the gate exactly there —
it changes only what the turn's record says about an admission the gate never
judged. So what holds it is the absence of an open plan.

## Notes

> 2026-09-23 — filed from the review of
> [#976](https://github.com/mkhomutov/Persatrix/pull/976) as finding F-6,
> outside that PR's tests-only scope. The bypass above was run at
> `68336102` and reverted; no production code lands with this issue.
