# RFC 0037 Amendment — Audience as an AND-Condition on the §D Egress Gate

**Type**: amendment to [RFC 0037](0037-memory-confidentiality-channel-classification.md) §D (the gate rule), §E (composition with declassification projections) and §G (the withhold-cause vocabulary the tripwire and the manifest read)
**Status**: 🔄 **Shadow** — v0.3.16 PR A2 ([ISSUE-0132](../issues/ISSUE-0132-memory-egress-gate-blind-to-room-audience.md); [v0.3.16 PR plan](../v0.3.16-pr-plan.md)). The check records and does not withhold; the flip to `live` is [verdict-gated](#promotion-the-verdict-gated-flip), its own PR, and lands only on a green measurement
**Author**: Maksim Khomutov
**Date**: 2026-09-09
**Target**: v0.3.16 — *The persona knows who is listening*
**Authoritative model**: [RFC 0037 — Memory Confidentiality & Channel Classification](0037-memory-confidentiality-channel-classification.md)
**Separate file because**: RFC 0037 sits at 7 973 of its 8 000-word cap. The [RFC 0049 L1 amendment](0049-amendment-l1-cross-room-availability.md) is the precedent (v0.3.16 [scope lock 3](../v0.3.16-scope-locks.md)).

---

## Context

RFC 0037 §D asks exactly one question of every candidate memory entry:
is its `protection_level` at or below the acting channel's
classification? That question is about **how secret** something is. It
is not about **whose** it is.

So consider a persona that Alice tells something in her DM. The DM is
classified `internal`; the fact is stamped `internal`; both are correct.
Later the persona is in a standup room, also `internal`, and Bob is in
it. §D compares `internal ≤ internal`, admits, and the persona may
repeat to Bob what Alice told it privately. Nothing has malfunctioned —
the gate did what it was specified to do. The specification was
incomplete.

RFC 0049 made this reachable rather than theoretical: cross-room recall
is the shipped default since v0.3.12, so a DM-taught fact is a candidate
on every later turn that names its subject. The classification wall was
carrying a load it was never designed for — room isolation — and when
RFC 0049 removed the wall, nothing replaced that half.

## The change

**The §D decision gains a second condition, ANDed with the first: the
acting room must add nobody the entry's source room did not already
hold.**

```
let L = classification of the turn's acting channel        (§B, unchanged)
let A = current member ids of the acting channel
for each candidate memory entry E with protection level P
                                and source channel S:
    if rank(P) > rank(L):        withhold (or project, §E) — unchanged
    else if P == public:         inject                    — exempt
    else if S is NULL:           verdict: unknown-no-provenance
    else if members(S) unknown:  verdict: unknown-fetch-failed
    else if A ⊆ members(S):      verdict: admit      → inject
    else:                        verdict: disjoint   → withhold
```

Five clauses that need stating, because each is a decision and not a
detail:

1. **Audience is the type-agnostic member-id set.** A peer *persona* in
   the acting room that was not in the source room is audience: an
   agent→agent leak is a leak. This is also what keeps the
   authenticated agent directory — which `401`s for the fleet under
   auth ([ISSUE-0140](../issues/ISSUE-0140-agent-fleet-401-on-roster-fetch-under-auth.md))
   — out of the decision entirely. The check reads the public
   channel-members half and never the names.
2. **`public` is exempt.** An entry at `public` is shareable by
   definition, so audience applies to `internal` and above. No new
   "shareable" marking is introduced.
3. **A subset admits.** The condition is `A ⊆ members(S)`, not equality:
   a room *smaller* than the source room contains nobody who did not
   already hear it.
4. **Membership is read at injection time, from both rooms.** An
   entry's audience is the *current* member set of its
   `source_channel_id` — which episodes and facts carry since §C. The
   **notes tier is excluded entirely**, not merely unfetched: a note is
   *authored* during a turn rather than derived from one channel, so its
   source channel is NULL by design, and judging the tier would stamp
   every recalled note *no-provenance* on every turn — a denominator
   that can never move, swamping the delta the shadow exists to
   measure. The [RFC 0035](0035-channel-membership-interval-ledger.md)
   assertion-time snapshot is the refinement, taken only if the verdict
   shows churned rooms matter — the only ledger endpoint is per
   participant and authenticated, so an assertion-time answer needs a
   new endpoint or a §C-time audience column, both wider than this
   change. **No schema change and no store migration land with this
   amendment.**
5. **Unknown is two facts, not one.** A roster call that missed is
   *transient*; a NULL `source_channel_id` — pre-migration rows and
   tick/task-scoped records — is *permanent by design*. They are
   recorded separately because they have different fixes, and because
   collapsing them would leave the measurement unable to say whether
   the unknowns are a bug or the schema.

### §E — an audience withhold is terminal

§D's rule says: when an entry is withheld, serve the best
declassification projection at or below `L`, and withhold entirely only
if none exists. **That composition does not extend to audience.** A
projection lowers an entry's *classification*; it does nothing about who
is in the room. Abstracting "the Helix rollout is paused pending
security review" into "a rollout decision was made" still tells Bob
that Alice raised a rollout decision. So an audience withhold serves
nothing in its place, and §E's selection branch skips those entries
rather than looking them up.

### §G — the withhold vocabulary widens

The §G tripwire watch is the *withheld* set, and the §G manifest is the
*admitted* set. Both are built from the gate's one per-turn decision
record, which is precisely why the audience check is an input to that
gate rather than a filter in front of it: a pre-filtered entry would be
invisible to the watch, the manifest and the shadow trace alike. The
four verdicts — `admit`, `withhold-disjoint`,
`withhold-unknown-fetch-failed`, `withhold-unknown-no-provenance` — are
recorded per entry for every §D-*admitted*, `internal`-and-above
candidate, and ride a structured per-turn log record.

An audience withhold in `live` mode therefore lands on the §G watch like
any other withhold: if the persona paraphrases it to Bob anyway, the
tripwire fires. As §G already says, a hit means a bug — and this
amendment adds one more bug class it can catch.

## Implementation (v0.3.16 PR A2 — the shadow slice)

- **The check** — new [`agents/persona_runtime/audience.py`](../../agents/persona_runtime/audience.py):
  the mode vocabulary, the four verdicts, the per-turn source-room cache
  and the resolution that fills it. The acting room is pre-seeded from
  the roster the turn already resolves (PR A1 moved that resolution
  ahead of the gate for every channel-anchored turn, DMs included), so
  the overwhelmingly common same-room recall costs no round trip; each
  *other* distinct source room among a turn's candidates costs one, and
  is fetched once.
- **The gate** — `TurnInjectionGate` takes the audience as an input and
  applies it **after** the rank comparison, so only §D-admitted entries
  carry a verdict. That is what makes the measured delta answer the
  question it claims to: *the share of gate-admitted entries the
  audience check would withhold*.
- **The knob** — `memory.egress.audience: off | shadow | live`, default
  `shadow`, mirroring `memory.{facts,episodic}.cross_room` down to the
  loud rejection of an unknown value. `shadow` stays the documented
  rollback lever after the flip, as it did for `cross_room`.
- **The trace and the verdict** — [`audience_shadow.py`](../../agents/persona_runtime/audience_shadow.py)
  emits one structured record per turn (ids, levels, room ids, verdicts
  — never entry content: the process log is its own egress surface),
  which the RFC 0044 harness threads into the report artifact and
  [`evaluators/shadow_measurement.py`](../../evaluators/shadow_measurement.py)
  renders as a fourth criterion beside the RFC 0049 three.
- **The offline sample** — `EVAL-MEMORY-005`: Alice teaches in her DM,
  the interaction closes, and she asks twice — once in a room with Bob
  (*disjoint*), once in a room whose every member was in the DM
  (*admit*). The eval driver gains an in-process roster seam for it, the
  way RFC 0044 PR 4c gave it a history fetcher.

## Promotion (the verdict-gated flip)

`live` withholds **disjoint only**. Both unknown causes are recorded and
*admitted*: withholding on an unresolved roster is the safe reading, but
it silently degrades a persona that has been useful for four releases,
and the measurement — not the caution — is the guard.

The flip is a separate PR, opens only on a green verdict, and states its
own threshold for the delta. A red or absent verdict ships the release
in `shadow` with the measured delta recorded as a Known Gap.

**The trade the flip buys, stated plainly**: RFC 0049's headline —
"taught in a DM, known in the standup" — narrows to *the standup whose
every member was in the DM*. In the default three-persona rooms, that is
no standup at all. Whether that is the right trade is what the delta
measures.

## Security considerations

The change is **restrictive only**: every entry it acts on is one §D had
already admitted, so no path is opened. In `shadow` it withholds
nothing, which is why the byte-identity claim (every prompt identical to
v0.3.15) is checkable rather than asserted.

The residual gap is honest and named: audience is *injection-time*
membership. Someone who was in a room when a fact was taught and has
since left still counts as audience; someone who joined the source room
after the fact was taught counts too. RFC 0035's interval ledger is the
answer to both, and is deliberately not in this release.

## Consequences

- One roster round trip per distinct source room per turn, deduplicated,
  bounded by the tier recall limits, and free for the acting room.
- The `relationship` tier is untouched — it is outside the §D gate by
  the RFC's Non-Goals, and its cross-room identity fields are protected
  at the write side, not by a read gate.
- Notes carry no `source_channel_id` and so acquire no audience.

## Sequencing & dependencies

Depends on PR A1 (roster resolution moved ahead of the gate, for every
channel turn) and on the [RFC 0044](0044-eval-set-golden-traces.md)
Phase 1 harness for its measurement. Blocks nothing; the flip blocks
release-prep PR 0. The live proof is
[MT-PERSONA-CONFIDENTIALITY-001](../manual-tests/MT-PERSONA-CONFIDENTIALITY-001.md)
Leg 5, run once at release-prep PR 1.

## Related documentation

- [ISSUE-0132](../issues/ISSUE-0132-memory-egress-gate-blind-to-room-audience.md) — the finding and its evidence.
- [v0.3.16 scope locks](../v0.3.16-scope-locks.md) — locks 1–3, which this amendment implements verbatim.
- [RFC 0049 L1 amendment §Promotion](0049-amendment-l1-cross-room-availability.md#promotion-v0312-pr-4--the-measurement-gated-flip) — the shadow → verdict → flip pattern reused here.
- [RFC 0035 — Channel Membership Interval Ledger](0035-channel-membership-interval-ledger.md) — the assertion-time refinement this amendment defers.
