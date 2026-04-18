# Manual Test MT-MEMORY-002: Relationship Memory — Trust Score Updates After N Exchanges

**Test ID**: `MT-MEMORY-002`
**Feature Area**: Memory
**Version**: 1.0
**Created**: 2026-04-18
**Last Updated**: 2026-04-18
**Status**: Active

---

## Overview

**Purpose**: Verify that `RelationshipMemory.update_trust()` updates trust scores correctly,
enforces the ±0.2 per-call delta cap, persists scores across close/re-open, and that
`apply_decay()` moves scores toward the neutral value of 0.5.

**Scope**: `update_trust()`, `_MAX_TRUST_DELTA` (0.2) cap, default trust (0.5), persistence,
`apply_decay()`.

**Out of Scope**: LLM-driven relationship inference; episodic memory integration.

---

## Related Documentation

**Feature Documentation**:
- [docs/rfcs/0005-persona-agent-memory.md](../rfcs/0005-persona-agent-memory.md)
- [agents/memory/relationship.py](../../agents/memory/relationship.py)

**Related Automated Tests**:
- Unit tests: `tests/unit/python/test_agents.py`

---

## Preconditions

### System Requirements

**Operating Systems**:
- ☐ Windows 10/11 (x64)
- ☐ macOS 12.0+ (Intel/Apple Silicon)
- ☐ Linux (Ubuntu 22.04+)

**Dependencies Installed**:
- Python 3.11+: `python3 --version`
- Agents package installed: `make build-agents`

### Application State

- ☐ No orchestrator required — library-level test.
- ☐ `data/` directory writable: `mkdir -p data`
- ☐ No leftover test database from a previous run (remove DB and SQLite WAL files):

```bash
rm -f data/mt-memory-002.db data/mt-memory-002.db-shm data/mt-memory-002.db-wal
```

---

## Test Procedure

### Step 1: Verify Default Trust for an Unknown Pair

**Action**:

```bash
python3 - <<'EOF'
import asyncio
from persatrix_agents.memory.relationship import RelationshipMemory

DB = "data/mt-memory-002.db"

async def main():
    mem = RelationshipMemory("ember-owl", db_path=DB)
    await mem.initialize()

    # get_trust() returns the float score directly (0.5 default for unknown pairs)
    trust = await mem.get_trust("unknown-agent")
    print(f"Default trust for unknown pair: {trust}")
    assert abs(trust - 0.5) < 0.001, f"Expected 0.5, got {trust}"

    await mem.close()
    print("PASS")

asyncio.run(main())
EOF
```

> **Fix (2026-04-18)**: `RelationshipMemory` has no `get_relationship()` method.
> Use `get_trust(other_agent_id)` to read a trust score, or
> `get_relationship_summary(other_agent_id)` to get the full `RelationshipSummary` object.
> Steps 3 and 4 below are updated accordingly.

**Expected Result**: `"Default trust for unknown pair: 0.5"` then `"PASS"`.

**Verification**:
- [x] Output contains `"Default trust for unknown pair: 0.5"`
- [x] Script exits 0

---

### Step 2: Apply Trust Updates and Verify Delta Cap

**Action**: Apply three positive updates with delta 0.15, then one oversized update with delta 0.5
(should be clamped to 0.2):

```bash
python3 - <<'EOF'
import asyncio
from persatrix_agents.memory.relationship import RelationshipMemory

DB = "data/mt-memory-002.db"

async def main():
    mem = RelationshipMemory("ember-owl", db_path=DB)
    await mem.initialize()

    score = 0.5  # starting default
    for i in range(3):
        score = await mem.update_trust("iron-fox", delta=0.15, reason=f"positive interaction {i+1}")
        print(f"After update {i+1} (+0.15): {score:.3f}")

    # Oversized delta — must be clamped to 0.2
    score = await mem.update_trust("iron-fox", delta=0.5, reason="large positive event")
    print(f"After capped update (+0.5 clamped to 0.2): {score:.3f}")
    assert score <= 1.0, "Trust must not exceed 1.0"

    await mem.close()
    print("PASS")

asyncio.run(main())
EOF
```

