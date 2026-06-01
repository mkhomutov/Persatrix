# Manual Test MT-EPOCH-001: epoch structural run-isolation

**Test ID**: `MT-EPOCH-001`
**Feature Area**: Epochs (RFC 0031 epoch axis — ISSUE-0085)
**Version**: 1.0
**Created**: 2026-06-01
**Last Updated**: 2026-06-01
**Status**: Active

---

## Overview

**Purpose**: Verify the **structural** half of the F-3 fix that a fresh channel
or session name alone cannot reach: a rerun under a fresh `PERSATRIX_EPOCH` (or
`--epoch`), **same room + same `--user`**, inherits **no** episodes, **no**
relationship trust, and **no** person-facts. Relationships and facts are keyed
on the *participant*, so reusing `--user alice` would otherwise carry old trust
forward even under a new session; the epoch axis isolates the whole run at once
([ISSUE-0085](../issues/ISSUE-0085-epoch-axis-run-isolation.md)).

This is the live counterpart to
[`test_epoch_run_isolation.py`](../../tests/integration/test_epoch_run_isolation.py).

**Scope**:
- Two runs holding **room and user constant**, varying only the epoch.
- The fresh-epoch run inherits nothing across all participant-keyed tiers
  (episodes, relationships/trust, facts).
- Epoch is **strict-equality** isolation: **no `legacy` carve-out, no `*`
  wildcard** (contrast the session axis).
