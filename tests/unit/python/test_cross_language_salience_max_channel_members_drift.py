"""Cross-language drift pin for the Tier B channel-size cap default.

PR #573 review finding ("the two ``20``s live in two languages with no
automated drift check") motivated this file, mirroring the established
``test_cross_language_max_cascade_depth_drift.py`` pattern (PR #319).

The RFC 0030 Tier B (v0.3.8) channel-size cap (TB6) has its default in
two languages:

* **Go** — ``const DefaultSalienceMaxChannelMembers`` in
  ``internal/channels/config.go``. ``LoadConfig`` and
  ``ChannelRouter.SetSalienceMaxChannelMembers`` both normalize a
  zero/absent ``salience_max_channel_members`` to this value, so the
  resolved cap the dispatcher stamps on the
  ``ChannelMessageEvent.salience_max_channel_members`` wire field is
  always positive.
* **Python** — ``DEFAULT_SALIENCE_MAX_CHANNEL_MEMBERS`` in
  ``agents/salience_bid.py``, the fallback the agent-side seam
  (``agents/persona_runtime/salience_gate.py``) applies when the inbound
  wire field is zero/absent (an "unknown" cap, i.e. a pre-v0.3.8
  publisher that never learned to send the field).

The Go doc-comment on ``DefaultSalienceMaxChannelMembers`` states it is
"kept in lock-step with the Python ``DEFAULT_SALIENCE_MAX_CHANNEL_MEMBERS``
... the two must agree" — but until this file landed, nothing pinned the
equality. Because the matched-version Go orchestrator always sends a
positive cap, the Python default is normally dormant; the drift bites a
*mixed-version* deployment (a pre-v0.3.8 publisher whose omitted field
makes the Python default load-bearing) or any future change that lets Go
send ``0`` on the wire. The documented contract deserves the same guard
the cascade-depth default carries.

The test imports the Python constant directly and parses
``internal/channels/config.go`` as text for the Go literal. Parsing the
Go source as text (instead of e.g. invoking ``go run``) keeps the test
runnable in any environment that already runs the Python unit suite — no
Go toolchain dependency, no build-artefact plumbing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agents.salience_bid import DEFAULT_SALIENCE_MAX_CHANNEL_MEMBERS

_CONFIG_GO = Path("internal/channels/config.go")

# Captures `const DefaultSalienceMaxChannelMembers = <int>` on a single line.
# The const declaration in `internal/channels/config.go` is intentionally
# single-line (no parenthesised group), so this anchored form is the
# narrowest pattern that still tolerates leading whitespace and trailing
# comments. A future refactor that moves the constant into a `const ( ... )`
# block would force a deliberate update here — that is intended: the parse
# rule is part of the contract.
_GO_CONST_PATTERN = re.compile(
    r"^\s*const\s+DefaultSalienceMaxChannelMembers\s*=\s*(\d+)\s*(?://.*)?$",
    re.MULTILINE,
)


def _go_default_salience_max_channel_members() -> int:
    """Parse ``DefaultSalienceMaxChannelMembers`` out of the Go source.

    Returns the integer literal. Raises ``pytest.fail`` (rather than
    returning ``None``) on a parse miss so a refactor that hides the
    constant lands as an actionable test failure instead of a silent
    ``None``-vs-``int`` ``AssertionError``.
    """
    src = _CONFIG_GO.read_text(encoding="utf-8")
    match = _GO_CONST_PATTERN.search(src)
    if match is None:
        pytest.fail(
            f"could not find `const DefaultSalienceMaxChannelMembers = <int>` in "
            f"{_CONFIG_GO}. If the constant was moved into a `const ( ... )` "
            f"block or renamed, update the parse rule in this test to match "
            f"the new shape — the cross-language drift pin is part of the "
            f"contract.",
        )
    return int(match.group(1))


def test_go_and_python_defaults_agree():
    """The two defaults MUST be equal.

    A drift here is silent: the matched-version Go orchestrator always
    normalizes the cap to a positive value before it rides the wire, so
    the Python default only governs an inbound event whose
    ``salience_max_channel_members`` is zero/absent (a pre-v0.3.8 publisher,
    or a future Go change that sends ``0``). If Python drifts above Go,
    such an event's bid runs on larger channels than the Go-side intent;
    if below, it is skipped sooner. Either way the cap boundary the two
    sides nominally share no longer matches.

    Equality is the only safe state until the wire contract grows an
    explicit "the agent always uses the orchestrator's value" handshake
    (out of scope for PR 2b — the field exists, but the seam still falls
    back to its own default on zero/absent).
    """
    go_value = _go_default_salience_max_channel_members()
    assert go_value == DEFAULT_SALIENCE_MAX_CHANNEL_MEMBERS, (
        f"salience_max_channel_members default drifted: "
        f"Go ({_CONFIG_GO}) = {go_value}, "
        f"Python (agents.salience_bid.DEFAULT_SALIENCE_MAX_CHANNEL_MEMBERS) "
        f"= {DEFAULT_SALIENCE_MAX_CHANNEL_MEMBERS}. One side was edited without "
        f"the other. Update both — and if the change is operator-visible, "
        f"update schemas/channel.schema.json (`default`) and "
        f"docs/guides/channels.md too."
    )


def test_go_default_matches_documented_value():
    """The Go default MUST be the documented ``20``.

    Independent of the cross-language equality test: pins the absolute
    value the operator-facing ``schemas/channel.schema.json``
    (`salience_max_channel_members.default: 20`) and `config/channels.yaml`
    discoverability comment advertise. A change to the absolute value
    should also update those surfaces; this test surfaces the omission.
    """
    assert _go_default_salience_max_channel_members() == 20