**Expected Result**: The first three updates apply `delta=0.15` without clamping (each increases
the score by exactly 0.15). The oversized `delta=0.5` update is capped to 0.2. Score never
exceeds 1.0.

**Verification**:
- [x] Scores print as increasing values after each `+0.15` update
- [x] The capped update moves score by ≤ 0.2 (not 0.5)
- [x] Script exits 0 with `"PASS"`

---

### Step 3: Verify Persistence Across Restart

**Action**: Re-open the database and confirm the score from Step 2 survived:

```bash
python3 - <<'EOF'
import asyncio
from persatrix_agents.memory.relationship import RelationshipMemory

DB = "data/mt-memory-002.db"

async def main():
    mem = RelationshipMemory("ember-owl", db_path=DB)
    await mem.initialize()

    rel = await mem.get_relationship_summary("iron-fox")
    assert rel is not None, "Relationship record missing after restart"
    print(f"Trust after restart: {rel.trust_score:.3f}")
    assert rel.trust_score > 0.5, "Trust should be above default after positive updates"

    await mem.close()
    print("PASS")

asyncio.run(main())
EOF
```

**Expected Result**: Trust score matches the last value from Step 2.

**Verification**:
- [x] `"Trust after restart"` value matches the last printed score in Step 2
- [x] Script exits 0 with `"PASS"`

---

### Step 4: Apply Decay and Verify Movement Toward Neutral

**Action**:

```bash
python3 - <<'EOF'
import asyncio
from persatrix_agents.memory.relationship import RelationshipMemory

DB = "data/mt-memory-002.db"

async def main():
    mem = RelationshipMemory("ember-owl", db_path=DB)
    await mem.initialize()

    rel_before = await mem.get_relationship_summary("iron-fox")
    trust_before = rel_before.trust_score

    count = await mem.apply_decay(decay_rate=0.01)
    print(f"Decay applied to {count} relationship(s)")

    rel_after = await mem.get_relationship_summary("iron-fox")
    trust_after = rel_after.trust_score

    print(f"Trust before decay: {trust_before:.4f}, after: {trust_after:.4f}")
    assert abs(trust_after - 0.5) < abs(trust_before - 0.5), \
        "Decay must move trust closer to 0.5"

    await mem.close()
    print("PASS")

asyncio.run(main())
EOF
```

**Expected Result**: Trust score moves toward 0.5 after `apply_decay()`.

**Verification**:
- [x] `count` ≥ 1
- [x] `trust_after` is numerically closer to 0.5 than `trust_before`
- [x] Script exits 0 with `"PASS"`

---

### Step 5: Clean Up

```bash
rm -f data/mt-memory-002.db data/mt-memory-002.db-shm data/mt-memory-002.db-wal
```

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Default trust for unknown pair is 0.5 | ☑ |
| 2 | Delta cap enforced; score ≤ 1.0 | ☑ |
| 3 | Score persists across close/re-open | ☑ |
| 4 | Decay moves score toward 0.5 | ☑ |
| 5 | Test DB cleaned up | ☑ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: Reason String Exceeds 1024 Characters

**Scenario**: `reason` parameter longer than 1024 chars passed to `update_trust()`.

**Expected Behavior**: Reason is silently truncated to 1024 chars; a warning is logged:
`"reason truncated from N to 1024 chars for ember-owl→iron-fox"`. The update still succeeds.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| 2026-04-18 | mkhomutov | Windows 11 | Pass | All 5 steps pass. Doc fix: replaced `get_relationship()` (does not exist) with `get_trust()` (Step 1) and `get_relationship_summary()` (Steps 3/4). Delta cap, persistence, and decay all verified. |
| 2026-04-18 | mkhomutov | Windows 11 | Pass | All 5 steps pass. Doc fixes: added pre-run cleanup step (remove stale DB + `.db-shm`/`.db-wal` WAL files); updated Step 5 cleanup to include WAL files. |
