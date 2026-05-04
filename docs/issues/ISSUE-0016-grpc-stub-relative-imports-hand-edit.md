---
id: ISSUE-0016
summary: agents/generated/*_pb2_grpc.py imports must be hand-patched to relative form after every `make proto` regen
status: open
severity: low
area: build/proto
created: 2026-05-04
refs:
  - docs/rfcs/0011-pr-plan.md
---

## Summary

`grpc_tools.protoc` emits top-level imports in the generated
`*_pb2_grpc.py` files:

```python
import task_pb2 as task__pb2
```

That form only resolves when `agents/generated/` is on `sys.path`. The
repo imports these stubs as `persatrix_agents.generated.*` (see the
`pyproject.toml` package-dir mapping under `agents/`), so mypy and the
runtime require the relative form:

```python
from . import task_pb2 as task__pb2
```

Every other `*_pb2_grpc.py` in `agents/generated/` already uses the
relative form. After PR #246's proto regeneration, two stubs
(`task_pb2_grpc.py`, `log_service_pb2_grpc.py`) reverted to the top-level
form and broke CI mypy with:

```
generated/task_pb2_grpc.py:6: error: Cannot find implementation or
  library stub for module named 'task_pb2'  [import-not-found]
```

The fix landed as a hand-patch on PR #246. The same hand-patch will be
required after every future `make proto` run unless the `Makefile`
target is updated.

## Context

Captured during PR #246 deep review (Should-fix #5). Noted in PR
description as a pre-existing project quirk and out of scope for the
proto + RPC change itself.

## Impact

- Each proto regen PR ships a CI-failing commit until the imports are
  hand-patched.
- New contributors regenerating stubs locally hit an import error that
  is not obvious from the protoc output.
- The hand-patch is forgettable; a future regen could land on `main`
  with broken imports if the author skips the `make lint` step.

## Proposed fix / investigation path

Two viable options, in order of preference:

1. **Post-process the generated files in the `make proto` target.** A
   small `sed` (or Python) post-step that rewrites
   `^import (\w+_pb2) as \1__pb2$` → `from . import \1 as \1__pb2`
   across `agents/generated/*_pb2_grpc.py`. Mirrors the convention
   already enforced manually.
2. **Adopt `protoc-gen-python_betterproto`** (or
   `mypy-protobuf` with a stub-emitter that respects package mapping).
   Larger blast radius — would also affect the `*_pb2.py` stubs and the
   public import surface; defer unless option 1 proves brittle.

Add a regression check (one-line `grep -L 'from \.' agents/generated/*_pb2_grpc.py`
in CI) that fails if any stub uses the top-level form.

## Notes

> 2026-05-04 — initial capture during PR #246 deep review (Should-fix
> #5). Hand-patch landed in PR #246 commit
> "fix(agents): use relative imports in regenerated grpc stubs".
