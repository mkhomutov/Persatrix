---
id: ISSUE-0006
summary: "envIntDefault silently falls back on invalid/zero values; log WARN when SECURITY_RATE_LIMIT_* is set but unparseable"
status: open
severity: low
area: cmd/orchestrator
created: 2026-05-04
refs:
  - docs/rfcs/0009-security-sandboxing.md
---

## Summary

`envIntDefault` in `cmd/orchestrator/ratelimit.go` returns the default when
the env value is unparseable OR `<= 0`. Operators get no signal that their
override was rejected.

## Context

- File: [cmd/orchestrator/ratelimit.go](../../cmd/orchestrator/ratelimit.go) → `envIntDefault`.
- Disabling the limiter requires `SECURITY_RATE_LIMIT_ENABLED=false`, not
  `SECURITY_RATE_LIMIT_CALLS=0` — but a typo in the latter currently boots
  with the default rate limit and no log line says so.

## Impact

Operator UX. A misconfigured deployment runs with the wrong limits silently.

## Proposed fix / investigation path

When the env var is non-empty but `strconv.Atoi` fails or the value is
`<= 0`, log a WARN naming the var and the rejected value, then fall back to
the default. Optional: surface as a `rate_limit.config_invalid` audit event.

## Notes

> 2026-05-04 — captured during PR #244 deep review (R3, finding L-R3-05).
