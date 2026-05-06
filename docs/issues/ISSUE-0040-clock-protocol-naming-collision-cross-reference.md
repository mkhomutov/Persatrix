---
id: ISSUE-0040
summary: "agents.clock.Clock and agents.memory.interactions.Clock collide in name but not shape; add a cross-reference comment in interactions.py"
status: open
severity: low
area: agents
created: 2026-05-06
refs:
  - agents/clock.py
  - agents/memory/interactions.py
  - docs/rfcs/0021-persona-temporal-awareness.md
  - docs/rfcs/0021-pr-plan.md
---

## Summary

Two `Clock` Protocols now coexist with the same class name and
incompatible shapes:

- [`agents/clock.py::Clock`](../../agents/clock.py#L18-L36) — has
  `now() -> float` and `now_iso() -> str`.
- [`agents/memory/interactions.py::Clock`](../../agents/memory/interactions.py#L96)
  — callable Protocol with shape `def __call__(self) -> float`.

`agents/clock.py` already documents the collision in its module
docstring ([clock.py:18-26](../../agents/clock.py#L18-L26)). The
sibling at `interactions.py` does not — a `grep -n "class Clock"`
lands on both files, but only one half of the cross-reference is
in place.

## Context

Captured during the PR #256 deep review (Finding M4). RFC 0021
PR 2 plans to alias the two surfaces (the narrowest path being
"tracker accepts `Clock | Callable[[], float]`, persona-runtime
passes `clock_instance.now`"). Until that aliasing lands, a
careless `from agents.clock import Clock` in tracker code (or
vice versa) type-checks successfully — mypy keeps the two
Protocols distinct — but breaks at runtime when someone calls
the Protocol as a function or accesses `.now` on a callable.

## Impact

- A mypy-clean import-site bug that fails only when the seam is
  exercised. The class of failure is exactly the kind that
  Protocols are supposed to prevent.
- Documentation that already exists in one file (`agents/clock.py`)
  but not in its mirror (`agents/memory/interactions.py`) creates a
  one-sided lookup — the reader who lands on `interactions.py`
  first has no signal that another `Clock` exists.

## Proposed fix / investigation path

Add a one-line cross-reference comment near the
`class Clock(Protocol)` declaration in
[`agents/memory/interactions.py:96`](../../agents/memory/interactions.py#L96),
matching the tone of the existing note in `agents/clock.py`:

```python
class Clock(Protocol):
    """Callable Protocol for monotonic-ish wall-clock reads.

    Naming-collision note: `agents.clock.Clock` (added in RFC 0021 P1)
    is a *different* Protocol with a `.now()` method, not a callable.
    The two are aliased in RFC 0021 P2 — until then, do not import
    `Clock` from the wrong module. See agents/clock.py docstring for
    the full plan.
    """

    def __call__(self) -> float: ...
```

Optional: a unit test that imports both `Clock` symbols under
distinct aliases and asserts they are not interchangeable, e.g.
via `isinstance` checks. Probably overkill for this surface; the
docstring is enough.

## Notes

> 2026-05-06 — captured during PR #256 review (Finding M4, marked
> "minor follow-up, optional"). Not a merge blocker for #256;
> tracked for PR 3 review follow-ups. Will be obviated once PR 2
> aliases the two seams.
