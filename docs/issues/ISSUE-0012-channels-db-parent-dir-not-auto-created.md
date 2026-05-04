---
id: ISSUE-0012
summary: --channels-db default lands on data/channels.db but parent dir is not ensured; fresh checkout silently degrades to 503
status: resolved
severity: low
area: cmd/orchestrator
created: 2026-05-04
closed: 2026-05-04
closed_pr: 246
refs:
  - docs/rfcs/0011-channels.md
---

## Summary

`initChannels()` in `cmd/orchestrator/channels.go` opens the SQLite store
at the path supplied via `--channels-db` (default `data/channels.db`). It
does not call `os.MkdirAll(filepath.Dir(dbPath), 0o755)` first. On a fresh
checkout where `data/` is gitignored and not yet created, SQLite open
fails and the channels subsystem silently degrades to 503 across all
seven REST endpoints (logged at WARN, not Fatal — by design).

## Context

Captured during PR #245 deep review (Nice-to-have #1). The Info/Warn split
for the YAML config helps operators distinguish "no config" from
"misconfigured config", but a missing parent directory is a different
class of footgun and currently falls into the WARN bucket.

## Impact

- Operator footgun on first run. The 503 response with no further
  guidance is hard to diagnose.
- Does not affect production deployments (operators provision the path
  explicitly), but degrades the local-dev experience.

## Proposed fix / investigation path

Pick one:

1. Auto-create the parent directory: `os.MkdirAll(filepath.Dir(dbPath),
   0o755)` before `sql.Open`. Preferred — least-surprise default.
2. Document the bootstrap step in `docs/development-workflow.md` and the
   v0.3.0 release-checklist; keep the WARN. Cheaper but less
   operator-friendly.

Either way, the WARN message should mention the resolved absolute path so
the operator can tell whether the path is wrong or the dir is missing.

## Notes

> 2026-05-04 — initial capture during PR #245 review (Nice-to-have #1).
