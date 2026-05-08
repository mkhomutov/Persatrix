---
id: ISSUE-0017
summary: agents/generated/task_pb2.pyi is hand-maintained because the project's protoc invocation does not emit type stubs
status: resolved
severity: low
area: build/proto
created: 2026-05-04
closed: 2026-05-08
refs:
  - docs/rfcs/0011-pr-plan.md
  - docs/issues/ISSUE-0016-grpc-stub-relative-imports-hand-edit.md
---

## Summary

`agents/generated/task_pb2.pyi` carries hand-written type stubs for the
`persatrix.v1` proto messages (currently `TaskRequest`, `TaskResponse`,
`TaskProgress`, `ChatRequest`, `ChatResponse`, `ChannelMessageEvent`,
`TaskAck`, plus the health enums). The file is checked in as if it were
a generated artifact, but the repo's `make proto` target invokes
`grpc_tools.protoc` without `--mypy_out` / `--pyi_out`, so the stubs
have to be edited by hand whenever a `.proto` message gains, drops, or
renames a field.

## Context

Captured during PR #246 deep review (Should-fix #4 — second half;
companion to ISSUE-0016 for the parallel `_pb2_grpc.py` import quirk).
PR #246 added `ChannelMessageEvent` (8 fields) and `TaskAck` (2 fields);
both required hand edits to `task_pb2.pyi` to keep mypy honest. The PR
description flagged this as a known hygiene gap.

## Impact

- Every proto change ships with a hand-edited `.pyi` whose drift from
  the `.proto` is invisible to CI: mypy passes whether the stub is
  accurate or not. A stale stub silently masks real type errors in
  downstream agent code.
- Contributors regenerating stubs locally have no automated way to
  refresh the `.pyi`; the hand-edit step is folk knowledge.
- Each new wire-contract PR pays the same review cost (verify the `.pyi`
  matches the `.proto`).

## Proposed fix / investigation path

1. Add `mypy-protobuf` (or equivalent) to the agent's dev dependencies
   in `agents/pyproject.toml`.
2. Update the `proto` target in [Makefile](../../Makefile) to pass
   `--mypy_out=agents/generated` (and the matching grpc stub flag) so
   `*_pb2.pyi` files are emitted alongside `*_pb2.py`.
3. Delete the hand-maintained `task_pb2.pyi` and let the regen produce
   it; verify against existing imports in `agents/server_servicers.py`
   and the unit tests under `tests/unit/python/`.
4. Document the new dependency in
   [docs/development-workflow.md](../development-workflow.md) so the
   tool is discoverable.

The same regen pass would also resolve ISSUE-0016 if the relative-import
output is configured at the same time.

## Notes

> 2026-05-04 — initial capture during PR #246 deep review (Should-fix
> #4). Companion to ISSUE-0016. Both are out of scope for the proto +
> RPC change itself; tracked here so the next `make proto` consumer can
> address them as a single hygiene PR.

> 2026-05-08 — closed. Wired `mypy-protobuf>=3.5,<4` into
> `agents/pyproject.toml`'s `[project.optional-dependencies].dev` and
> added `--mypy_out=$(PROTO_PY_OUT)` to the `proto-python` target.
> Regenerated both `task_pb2.pyi` and `log_service_pb2.pyi` (the
> companion hand-maintained stub for the log shipper proto), introduced
> a `make proto-python-check` target that fails when the committed
> `.pyi` drifts from the generator output, and locked the gate in CI
> via the Python lint/test job. Parity is also pinned by
> `tests/unit/python/test_task_pb2_pyi_parity.py`. The 3.5–4.0 cap on
> `mypy-protobuf` is necessary because the 4.x line requires
> `protobuf>=6` while the runtime closure here pins `protobuf<6`; bump
> in lockstep with the protobuf cap.
