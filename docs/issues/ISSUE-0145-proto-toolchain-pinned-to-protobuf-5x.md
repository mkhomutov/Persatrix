---
id: ISSUE-0145
summary: "Reproducible Python protobuf stubs rest on the `protobuf<6` cap indirectly constraining which grpcio-tools pip resolves — an implicit coupling no comment or gate states, so a widening that looks like a dependency bump silently restages every generated file, as PRs #870 and #879 each did"
status: open
severity: medium
area: build/proto
created: 2026-09-07
refs:
  - agents/pyproject.toml
  - .github/dependabot.yml
  - Makefile
---

## Summary

`agents/generated/*_pb2.py`, `*_pb2_grpc.py` and `*.pyi` are committed, and
`make proto-python-check` fails the build when they drift from `proto/`. What
keeps them reproducible is not a pin on the generator. It is the
`protobuf>=5.28.0,<6` cap, which indirectly constrains which `grpcio-tools`
pip may resolve inside `>=1.71.2,<2` — and `grpcio-tools` is the thing that
actually emits the stubs.

Nothing states that coupling. It is load-bearing and invisible.

## Context

Found twice, the same way. The first pip sweep
([PR #870](https://github.com/mkhomutov/Persatrix/pull/870)) widened
`protobuf` to `<8` and moved `mypy-protobuf` from `==3.6.0` to `5.1.0`, and
the Python job went red with **every** generated file stale:

```
✗ agents/generated/log_service_pb2.py is stale; run: make proto-python
✗ agents/generated/log_service_pb2_grpc.py is stale
✗ agents/generated/task_pb2.py is stale
… and the three .pyi
```

The tell is the breadth. A `mypy-protobuf` bump alone can only restage the
`.pyi`; the `_pb2.py` and `_pb2_grpc.py` going stale as well means the
*runtime* generator changed. It did, because lifting the `protobuf` cap let
pip take the newest `grpcio-tools` in range rather than one compatible with
protobuf 5.

Both edits were reverted in #870. Within hours of that merge, Dependabot
opened [PR #879](https://github.com/mkhomutov/Persatrix/pull/879) proposing
exactly the same two changes, which failed on exactly the same gate. That is
the recurrence this issue exists to stop: a refusal held only in a reviewer's
memory is not a control.

`mypy-protobuf`'s own comment in `agents/pyproject.toml` already says the
right thing — bump deliberately, regenerate the `.pyi`, commit in the same
change. The `protobuf` cap carries no equivalent note, and it is the one
doing the load-bearing work.

## Impact

Nothing is broken today: the caps hold and the committed stubs match
`proto/`. Two costs accrue.

- **The Python runtime stays on protobuf 5.x** while 6.x and 7.x exist, and
  `grpcio-tools` is effectively frozen with it.
- **The coupling is undocumented**, so the next person to read
  `protobuf>=5.28.0,<6` sees an ordinary compatibility cap and has no way to
  know that relaxing it restages generated files. That is precisely the
  mistake Dependabot made mechanically, twice.

## Proposed fix / investigation path

The upgrade is not a bump. It needs, in one change:

1. Raise the `protobuf` cap and the `grpcio-tools` floor together, deciding
   the pair deliberately rather than letting pip resolve it.
2. Regenerate every Python stub **with the CI-pinned toolchain** — `CLAUDE.md`
   pins protoc and the plugin versions precisely because a newer local
   toolchain emits stubs that fail the staleness gate.
3. Move `mypy-protobuf` off the exact `3.6.0` pin in the same change and
   regenerate the `.pyi`, per its existing comment.
4. Verify `make proto-python-check` **and** `make proto-check` (Go stubs and
   orphan detection, ISSUE-0023) are green together.
5. Drop the `protobuf` and `mypy-protobuf` entries from `ignore:` in
   `.github/dependabot.yml`.

Worth doing regardless of the upgrade: state the coupling in
`agents/pyproject.toml` next to the `protobuf` cap, so the cap explains itself
the way the `mypy-protobuf` pin already does. A stronger form would pin
`grpcio-tools` exactly, making the generator explicit rather than a
consequence of a cap on a different package.

## Notes

> 2026-09-07 — captured after #879 re-proposed what #870 had already been
> reverted for. Dependabot `ignore` rules for both packages' majors landed
> with this file; they are deliberate and should stay until the upgrade above
> is done. Same shape as [ISSUE-0144](ISSUE-0144-anthropic-sdk-pinned-below-1x.md)
> — a cap held by a rule that also suppresses the reminder to lift it. Not yet
> slotted to a version.
