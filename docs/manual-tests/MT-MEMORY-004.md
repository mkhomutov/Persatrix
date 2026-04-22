# Manual Test MT-MEMORY-004: Memory Injection Token Budget — Per-Event Bound Holds

**Test ID**: `MT-MEMORY-004`
**Feature Area**: Memory
**Version**: 1.0
**Created**: 2026-04-22
**Last Updated**: 2026-04-22
**Status**: Active

---

## Overview

**Purpose**: Verify that the per-event memory-injection token budget introduced by RFC 0017
([Section B](../rfcs/0017-persona-memory-injection-budget.md#b-memory-budget-allocator)) is
enforced at the allocator layer and that an oversized content payload cannot exceed
`_MEMORY_BUDGET_TOKENS = 1500`.

**Scope**: `agents.persona_runtime.memory_budget.MemoryBudget` allocator behaviour, token-aware
truncation, greedy in-priority-order admission, and the `MemoryInjectionResult.memory_admitted_tokens`
contract that RFC 0017 §F's TICK short-circuit consumes.

**Out of Scope**: LLM response quality; relevance-threshold (`min_score`) tuning (covered by
`tests/unit/python/test_episodic_memory.py`); working-memory summarisation (covered by
[MT-MEMORY-003](MT-MEMORY-003.md)).

---

## Related Documentation

**Feature Documentation**:
- [docs/rfcs/0017-persona-memory-injection-budget.md](../rfcs/0017-persona-memory-injection-budget.md) — §B Memory Budget Allocator
- [agents/persona_runtime/memory_budget.py](../../agents/persona_runtime/memory_budget.py)
- [agents/persona_runtime/memory_context.py](../../agents/persona_runtime/memory_context.py) — `_MEMORY_BUDGET_TOKENS`, `_inject_memory_context`

**Related Automated Tests**:
- Unit tests: `agents/tests/test_memory_budget.py`
- Unit tests: `agents/tests/test_inject_memory_context.py`
- Unit tests: `agents/tests/test_inject_memory_context_gates.py`
- Integration tests: `tests/integration/test_memory_budget_e2e.py`

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

- ☐ No orchestrator or agent processes required — this test exercises the library directly.
- ☐ No `ANTHROPIC_API_KEY` required — the allocator is a pure function with no LLM calls.

---

## Test Procedure

### Step 1: Allocator Greedily Fills the Budget and Drops Overflow

**Action**: Run the following Python script:

```bash
python3 - <<'EOF'
from persatrix_agents.persona_runtime.memory_budget import MemoryBudget
from persatrix_agents.persona_runtime.memory_context import _MEMORY_BUDGET_TOKENS

print(f"_MEMORY_BUDGET_TOKENS = {_MEMORY_BUDGET_TOKENS}")
budget = MemoryBudget(total_tokens=_MEMORY_BUDGET_TOKENS)

# Build five oversized items, each ~600 tokens (~2 400 chars).
filler = "The quick brown fox jumps over the lazy dog. " * 54  # ~600 tokens
items = [f"item-{i}: {filler}" for i in range(5)]

admitted: list[str] = []
for i, item in enumerate(items):
    result = budget.try_add(item)
    print(f"  try_add(item-{i}) -> "
          f"{'admitted' if result is not None else 'DROPPED'}; "
          f"remaining={budget.remaining}")
    if result is not None:
        admitted.append(result)

assert budget.remaining >= 0, "Budget cannot go negative"
assert budget.remaining <= _MEMORY_BUDGET_TOKENS, "Budget cannot exceed initial total"
total_admitted_tokens = _MEMORY_BUDGET_TOKENS - budget.remaining
print(f"Total admitted tokens: {total_admitted_tokens} "
      f"(<= {_MEMORY_BUDGET_TOKENS})")
print(f"Items admitted: {len(admitted)} / {len(items)} "
      f"(later items expected to be dropped)")
print("PASS")
EOF
```

**Expected Result**: Earlier items admitted; later items dropped once the budget is exhausted;
total admitted tokens never exceed `_MEMORY_BUDGET_TOKENS`.

**Verification**:
- [ ] `_MEMORY_BUDGET_TOKENS = 1500` printed
- [ ] At least one `admitted` and at least one `DROPPED` line
- [ ] Final line `PASS` printed (no `AssertionError` raised)
- [ ] `Total admitted tokens` value is `<= 1500`

---

### Step 2: Per-Item `min_tokens` Floor Drops Slivers

**Action**: Run the following Python script — exhaust most of the budget, then attempt to admit
one more oversized item with a high `min_tokens` floor:

```bash
python3 - <<'EOF'
from persatrix_agents.persona_runtime.memory_budget import MemoryBudget

budget = MemoryBudget(total_tokens=120)

# Consume ~100 tokens.
filler_100 = "x " * 100  # ~100 tokens
admitted_first = budget.try_add(filler_100, min_tokens=32)
print(f"first admission: {'OK' if admitted_first is not None else 'FAILED'}; "
      f"remaining={budget.remaining}")

# Try to add a large item with a high floor.  Truncated form would be small;
# the min_tokens floor must drop it entirely and leave remaining unchanged.
remaining_before = budget.remaining
big_item = "y " * 1000
result = budget.try_add(big_item, min_tokens=200)
remaining_after = budget.remaining
print(f"second admission: {'admitted' if result is not None else 'DROPPED'}; "
      f"remaining: {remaining_before} -> {remaining_after}")

assert result is None, "High min_tokens floor should drop the sliver"
assert remaining_before == remaining_after, "Dropped item must not consume budget"
print("PASS")
EOF
```

**Expected Result**: The second `try_add` returns `None`; budget `remaining` is unchanged across
the dropped call.

**Verification**:
- [ ] First admission `OK`
- [ ] Second admission line shows `DROPPED`
- [ ] `remaining_before == remaining_after`
- [ ] Final `PASS` printed

---

### Step 3: Empty Input Returns `None` Without Touching the Budget

**Action**:

```bash
python3 - <<'EOF'
from persatrix_agents.persona_runtime.memory_budget import MemoryBudget

budget = MemoryBudget(total_tokens=500)
remaining_before = budget.remaining
result = budget.try_add("")
remaining_after = budget.remaining

print(f"empty try_add returned: {result!r}; "
      f"remaining: {remaining_before} -> {remaining_after}")
assert result is None, "Empty input must return None"
assert remaining_before == remaining_after, "Empty input must not consume budget"
print("PASS")
EOF
```

**Expected Result**: `try_add("")` returns `None`; budget `remaining` is unchanged.

**Verification**:
- [ ] Output shows `empty try_add returned: None`
- [ ] `remaining` unchanged
- [ ] Final `PASS` printed

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Greedy admission; later items dropped; total admitted ≤ 1500 | ☐ |
| 2 | Sliver below `min_tokens` is dropped; budget unchanged | ☐ |
| 3 | Empty input returns `None`; budget unchanged | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: `tiktoken` Not Installed

**Scenario**: `tiktoken` package absent from the environment.

**Expected Behavior**: Allocator falls back to the `chars // 4 ≈ tokens` approximation. Token
bound becomes approximate but never panics. The `_MEMORY_BUDGET_TOKENS` ceiling continues to hold
within fallback precision.

### Edge Case 2: `total_tokens=0` Initial Budget

**Scenario**: `MemoryBudget(total_tokens=0)` constructed with no headroom.

**Expected Behavior**: Every `try_add` call returns `None`; `remaining` stays at 0.

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| YYYY-MM-DD | [Name] | [OS] | Pass/Fail | [Notes] |

---

## Notes

- The token bound is enforced at the allocator layer; the per-event integration path
  (`_inject_memory_context` queries three memory tiers and feeds each item through the same
  allocator) is exercised by the automated integration test
  [`tests/integration/test_memory_budget_e2e.py`](../../tests/integration/test_memory_budget_e2e.py).
  This manual test focuses on the allocator's pure-function contract because it is the smallest
  reproducible surface that proves the cap holds.
- `_MEMORY_BUDGET_TOKENS` is a module-level constant; retuning is a one-line change per
  RFC 0017 [OQ1](../rfcs/0017-persona-memory-injection-budget.md#open-questions). If the
  constant changes in a future release, update Step 1's expected value accordingly.
