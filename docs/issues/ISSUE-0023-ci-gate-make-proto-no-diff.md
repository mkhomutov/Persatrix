---
id: ISSUE-0023
summary: CI does not gate on `make proto && git diff --exit-code`; orphan .proto files and hand-edits to generated stubs can drift from source
status: open
severity: low
area: ci
created: 2026-05-04
refs:
  - Makefile
  - .github/workflows/ci.yml
  - docs/issues/ISSUE-0016-grpc-stub-relative-imports-hand-edit.md
  - docs/issues/ISSUE-0017-task-pb2-pyi-hand-maintained.md
---

## Summary

The repo's CI does not run `make proto` and assert that the working
tree is clean afterwards. Three classes of drift therefore ship green:

1. An orphan `.proto` file (one that was deleted from the source tree
   but whose generated stubs survive in `internal/generated/` or
   `agents/generated/`).
2. A hand-edit to a generated `*.pb.go` / `*_pb2.py` / `*_pb2_grpc.py`
   that does not survive a regen.
3. A `.proto` change that the contributor forgot to regenerate before
   committing — the source and the stubs diverge until the next
   contributor runs `make proto` locally.

## Context

Captured during PR #246 deep review (Nice-to-have #4). PR 3 of RFC 0011
deleted `proto/agent_message.proto` and the entire
`internal/generated/msgpb/` package in the same diff. The deletion was
correct, but no automated gate would have caught a missed file
(verified manually via repo-wide grep). Companion to
[ISSUE-0016](ISSUE-0016-grpc-stub-relative-imports-hand-edit.md) and
[ISSUE-0017](ISSUE-0017-task-pb2-pyi-hand-maintained.md), which both
require post-regen hand edits — a clean-tree gate would force those
edits into a documented script rather than folk knowledge.

## Impact

- Wire-shape source-of-truth ambiguity: is `proto/*.proto` authoritative,
  or are the generated stubs? A drift makes the answer "neither".
- Reviewers must manually grep for orphan stubs and hand-edits on every
  proto-touching PR. Cost compounds across RFC 0011 PRs 4–8.

## Proposed fix / investigation path

1. Add a CI job (or a step in the existing `lint` job) that runs:
   ```
   make proto
   git diff --exit-code -- proto/ internal/generated/ agents/generated/
   ```
   Failure indicates either (a) a forgotten `make proto` run or
   (b) a hand-edit that does not survive regen.
2. To make this gate viable, ISSUE-0016 (`_pb2_grpc.py` relative-import
   hand-edits) and ISSUE-0017 (`task_pb2.pyi` hand-maintained) must
   first be resolved by configuring `protoc` to emit the correct shape
   directly. Until then, the gate would false-fail on every PR.
3. Document the hand-edit fallback in
   [docs/development-workflow.md](../development-workflow.md) so
   contributors can reproduce the post-regen state locally.

## Notes

> 2026-05-04 — initial capture during PR #246 deep review (NTH-4).
> Blocked on ISSUE-0016 + ISSUE-0017 — sequence those first or accept
> the gate will require a one-off `make proto-clean-edits` step that
> applies the known hand-patches.
