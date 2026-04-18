# Manual Test MT-MEMORY-003: Working Memory — Summarisation Triggers Near Context-Window Threshold

**Test ID**: `MT-MEMORY-003`
**Feature Area**: Memory
**Version**: 1.0
**Created**: 2026-04-18
**Last Updated**: 2026-04-18
**Status**: Active

---

## Overview

**Purpose**: Verify that `WorkingMemory` triggers LLM-assisted summarisation when total context
tokens approach the configured `max_tokens` limit, and that the compression pass reduces total
token count while preserving section content.

**Scope**: `WorkingMemory.compress_if_needed()`, token estimation, compression log output,
`max_tokens` threshold.

**Out of Scope**: Episodic/relationship memory; LLM response quality.

---

## Related Documentation

**Feature Documentation**:
- [docs/rfcs/0005-persona-agent-memory.md](../rfcs/0005-persona-agent-memory.md)
- [agents/memory/working.py](../../agents/memory/working.py)

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
- `ANTHROPIC_API_KEY` set — compression calls the LLM to summarise sections
- Agents package installed: `make build-agents`

### Application State

- ☐ No orchestrator required — library-level test.
- ☐ Internet access to `api.anthropic.com` for LLM compression call.

---

## Test Procedure

### Step 1: Fill Working Memory Above the Threshold

**Action**: Set a low `max_tokens` limit (1 000 tokens) and add sections that exceed it, then
call `compress_if_needed()`. Run:

```bash
python3 - <<'EOF'
import asyncio, logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from persatrix_agents.memory.working import WorkingMemory
from persatrix_agents.llm_client import LLMClient

# Use a small threshold to trigger compression without adding 100 k tokens.
MAX_TOKENS = 1_000

async def main():
    mem = WorkingMemory(max_tokens=MAX_TOKENS)
    # Approximately 300 tokens each (chars / 4 ≈ 300)
    filler = "The quick brown fox jumps over the lazy dog. " * 27  # ~300 tokens

    mem.set_section("history",    filler, priority=1)
    mem.set_section("recent",     filler, priority=2)
    mem.set_section("background", filler, priority=3)
    mem.set_section("goals",      filler, priority=4)

    total = sum(len(s.content) // 4 for s in mem._sections.values())
    print(f"Total estimated tokens before compression: {total}")
    assert total > MAX_TOKENS, "Pre-condition: content must exceed threshold"

    # Build context to confirm overflow is detected
    ctx = mem.build_context()
    print(f"Context built (may be truncated); total sections: {len(mem._sections)}")

    # Trigger compression (requires LLM)
    client = LLMClient()  # reads ANTHROPIC_API_KEY from environment
    await mem.compress_if_needed(client)
    # Wait for background compression task to finish
    import asyncio as _a
    await _a.sleep(10)

    total_after = sum(len(s.content) // 4 for s in mem._sections.values())
    print(f"Total estimated tokens after compression: {total_after}")

asyncio.run(main())
EOF
```

**Expected Result**: Compression fires; total tokens after is lower than before.

**Verification**:
- [ ] `"Total estimated tokens before compression"` value exceeds `MAX_TOKENS` (1 000)
- [ ] At least one `"Compression pass: X → Y total tokens"` log line printed (INFO level)
- [ ] `"Total estimated tokens after compression"` is lower than before

---

### Step 2: Verify Compression Log Messages

**Action**: Inspect stdout/stderr from Step 1 for expected log lines:

```bash
# The script above prints INFO logs to stderr; re-run with grep if needed:
python3 <the script above> 2>&1 | grep -E "Compression|Compressed|section"
```

**Expected Result**: At least one of:
- `"Compressed section 'history': N → M tokens"` (INFO)
- `"Compression pass: N → M total tokens"` (INFO)

**Verification**:
- [ ] At least one compression log line present
- [ ] No `"Failed to compress section"` warning (unless LLM returned empty text — acceptable)
- [ ] No unhandled Python exception

---

### Step 3: Verify Section Content Preserved

**Action**: After compression, confirm sections still exist and have non-empty content:

Add to the script from Step 1 (after `sleep(10)`):

```python
    for name, section in mem._sections.items():
        assert len(section.content) > 0, f"Section '{name}' has empty content after compression"
        print(f"Section '{name}': {len(section.content)} chars remaining")
    print("PASS")
```

**Expected Result**: All sections retain non-empty content.

**Verification**:
- [ ] Each section reports a positive char count
- [ ] Script prints `"PASS"`

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Compression triggered; token count reduced | ☐ |
| 2 | Compression log lines present; no failure warnings | ☐ |
| 3 | All sections retain non-empty content | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: LLM Compression Returns Empty Text

**Scenario**: LLM API returns an empty string for a section summary.

**Expected Behavior**: Warning logged: `"Compression of section 'N' returned no text, preserving
original"`. Original section content is kept; compression pass continues with remaining sections.

### Edge Case 2: tiktoken Not Installed

**Scenario**: `tiktoken` package absent from the environment.

**Expected Behavior**: Warning logged that token estimation falls back to `chars // 4`. Compression
still triggers correctly (conservative token estimates may cause earlier-than-needed compression).

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| | | | | |

---

## Notes

- `ANTHROPIC_API_KEY` is required. Without it, `compress_if_needed()` will raise an authentication
  error on the first LLM call; the section content will be preserved unchanged.
- The `sleep(10)` in the script waits for the background compression task. For slower network
  connections increase this to 30 s.
