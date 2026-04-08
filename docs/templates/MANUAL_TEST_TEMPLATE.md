# Manual Test [ID]: [Feature/Test Title]

**Test ID**: `MT-[AREA]-[NNN]` (e.g., `MT-WORKFLOW-001`, `MT-AGENT-042`)  
**Feature Area**: [Workflow / Agent / CLI / Orchestrator / Config / Integration]  
**Version**: 1.0  
**Created**: YYYY-MM-DD  
**Last Updated**: YYYY-MM-DD  
**Status**: Draft | Active | Deprecated

---

## Overview

**Purpose**: [Brief 1-2 sentence description of what this test validates]

**Scope**: [What is tested]

**Out of Scope**: [What is NOT tested in this document]

---

## Related Documentation

**Feature Documentation**:
- [Link to architecture doc]
- [Link to spec section]

**Related Automated Tests**:
- Unit tests: `[path/to/test_file.py::test_function_name]`
- Integration tests: `[tests/integration/test_file.py]`

---

## Preconditions

### System Requirements

**Operating Systems**:
- ☐ Windows 10/11 (x64)
- ☐ macOS 12.0+ (Intel/Apple Silicon)
- ☐ Linux (Ubuntu 22.04+)

**Dependencies Installed**:
- Go 1.23+: `go version`
- Python 3.11+: `python3 --version`
- Rust: `rustc --version`

### Application State

**Orchestrator Setup**:
- ☐ Orchestrator running (`make run`)
- ☐ Agent(s) registered and connected
- ☐ Config files valid (`make validate`)

### Test Data

**Fixtures Used**:
- [Fixture name/path from `tests/fixtures/`]
- [Manual setup steps if needed]

---

## Test Procedure

### Step 1: [Action Description]

**Action**: [What the tester does]

**Expected Result**: [What should happen]

**Verification**:
- [ ] [Specific check 1]
- [ ] [Specific check 2]

### Step 2: [Action Description]

**Action**: [What the tester does]

**Expected Result**: [What should happen]

**Verification**:
- [ ] [Specific check 1]

---

## Expected Results Summary

| Step | Expected Outcome | Pass/Fail |
|------|-----------------|-----------|
| 1 | [Expected] | ☐ |
| 2 | [Expected] | ☐ |

---

## Edge Cases & Error Scenarios

### Edge Case 1: [Description]

**Scenario**: [What unusual condition is tested]

**Expected Behavior**: [How the system should handle it]

---

## Test Results

| Date | Tester | OS | Result | Notes |
|------|--------|----|--------|-------|
| YYYY-MM-DD | [Name] | [OS] | Pass/Fail | [Notes] |

---

## Notes

- [Any additional observations or known issues]
