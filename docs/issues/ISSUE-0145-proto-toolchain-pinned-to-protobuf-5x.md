---
id: ISSUE-0145
summary: "The Python protobuf toolchain is frozen at protobuf 5.x with grpcio-tools pinned to 1.71.2 — every later grpcio-tools requires protobuf>=6, so the two move together or not at all, and lifting the pair means regenerating every committed stub with the CI-pinned toolchain rather than widening a range"
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
`make proto-python-check` fails the build when they drift from `proto/`. The
generator is now pinned exactly — `grpcio-tools==1.71.2` — so what emits those
files says so.

What remains is that the toolchain is frozen. Every `grpcio-tools` release
above 1.71.2 requires `protobuf>=6`, and the runtime caps `protobuf<6`, so the
pair moves together or not at all. Upgrading is a coordinated change that
regenerates every committed stub, not a range widening.

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

Resolving that showed the range was never real. Under `protobuf<6`, **every**
`grpcio-tools` from 1.72.1 to 1.83.1 is unresolvable — they all require
`protobuf>=6` — so `>=1.71.2,<2` could only ever resolve to 1.71.2. The
declared range implied a choice that did not exist, and the actual constraint
lived on a different package. `grpcio-tools` is therefore pinned exactly as of
this file, verified by regenerating every stub and getting a byte-identical
tree.

## Impact

Nothing is broken today: the caps hold and the committed stubs match
`proto/`. Two costs accrue.

- **The Python runtime stays on protobuf 5.x** while 6.x and 7.x exist, and
  `grpcio-tools` is frozen at 1.71.2 with it. Both are build- and
  wire-critical, so this is not a peripheral freeze.
- **The freeze is now visible but still unscheduled.** The pin and the cap
  comments explain themselves, and Dependabot will no longer propose the
  jump — which also means nothing recurring will raise it. Same shape as
  [ISSUE-0144](ISSUE-0144-anthropic-sdk-pinned-below-1x.md).

## Proposed fix / investigation path

The upgrade is not a bump. It needs, in one change:

1. Raise the `protobuf` cap and the `grpcio-tools` pin together — they are a
   matched pair, and pip cannot resolve a mixed state at all.
2. Regenerate every Python stub **with the CI-pinned toolchain** — `CLAUDE.md`
   pins protoc and the plugin versions precisely because a newer local
   toolchain emits stubs that fail the staleness gate.
3. Move `mypy-protobuf` off the exact `3.6.0` pin in the same change and
   regenerate the `.pyi`, per its existing comment.
4. Verify `make proto-python-check` **and** `make proto-check` (Go stubs and
   orphan detection, ISSUE-0023) are green together.
5. Drop the `protobuf`, `mypy-protobuf` and `grpcio-tools` entries from
   `ignore:` in `.github/dependabot.yml`.

The prerequisite work is already done: the generator is pinned explicitly and
both caps carry their reasoning, so the upgrade starts from a state that says
what it is rather than one that has to be re-derived.

## Notes

> 2026-09-07 — captured after #879 re-proposed what #870 had already been
> reverted for. Landed with this file: `grpcio-tools` pinned exactly (verified
> by regenerating every stub for a byte-identical tree), both cap comments
> stating what they hold, and Dependabot `ignore` rules for the `protobuf`,
> `mypy-protobuf` and `grpcio-tools` updates that cannot resolve under the cap.
> Those rules are deliberate and should stay until the upgrade above is done.
> Same shape as [ISSUE-0144](ISSUE-0144-anthropic-sdk-pinned-below-1x.md) — a
> freeze held by a rule that also suppresses the reminder to lift it. Not yet
> slotted to a version.
