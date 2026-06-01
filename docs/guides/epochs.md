# Run/Test-Isolation Epochs (ISSUE-0085)

> **Operator guide.** How to isolate a run's persona memory with the `epoch`
> axis — the `PERSATRIX_EPOCH` process knob and the per-invocation `--epoch`
> override on the dispatch-bearing verbs.

**Status**: ✅ Shipped in v0.3.5 (RFC 0031 epoch axis,
[ISSUE-0085](../issues/ISSUE-0085-epoch-axis-run-isolation.md) closed). The
storage + strict-equality filter + gRPC rail (PRs 2–4), the `--epoch` operator
surface (PR 5), and the end-to-end run-isolation acceptance gate
([`test_epoch_run_isolation.py`](../../tests/integration/test_epoch_run_isolation.py),
PR 6 closeout) are all merged
([#472](https://github.com/mkhomutov/Persatrix/pull/472)–[#478](https://github.com/mkhomutov/Persatrix/pull/478)).

## TL;DR

```bash
PERSATRIX_EPOCH=ci-run-5 make run            # whole orchestrator boots under one epoch
persatrix chat support-bot --epoch ci-run-5  # one invocation under an explicit epoch
```

The effective epoch is resolved by a two-layer precedence:

```text
--epoch flag  >  PERSATRIX_EPOCH env  >  live  (the orchestrator boot default)
```

## What an epoch is

An **epoch** is the run/test-isolation axis: it answers *"which logical run
wrote this row?"* A fresh epoch sees **none** of a prior run's persona memory —
episodes, relationships, *and* person-facts all reset at once. That is the
structural half of the F-3 fix a fresh channel name alone cannot deliver:
relationships and person-facts are keyed on the participant, so a rerun reusing
`--user alice` still inherits old trust until the epoch isolates it.

Epoch is **strict-equality** isolation: there is **no `legacy` carve-out and no
`*` wildcard** (contrast the [session axis](sessions.md), whose carve-out exists
*for* continuity). Production never changes it — every untagged deployment runs
under the default `live` epoch, so behaviour is unchanged. CI bumps it per job.

## Epoch vs. session vs. `make reset`

| Axis | Question | Default | Carve-out |
|------|----------|---------|-----------|
| [`session`](sessions.md) | which room / conversation? | `legacy` | `legacy` ∪ `*` opt-in |
| `epoch` | which test run / logical branch? | `live` | none — strict equality |

`make reset` wipes the whole volume — **all** epochs and sessions at once — so it
cannot express the isolated-but-coexisting worlds an epoch gives CI. Reach for an
epoch to isolate one run; `make reset` is still the full nuke. See the design
home, [Memory Scope Axes §Epoch](../memory-scope-axes.md#epoch--the-testrun-isolation-axis).

## The process knob: `PERSATRIX_EPOCH`

The orchestrator reads `PERSATRIX_EPOCH` once at boot and emits it on every
dispatch as the `persatrix-epoch` gRPC header; the persona side re-establishes an
`epoch_scope` from it so recall and writes filter by that epoch. Unset defaults
to `live` (an INFO line at boot records the fallback). A value outside
`[A-Za-z0-9_-]` is accepted verbatim with a WARN. Set it in the orchestrator's
environment — locally via `make run`, or under Docker on the orchestrator
service in your compose env:

```bash
PERSATRIX_EPOCH=ci-$GITHUB_RUN_ID make run
```

## The per-invocation override: `--epoch`

`--epoch <id>` on the dispatch-bearing verbs (`chat`, `channel send`,
`channel reply`) overrides the boot epoch for that one invocation — parity with
[`--session`](sessions.md), precedence above the env. Useful for a one-off
isolated run against an orchestrator already serving the `live` epoch:

```bash
persatrix chat support-bot --epoch trial-7
persatrix channel send planning "kickoff" --epoch trial-7
```

Resolution is a bare flag-or-env precedence — unlike a session, an epoch has no
`new` / `use` lifecycle and no active-epoch pointer file, so there is nothing to
mint or activate. The id must be printable ASCII (it rides a gRPC metadata
header); a control or non-ASCII byte is rejected with a `BAD_REQUEST`.

## See also

- [Memory Scope Axes §Epoch](../memory-scope-axes.md#epoch--the-testrun-isolation-axis) — the design rationale.
- [Persona-Memory Sessions](sessions.md) — the orthogonal room-continuity axis.
- [ISSUE-0085](../issues/ISSUE-0085-epoch-axis-run-isolation.md) — the tracking issue.
- [RFC 0031 epoch PR plan](../rfcs/0031-epoch-pr-plan.md) — the implementation sequence.
