# Manual Test MT-MEMORY-001: Episodic Memory — Write and Recall Across Agent Restart

**Test ID**: `MT-MEMORY-001`
**Feature Area**: Memory
**Version**: 1.0
**Created**: 2026-04-18
**Last Updated**: 2026-04-18
**Status**: Active

---

## Overview

**Purpose**: Verify that episodes written to episodic memory are durably persisted in SQLite and
are recoverable via `recall()` after the `EpisodicMemory` instance is closed and re-opened,
simulating an agent restart.

**Scope**: `EpisodicMemory.store_episode()`, `recall()`, SQLite WAL persistence, FTS5 ranking.

**Out of Scope**: LLM-driven memory injection into prompts; working-memory summarisation.

---

## Related Documentation

**Feature Documentation**:
- [docs/rfcs/0005-persona-agent-memory.md](../rfcs/0005-persona-agent-memory.md)
- [agents/memory/episodic.py](../../agents/memory/episodic.py)
- [agents/memory/episodic_queries.py](../../agents/memory/episodic_queries.py)

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

- ☐ No orchestrator or agent processes required — this test exercises the library directly.
- ☐ `data/` directory exists (or will be created by the test script): `mkdir -p data`

---

## Test Procedure

### Step 1: Write Episodes to a Fresh Database

**Action**: Run the following Python script:

```bash
python3 - <<'EOF'
import asyncio
from persatrix_agents.memory.episodic import EpisodicMemory

DB = "data/mt-memory-001.db"

async def main():
    mem = EpisodicMemory("test-agent", db_path=DB)
    await mem.initialize()

    ids = []
    for i in range(3):
        ep_id = await mem.store_episode(
            summary=f"Resolved critical issue #{i+1} in the pipeline",
            context={"iteration": str(i+1), "area": "pipeline"},
            outcome="success",
            importance=0.7 + i * 0.1,
        )
        ids.append(ep_id)
        print(f"Stored episode {i+1}: {ep_id}")

    await mem.close()
    print("Database closed (simulating agent shutdown).")
    print("Episode IDs:", ids)

asyncio.run(main())
EOF
```

**Expected Result**: Three episodes stored; database closed without error.

**Verification**:
- [ ] Script exits 0
- [ ] Three "Stored episode N: <uuid>" lines printed
- [ ] File `data/mt-memory-001.db` exists on disk: `ls -lh data/mt-memory-001.db`

---

### Step 2: Confirm FTS5 Availability

**Action**: Check the agent startup log or the script output from Step 1 for the FTS5 notice:

```bash
python3 -c "
import asyncio
from persatrix_agents.memory.episodic import EpisodicMemory
async def main():
    m = EpisodicMemory('probe', db_path='data/mt-memory-001.db')
    await m.initialize()
    await m.close()
asyncio.run(main())
" 2>&1 | grep -i fts5
```

**Expected Result**: One of:
- `"FTS5 enabled for episodic memory"` — full-text search active.
- `"FTS5 not available — falling back to LIKE-based queries"` — fallback mode; recall still works.

**Verification**:
- [ ] One of the two messages above is present (or neither, if the message goes to a log file)
- [ ] Script exits 0

---

### Step 3: Recall Episodes After Restart

**Action**: Re-open the database (simulating an agent restart) and recall episodes:

```bash
python3 - <<'EOF'
import asyncio
from persatrix_agents.memory.episodic import EpisodicMemory

DB = "data/mt-memory-001.db"

async def main():
    mem = EpisodicMemory("test-agent", db_path=DB)
    await mem.initialize()

    episodes = await mem.recall(query="pipeline issue", limit=10)
    print(f"Recalled {len(episodes)} episode(s):")
    for ep in episodes:
        print(f"  [{ep.importance:.1f}] {ep.summary}")

    await mem.close()

asyncio.run(main())
EOF
```

**Expected Result**: All three episodes from Step 1 are returned.

**Verification**:
- [ ] `"Recalled 3 episode(s)"` printed
- [ ] All three summaries (`"Resolved critical issue #1/2/3 in the pipeline"`) present
- [ ] Episodes ordered by relevance score (most important / most relevant first)

---

### Step 4: Clean Up Test Database

**Action**: Remove the test database:

```bash
rm data/mt-memory-001.db
```

**Verification**:
- [ ] File removed

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | Three episodes stored; DB file created | ☐ |
| 2 | FTS5 status logged; no error | ☐ |
| 3 | All three episodes recalled after re-open | ☐ |
| 4 | Test DB cleaned up | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: FTS5 Not Available

**Scenario**: SQLite installation lacks FTS5 extension.

**Expected Behavior**: Warning logged; `recall()` falls back to `LIKE`-based search. All three
episodes are still returned (lower ranking fidelity is acceptable for this test).

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| | | | | |

---

## Notes

- This test does not require a running orchestrator or LLM API key.
- WAL mode is enabled automatically by `EpisodicMemory.initialize()`. If the test machine has
  read-only `/data`, adjust `DB` path to a writable location (e.g., `/tmp/mt-memory-001.db`).
