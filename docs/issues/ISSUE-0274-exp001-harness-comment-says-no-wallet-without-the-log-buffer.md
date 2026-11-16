---
id: ISSUE-0274
summary: "A comment in the EXP-001 harness says the orchestrator's log buffer must start or the orchestrator serves no wallet. That stopped being true when ISSUE-0204 was fixed. The harness folder is not changed during the experiment's scoring period, so the comment waits."
status: open
severity: low
area: evaluators
created: 2026-11-16
refs:
  - evaluators/exp001/processes.py
  - docs/issues/ISSUE-0204-grpc-server-starts-only-if-the-log-buffer-did.md
---

# ISSUE-0274: An EXP-001 harness comment says "no wallet" without the log buffer

## Summary

`orchestrator_process` in `evaluators/exp001/processes.py` sets
`PERSATRIX_LOGBUFFER_DIR` for the orchestrator it starts, under a comment
that says the log buffer must start "or the orchestrator serves no
wallet and every adviser's model call is refused". That described the
fault of
[ISSUE-0204](ISSUE-0204-grpc-server-starts-only-if-the-log-buffer-did.md).
Since its fix the wallet is served whether or not the buffer starts.

## Context

The setting itself is still right: it keeps a deployment's logs inside
the deployment's own folder. Only the reason given for it is stale. The
harness's behaviour does not change: it reads the orchestrator's log for
the two lines that show the wallet is served (`wallet lease enforcement
initialized` and `gRPC server listening`), and both are still logged,
from the same file, when the wallet is served.

The folder `evaluators/` is not changed while EXP-001 is in its blinded
scoring period, so the fix for ISSUE-0204 left the file alone.

## Impact

A reader of the harness is told of a failure that can no longer happen.

## Proposed fix / investigation path

Once the scoring period has ended, reword the comment: the setting
keeps the deployment's logs in its own folder.

## Notes

> 2026-11-16 — filed with the fix for
> [ISSUE-0204](ISSUE-0204-grpc-server-starts-only-if-the-log-buffer-did.md).
> Severity proposed from the tracker's precedent for a stale comment
> (low); not ruled by the maintainer.
