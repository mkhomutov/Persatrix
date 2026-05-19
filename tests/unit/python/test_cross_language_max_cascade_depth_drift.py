"""Cross-language drift pin for ``max_cascade_depth`` defaults.

PR #319 deep review finding 5.3 ("the two ``5``s live in two languages
with no automated drift check") motivated this file.

The cooperative-path cascade-depth backstop has two enforcement points:

* **Primary** — the Go orchestrator's ``ChannelRouter`` clamps and drops
  on ``cascade_depth >= max_cascade_depth`` (RFC 0011 amendment
  "Cascade-depth wire propagation", PR 2 / PR #319).
* **Defense-in-depth** — the Python ``EventDispatcher`` mirrors the cap
  on the persona-side fanout (``agents/dispatch.py``).

Both sides default to ``5``. The doc-comment on
``internal/defaults/defaults.go`` cross-references ``agents/dispatch.py``
and vice versa, stating ``keep aligned`` — but until this file landed,
nothing pinned the equality. If one side drifts (Python rises to ``8``
while Go stays at ``5``), the backstop fires on routine cap-bound
traffic rather than only on a primary-enforcement regression — exactly
the failure mode the doc-comments warn against.

The test parses ``internal/defaults/defaults.go`` for the ``const
DefaultMaxCascadeDepth = N`` declaration and reflects the
``EventDispatcher.__init__`` signature for its ``max_cascade_depth``
keyword default. Both sides need an intentional edit for a deliberate
change; a drift caused by a one-sided edit lands here as a red test.

Parsing the Go source as text (instead of e.g. invoking ``go run``)
keeps the test runnable in any environment that already runs the
Python unit suite — no Go toolchain dependency, no build artefact
plumbing.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from agents.dispatch import EventDispatcher

_DEFAULTS_GO = Path("internal/defaults/defaults.go")

# Captures `const DefaultMaxCascadeDepth = <int>` on a single line. The
# const declaration in `internal/defaults/defaults.go` is intentionally
# single-line (no parenthesised group), so this anchored form is the
# narrowest pattern that still tolerates leading whitespace and trailing
# comments. A future refactor that moves the constant into a `const ( ... )`
# block would force a deliberate update here — that is intended: the
# parse rule is part of the contract.
_GO_CONST_PATTERN = re.compile(
    r"^\s*const\s+DefaultMaxCascadeDepth\s*=\s*(\d+)\s*(?://.*)?$",
    re.MULTILINE,
)


def _go_default_max_cascade_depth() -> int:
    """Parse ``DefaultMaxCascadeDepth`` out of the Go source.

    Returns the integer literal. Raises ``pytest.fail`` (rather than
    returning ``None``) on a parse miss so a refactor that hides the
    constant lands as an actionable test failure instead of a silent
    ``None``-vs-``int`` ``AssertionError``.
    """
    src = _DEFAULTS_GO.read_text(encoding="utf-8")
    match = _GO_CONST_PATTERN.search(src)
    if match is None:
        pytest.fail(
            f"could not find `const DefaultMaxCascadeDepth = <int>` in "
            f"{_DEFAULTS_GO}. If the constant was moved into a "
            f"`const ( ... )` block or renamed, update the parse rule "
            f"in this test to match the new shape — the cross-language "
            f"drift pin is part of the contract.",
        )
    return int(match.group(1))


def _python_default_max_cascade_depth() -> int:
    """Reflect ``EventDispatcher.__init__``'s ``max_cascade_depth`` default."""
    sig = inspect.signature(EventDispatcher.__init__)
    return sig.parameters["max_cascade_depth"].default


def test_go_and_python_defaults_agree():
    """The two defaults MUST be equal.

    A drift here is operationally costly: if Python > Go, the Python
    defense-in-depth never fires on the orchestrator's intended cap
    boundary (silently dead-code on the backstop). If Python < Go, the
    Python side trips on traffic the Go side considers in-policy
    (false-positive drops on cap-bound traffic).

    Either direction is a bug; equality is the only safe state until
    the wire amendment grows an explicit "Python uses Go's value over
    the wire" handshake (out-of-scope for the v0.3.0 testing pass).
    """
    go_value = _go_default_max_cascade_depth()
    python_value = _python_default_max_cascade_depth()
    assert go_value == python_value, (
        f"max_cascade_depth default drifted: "
        f"Go ({_DEFAULTS_GO}) = {go_value}, "
        f"Python (agents.dispatch.EventDispatcher) = {python_value}. "
        f"One side was edited without the other. Update both — and "
        f"if the change is operator-visible, update the docs in "
        f"docs/guides/channels.md too."
    )


def test_go_default_matches_documented_value():
    """The Go default MUST be the documented ``5``.

    Independent of the cross-language equality test: pins the absolute
    value the operator-facing docs (``docs/guides/channels.md``
    §"Cascade-depth backstop") and the ``schemas/channel.schema.json``
    `default: 5` advertise. A change to the absolute value should also
    update those surfaces; this test surfaces the omission.
    """
    assert _go_default_max_cascade_depth() == 5