- The `channel.dispatch` span carries the resolved epoch (low-cardinality, per
  [OQ #7](../rfcs/0031-per-session-namespacing-channels.md#open-questions)).

**Out of Scope**:
- Room/session-scoped recall isolation — [MT-SESSION-003](MT-SESSION-003.md).
- The session operator verbs — [MT-SESSION-002](MT-SESSION-002.md). (Epoch has
  **no** `new`/`use` lifecycle and no active-epoch pointer file — it is a bare
  flag-or-env knob.)

---

## Related Documentation

- [docs/guides/epochs.md](../guides/epochs.md) — operator guide (the `PERSATRIX_EPOCH` knob, the `--epoch` override, epoch-vs-session-vs-`make reset`).
- [docs/rfcs/0031-epoch-pr-plan.md](../rfcs/0031-epoch-pr-plan.md) — the epoch PR sequence (storage v12 → filter → gRPC rail → operator surface → closeout).
- [docs/memory-scope-axes.md §Epoch](../memory-scope-axes.md#epoch--the-testrun-isolation-axis) — the design rationale.
- [ISSUE-0085](../issues/ISSUE-0085-epoch-axis-run-isolation.md) — the tracking issue.

**Related Automated Tests**:
- `tests/integration/test_epoch_run_isolation.py` — fresh-epoch rerun inherits nothing; the prior run's bundle retains its epoch snapshot.
- `tests/unit/python/test_epoch_filter.py` / `test_epoch_scope.py` / `test_epoch_no_carveout.py` / `test_epoch_migration.py` / `test_epoch_metadata_rail.py` — strict-equality filter, the no-carve-out invariant, the v12 migration, the gRPC metadata rail.

---

## Preconditions

A live persona stack with `ANTHROPIC_API_KEY` set:

```bash
make demo-anthropic
export PERSATRIX_SERVER=http://127.0.0.1:8080
```

The orchestrator boots under the default `live` epoch (an INFO line records the
fallback when `PERSATRIX_EPOCH` is unset). Per-invocation `--epoch` overrides it
for one turn — the tidiest way to run this MT against an already-serving `live`
stack. Start from a clean memory surface (`make reset`, then bring the stack up)
so the `live` baseline is empty.

> **Hold room + user constant.** The whole point of this MT is that varying the
> *epoch alone* — same `ember-owl`, same `--user alice`, same channel/DM —
> isolates the run. Do **not** vary the user or room between Step 1 and Step 3,
> or you are testing the session axis instead.

> **Set the epoch at the persona's boot for write-scope.** As with the session
> axis ([MT-SESSION-003](MT-SESSION-003.md) Preconditions), the persona writes
> its episodes/facts at *interaction close* in its background loop. To land a
> run's rows under a given epoch, boot the persona under `PERSATRIX_EPOCH`
> (Docker: thread it into the agent service env; local:
> `PERSATRIX_EPOCH=trial-7 python -m persatrix_agents.server …`). The
> per-invocation `--epoch` governs the dispatch/recall binding for that call;
> start a *fresh* interaction (don't append to one opened under the prior epoch)
> when switching epochs, or the close-path write follows the interaction's
> original epoch. The authoritative structural-isolation proof is
> [`test_epoch_run_isolation.py`](../../tests/integration/test_epoch_run_isolation.py).

---

## Test Procedure

### Step 1: Baseline run under the default `live` epoch — build trust + a fact

**Action**:

Drive two or three turns so a relationship row accrues trust and a person-fact
lands, all under the `live` epoch (the boot default), same room + user:

```bash
echo "Hey, I'm Alice — I lead the platform team. I trust your read on incidents." \
  | ./bin/persatrix chat ember-owl --user alice
echo "We shipped the migration cleanly last night, thanks for the steer." \
  | ./bin/persatrix chat ember-owl --user alice
```

**Expected**: a `relationships` row for `alice` with a non-trivial `trust_score`
and ≥ 1 `episodes` + a `facts` row, all tagged `epoch_id='live'`.

**Verification**:
- [ ] `sqlite3 <ember-owl memory.db> "SELECT trust_score, epoch_id FROM relationships WHERE other_participant_id LIKE '%alice%';"`
  shows a `live` row with accrued trust.
- [ ] `sqlite3 <memory.db> "SELECT COUNT(*) FROM episodes WHERE epoch_id='live';"` ≥ 1.

---

### Step 2: Confirm continuity *within* the `live` epoch

**Action**:

```bash
echo "Where did we land on that incident process?" \
  | ./bin/persatrix chat ember-owl --user alice
```

**Expected**: the persona recalls the prior turns (trust + the platform-team
fact) — within-epoch continuity is intact; the epoch axis does not break the
normal accumulation a `live` deployment relies on.

**Verification**:
- [ ] The reply reflects the established relationship/fact (recognises Alice,
  references the prior context).

---

### Step 3: Rerun under a fresh epoch — same room + same user — inherits nothing

**Action**:

```bash
# Persona booted under PERSATRIX_EPOCH=trial-7 (see Preconditions), or per-call:
echo "Where did we land on that incident process?" \
  | ./bin/persatrix chat ember-owl --user alice --epoch trial-7
```

**Expected**: under `--epoch trial-7`, the **same** persona and the **same**
`--user alice` start from a blank relationship — **no** accrued trust, **no**
prior episodes, **no** facts. The persona treats Alice as new (no recall of the
platform team or the migration). This is the structural residue a fresh channel
name alone cannot reach.

**Verification**:
- [ ] The reply shows **no** recall of the Step 1–2 context (treats Alice as a
  first contact).
- [ ] `sqlite3 <memory.db> "SELECT DISTINCT epoch_id FROM episodes;"` shows both
  `live` and `trial-7` — the rows coexist; isolation is a recall filter keyed on
  strict epoch equality, not a delete.
- [ ] `sqlite3 <memory.db> "SELECT trust_score, epoch_id FROM relationships WHERE other_participant_id LIKE '%alice%';"`
  shows the `live` trust row **and** (after Step 3 writes) a separate `trial-7`
  row that did **not** inherit the `live` trust score.

---

### Step 4: No `legacy` carve-out, no `*` wildcard

**Action**:

Confirm the epoch axis has none of the session axis's escape hatches: a
`live`-epoch row is **not** visible to `trial-7` (contrast a `legacy`-session row,
which *is* visible to every session — [MT-SESSION-003](MT-SESSION-003.md) Step 4).

```bash
# Already shown by Step 3: the live context did not surface under trial-7.
# Strict equality is asserted by tests/integration/test_epoch_run_isolation.py
# (TestEpochHasNoCarveOut::test_live_epoch_rows_invisible_to_a_fresh_epoch).
```

**Expected**: there is no epoch value (no `legacy`, no `*`) that unions epochs at
the default recall path — strict equality only.

**Verification**:
- [ ] Step 3's no-recall result *is* this assertion at the operator surface;
  the `test_epoch_no_carveout.py` unit gate is the automated counterpart.

---

### Step 5: The `channel.dispatch` span carries the resolved epoch

**Action**:

Drive an epoch'd turn over a channel (always-sampled by tagging a workflow or a
deliberately-slow trace — healthy fast turns are tail-sampled at 1%, see
[MT-OTEL-001](MT-OTEL-001.md)), and inspect the span attributes in Jaeger, or
read it off telemetry:

```bash
./bin/persatrix channel send planning "kickoff" --epoch trial-7
# Jaeger → channel.dispatch span → attribute persatrix.epoch=trial-7
```

**Expected**: the `channel.dispatch` span carries the resolved epoch as a
low-cardinality attribute (OQ #7); the default-`live` path carries `live`.

**Verification**:
- [ ] The dispatch span exposes the resolved epoch attribute (or, under tail
  sampling, the `test_epoch_metadata_rail.py` gate covers the rail).

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|------------------|-----------|
| 1 | `live` baseline accrues trust + a fact, same room+user | ☐ |
| 2 | Within-`live`-epoch continuity intact | ☐ |
| 3 | Fresh `--epoch trial-7`, same room+user, inherits **nothing** | ☐ |
| 4 | No `legacy` carve-out, no `*` wildcard — strict equality | ☐ |
| 5 | `channel.dispatch` span carries the resolved epoch | ☐ |

**Release gate**: Step 2 **and** Step 3 — within-epoch continuity preserved
*and* a fresh epoch isolates the run structurally (participant-keyed trust/facts
do **not** carry forward). This is the F-3 structural fix a session alone cannot
deliver.

---

## Edge Cases & Error Scenarios

### Edge Case 1: Epoch vs. session — which axis to reach for

If you vary the **session** but reuse the **user**, participant-keyed
relationship trust / facts can still carry forward — that is by design (session =
room continuity). When you need a rerun that inherits *nothing* even with the
same user, reach for the **epoch** ([epochs guide](../guides/epochs.md), the
Epoch-vs-session table). `make reset` wipes *all* epochs and sessions at once —
the whole-stack nuke, not the per-run tool.

### Edge Case 2: Non-canonical / non-ASCII epoch value

A value outside `[A-Za-z0-9_-]` is **accepted verbatim with a WARN** on the
process knob (parity with `PERSATRIX_SESSION_ID`). But because the epoch rides a
gRPC metadata header, a **control or non-ASCII byte** is rejected with a
`BAD_REQUEST` ([epochs guide](../guides/epochs.md)) — the header must be
printable ASCII.

### Edge Case 3: Production never sets it

Every untagged deployment runs under `live`, so production behaviour is
unchanged — the epoch axis is dormant until CI/test bumps it. Forgetting to bump
the epoch in CI means runs share the `live` epoch and bleed into each other (the
pre-fix behaviour) — that is an operator omission, not a regression.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-06-01 | Claude (Opus 4.8) | macOS + Docker (OpenAI stack) | Pass | Structural isolation green via [`test_epoch_run_isolation.py`](../../tests/integration/test_epoch_run_isolation.py) (incl. `TestEpochHasNoCarveOut`) on `main` `3ceb400`; migration **v12** `epoch_id` on all five tiers applied live (`schema_version=12`); persona epoch-boot confirmed (`PERSATRIX_EPOCH=trial-7` → a `relationships` row tagged `trial-7`). Live wrinkle noted (F-2): start a fresh interaction when switching epochs, else the close-path episode keeps the prior epoch. See [v0.3.5-execution-report.md](v0.3.5-execution-report.md#mt-epoch-001--epoch-structural-run-isolation-gate--live). |

---

## Notes

- Epoch is the **run/test-isolation** axis ("which logical run wrote this
  row?"); session is the **room-continuity** axis ("which conversation?"). They
  are orthogonal — a row carries both a `session_id` and an `epoch_id`
  ([memory-scope-axes.md](../memory-scope-axes.md)).
- Isolation is a strict-equality **recall filter**, not a delete — Step 3's
  storage check confirms `live` and `trial-7` rows coexist; only what a given
  epoch *surfaces* differs.
- Unlike a session, an epoch has **no lifecycle**: no `new`, no `use`, no pointer
  file — just `--epoch` (per invocation) or `PERSATRIX_EPOCH` (per process),
  defaulting to `live`.
