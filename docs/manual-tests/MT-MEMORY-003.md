# Manual Test MT-MEMORY-003: Working Memory — Summarisation Triggers Near Context-Window Threshold

**Test ID**: `MT-MEMORY-003`
**Feature Area**: Memory
**Version**: 1.0
**Created**: 2026-04-18
**Last Updated**: 2026-04-18
**Status**: Complete

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

from persatrix_agents.memory.working import WorkingMemory, ContextSection
from persatrix_agents.llm_client import LLMClient, AnthropicProvider

# Use a small threshold to trigger compression without adding 100 k tokens.
MAX_TOKENS = 1_000

async def main():
    mem = WorkingMemory(max_tokens=MAX_TOKENS)
    # Approximately 300 tokens each (chars / 4 ≈ 300)
    filler = "The quick brown fox jumps over the lazy dog. " * 27  # ~300 tokens

    for name, priority in [("history", 1), ("recent", 2), ("background", 3), ("goals", 4)]:
        token_count = len(filler) // 4
        mem.add_section(ContextSection(
            name=name, content=filler, priority=priority,
            token_count=token_count, compressible=True,
        ))

    total = mem.total_tokens()
    print(f"Total estimated tokens before compression: {total}")
    assert total > MAX_TOKENS, "Pre-condition: content must exceed threshold"

    # Build context to confirm overflow is detected
    ctx = mem.build_context()
    print(f"Context built (may be truncated); total sections: {len(mem._sections)}")

    # Trigger compression (requires LLM).
    # compress_if_needed is async and awaited directly here, so it runs to
    # completion before the next line — no sleep needed.
    # (The background-task path goes through try_start_compression +
    # await_pending_compression, not this function directly.)
    client = LLMClient(AnthropicProvider())  # reads ANTHROPIC_API_KEY from environment
    await mem.compress_if_needed(client)

    total_after = mem.total_tokens()
    print(f"Total estimated tokens after compression: {total_after}")

asyncio.run(main())
EOF
```

> **Fix (2026-04-18)**: Three API errors in the original script:
> 1. `mem.set_section(name, content, priority=N)` does not exist — use
>    `mem.add_section(ContextSection(name=..., content=..., priority=..., token_count=..., compressible=True))`.
> 2. `mem._sections.values()` is wrong — `_sections` is a `list`, not a dict. Use
>    `mem.total_tokens()` instead of manually summing.
> 3. `ContextSection` must be imported from `persatrix_agents.memory.working`.

> **Fix (2026-04-18)**: Two additional bugs discovered during full run:
> 4. `LLMClient()` requires a provider argument — use `LLMClient(AnthropicProvider())` (also import
>    `AnthropicProvider` from `persatrix_agents.llm_client`).
> 5. Default `compression_model` was `"claude-haiku-4"` (non-existent model) — corrected to
>    `"claude-haiku-4-5"` in `agents/memory/working.py`.

**Expected Result**: Compression fires; total tokens after is lower than before.

**Verification**:
- [x] `"Total estimated tokens before compression"` value exceeds `MAX_TOKENS` (1 000)
- [x] At least one `"Compression pass: X → Y total tokens"` log line printed (INFO level)
- [x] `"Total estimated tokens after compression"` is lower than before

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
- [x] At least one compression log line present
- [x] No `"Failed to compress section"` warning (unless LLM returned empty text — acceptable)
- [x] No unhandled Python exception

---

### Step 3: Verify Section Content Preserved

**Action**: After compression, confirm sections still exist and have non-empty content:

Add to the script from Step 1 (after `await mem.compress_if_needed(client)`):

```python
    for section in mem._sections:
        assert len(section.content) > 0, f"Section '{section.name}' has empty content after compression"
        print(f"Section '{section.name}': {len(section.content)} chars remaining")
    print("PASS")
```

> **Fix (2026-04-18)**: `mem._sections` is a `list[ContextSection]`, not a dict.
> Iterate it directly; access the name via `section.name`.

**Expected Result**: All sections retain non-empty content.

**Verification**:
- [x] Each section reports a positive char count
- [x] Script prints `"PASS"`

> **Note**: Steps 2 and 3 require `ANTHROPIC_API_KEY` and cannot be fully verified without it.
> Step 1 (threshold detection) was verified without an API key (total 1 212 tokens > 1 000 limit).

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Compression triggered; token count reduced | ✅ PASS (1 212 → 961 tokens) |
| 2 | Compression log lines present; no failure warnings | ✅ PASS |
| 3 | All sections retain non-empty content | ✅ PASS |

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
| 2026-04-18 | mkhomutov | Windows 11 | Partial | Step 1 (threshold detection): 1 212 tokens > 1 000 limit, PASS. Steps 2–3 require `ANTHROPIC_API_KEY` (not set). Doc fixes: `set_section` → `add_section(ContextSection(...))`, `_sections.values()` → `total_tokens()`, `_sections.items()` → iterate list directly. |
| 2026-04-18 | mkhomutov | Windows 11 | Partial | Retest — Step 1 confirms 1 212 tokens > 1 000 limit. API fixed scripts verified correct. Steps 2–3 still require `ANTHROPIC_API_KEY`. |
| 2026-04-18 | mkhomutov | Windows 11 | **PASS** | Full run with `ANTHROPIC_API_KEY` set. Steps 1–3 all pass: 1 212 → 961 tokens, compression log lines present, all sections non-empty. Two bugs found and fixed: `LLMClient()` requires `LLMClient(AnthropicProvider())`; default `compression_model` was `claude-haiku-4` (non-existent) → corrected to `claude-haiku-4-5` in `agents/memory/working.py`. |

---

## Notes

- `ANTHROPIC_API_KEY` is required. Without it, `compress_if_needed()` will raise an authentication
  error on the first LLM call; the section content will be preserved unchanged.
- The script calls `compress_if_needed()` directly and awaits it, so compression is complete
  before the token-count assertions. No sleep is needed — and the original `sleep(10)` has been
  removed. If you switch to the background-task API (`try_start_compression`), use
  `await mem.await_pending_compression()` instead of sleeping.
