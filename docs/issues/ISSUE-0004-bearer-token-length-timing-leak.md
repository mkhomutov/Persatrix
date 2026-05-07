---
id: ISSUE-0004
summary: "validBearerToken length-mismatch path leaks expected token length via response timing"
status: resolved
severity: low
area: server
created: 2026-05-04
closed: 2026-05-07
refs:
  - docs/rfcs/0009-security-sandboxing.md
---

## Summary

`validBearerToken` compares `len(supplied) != len(expected)` before
`subtle.ConstantTimeCompare`. The early-exit length check makes response
timing differ between "wrong length" and "wrong content", letting a remote
attacker probe the expected token length.

## Context

- File: [internal/server/agent_handlers.go](../../internal/server/agent_handlers.go) → `validBearerToken`.
- An in-source comment acknowledges the length leak as deliberate. The fix
  below removes it without losing the constant-time property.

## Impact

Low — token length is mildly sensitive (narrows a brute-force search space).
Production deployments should rely on the rate limiter on `/api/v1/*` to
make brute-forcing infeasible regardless. Worth a quick fix when the file is
next touched.

## Proposed fix / investigation path

Hash both inputs (e.g. `sha256.Sum256`) and `subtle.ConstantTimeCompare` the
fixed-size digests. The lengths are then always equal and the early-exit
branch goes away.

```go
sup := sha256.Sum256([]byte(suppliedToken))
exp := sha256.Sum256([]byte(expectedToken))
return subtle.ConstantTimeCompare(sup[:], exp[:]) == 1
```

## Notes

> 2026-05-04 — captured during PR #244 deep review (R3, finding L-R3-02).
