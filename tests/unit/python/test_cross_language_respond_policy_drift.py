"""Cross-language drift pins for the ``respond_policy`` vocabulary.

Mirrors the established cross-language drift-test pattern
(``test_cross_language_max_cascade_depth_drift.py``, PR #319;
``test_cross_language_salience_max_channel_members_drift.py``, PR #573).

The respond-policy vocabulary is written out in several places that the
doc-comments declare "must be kept in lockstep" — but until this file
landed, nothing pinned the cross-language equalities. Each side is
independently pinned by same-language tests
(``test_response_gate_disposition.py`` here; the channels package's
normalization tests in Go), so a one-sided edit keeps that side's suite
green and the divergence lands silently:

* **Go** — the ``RespondPolicy`` const block and ``Normalize()``
  disposition→legacy mapping in ``internal/channels/channels.go``, and
  the ``MentionEveryone`` broadcast sentinel.
* **Python** — the ``POLICY_*`` constants and ``_DISPOSITION_ALIASES``
  defence-in-depth mirror in ``agents/response_gate.py``, the
  ``MENTION_EVERYONE`` sentinel, and the inbound-wire enum
  ``_CHANNEL_RESPOND_POLICIES`` in ``agents/channel_validation.py``.
* **Schema / DB** — the operator-facing ``respond`` enum in
  ``schemas/channel.schema.json`` and the legacy-triple CHECK constraint
  on ``memberships.respond_policy`` in
  ``internal/channels/sqlite_schema.go``.

The value sets deliberately differ per surface (config accepts both
vocabularies; the DB and wire carry only the legacy triple; ``never`` is
filtered upstream of dispatch so the agent-side validator rejects it as
malformed). The pins below assert those *documented relationships*, not
blanket equality, so an intentional subset stays distinguishable from an
accidental drift.

A drift here is silent in a matched-version deployment: the Go loader
normalizes dispositions at every write boundary, so the Python mirrors
are normally dormant. They become load-bearing exactly when things are
already abnormal — a hand-edited membership row, a caller bypassing the
loader, or a *mixed-version* deployment — and a drifted mirror then
fail-closes the gate (an agent silently goes quiet) or admits traffic
the Go side meant to suppress.

The test imports the Python constants directly and parses the Go source
as text. Parsing text (instead of e.g. invoking ``go run``) keeps the
test runnable in any environment that already runs the Python unit
suite — no Go toolchain dependency, no build-artefact plumbing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agents.channel_validation import _CHANNEL_RESPOND_POLICIES
from agents.response_gate import (
    _DISPOSITION_ALIASES,
    MENTION_EVERYONE,
    POLICY_ADDRESSED,
    POLICY_ALWAYS,
    POLICY_CHAIR,
    POLICY_NEVER,
    POLICY_OBSERVER,
    POLICY_PARTICIPANT,
    POLICY_WHEN_MENTIONED,
)

_CHANNELS_GO = Path("internal/channels/channels.go")
_SQLITE_SCHEMA_GO = Path("internal/channels/sqlite_schema.go")
_CHANNEL_SCHEMA_JSON = Path("schemas/channel.schema.json")

# The full declared vocabulary, from the Python side. Pinned as a dict
# (const-name suffix → wire string) so the same table also resolves the
# Go identifiers that appear in the ``Normalize()`` switch.
_PYTHON_POLICIES: dict[str, str] = {
    "WhenMentioned": POLICY_WHEN_MENTIONED,
    "Always": POLICY_ALWAYS,
    "Never": POLICY_NEVER,
    "Participant": POLICY_PARTICIPANT,
    "Addressed": POLICY_ADDRESSED,
    "Observer": POLICY_OBSERVER,
    "Chair": POLICY_CHAIR,
}

_LEGACY_TRIPLE: set[str] = {POLICY_WHEN_MENTIONED, POLICY_ALWAYS, POLICY_NEVER}

# Captures `Respond<Name> RespondPolicy = "<value>"` lines in the
# `const ( ... )` block of channels.go. A future refactor that renames
# the constants or moves them out of the typed-const form would force a
# deliberate update here — that is intended: the parse rule is part of
# the contract.
_GO_CONST_PATTERN = re.compile(
    r'^\s*Respond(\w+)\s+RespondPolicy\s*=\s*"([^"]+)"\s*$',
    re.MULTILINE,
)

# Captures the body of `func (p RespondPolicy) Normalize() RespondPolicy`.
# The switch's closing brace is tab-indented, so the non-greedy match up
# to a column-0 `}` stops at the function's closing brace.
_GO_NORMALIZE_PATTERN = re.compile(
    r"func \(p RespondPolicy\) Normalize\(\) RespondPolicy \{\n(.*?)\n\}",
    re.DOTALL,
)

# Captures `const MentionEveryone = "<value>"`.
_GO_MENTION_EVERYONE_PATTERN = re.compile(
    r'^\s*const\s+MentionEveryone\s*=\s*"([^"]+)"\s*$',
    re.MULTILINE,
)

# Captures the memberships-table CHECK constraint value list. DOTALL
# because the column definition wraps across two lines in the schema
# string literal.
_SQLITE_CHECK_PATTERN = re.compile(
    r"respond_policy\s+TEXT.*?CHECK\s*\(respond_policy\s+IN\s*\(([^)]*)\)\)",
    re.DOTALL,
)


def _parse_miss(what: str, where: Path) -> None:
    """Fail with an actionable message on a parse miss (vs a silent
    ``None``-vs-value ``AssertionError``): a refactor that hides the
    declaration must land as a deliberate update to this test's parse
    rules — the cross-language drift pin is part of the contract.
    """
    pytest.fail(
        f"could not find {what} in {where}. If it was renamed or "
        f"restructured, update the parse rule in this test to match the "
        f"new shape — the cross-language drift pin is part of the "
        f"contract.",
    )


def _go_policy_consts() -> dict[str, str]:
    """Parse the ``RespondPolicy`` const block: name suffix → wire string."""
    src = _CHANNELS_GO.read_text(encoding="utf-8")
    consts = dict(_GO_CONST_PATTERN.findall(src))
    if not consts:
        _parse_miss('`Respond<Name> RespondPolicy = "<value>"` consts', _CHANNELS_GO)
    return consts


def _go_normalize_aliases() -> dict[str, str]:
    """Parse the ``Normalize()`` switch into a disposition→legacy map.

    Resolves the ``Respond*`` identifiers in the ``case``/``return``
    arms through the const block, returning wire strings on both sides —
    directly comparable to ``_DISPOSITION_ALIASES``.
    """
    src = _CHANNELS_GO.read_text(encoding="utf-8")
    body_match = _GO_NORMALIZE_PATTERN.search(src)
    if body_match is None:
        _parse_miss("`func (p RespondPolicy) Normalize()`", _CHANNELS_GO)
        raise AssertionError("unreachable")  # _parse_miss always raises
    consts = _go_policy_consts()
    aliases: dict[str, str] = {}
    # Each `case A, B:` chunk maps its named constants to the chunk's
    # first `return Respond<Name>`. The trailing `return p` (identity for
    # legacy values) never matches the Respond-prefixed pattern.
    for chunk in body_match.group(1).split("case ")[1:]:
        head, _, rest = chunk.partition(":")
        target_match = re.search(r"return\s+Respond(\w+)", rest)
        if target_match is None:
            _parse_miss(
                f"a `return Respond<Name>` arm for `case {head.strip()}`",
                _CHANNELS_GO,
            )
            raise AssertionError("unreachable")
        target = consts[target_match.group(1)]
        for name in head.split(","):
            aliases[consts[name.strip().removeprefix("Respond")]] = target
    return aliases


def _go_mention_everyone() -> str:
    src = _CHANNELS_GO.read_text(encoding="utf-8")
    match = _GO_MENTION_EVERYONE_PATTERN.search(src)
    if match is None:
        _parse_miss('`const MentionEveryone = "<value>"`', _CHANNELS_GO)
        raise AssertionError("unreachable")
    return match.group(1)


def _sqlite_check_policies() -> set[str]:
    """Parse the memberships-table ``respond_policy`` CHECK value list."""
    src = _SQLITE_SCHEMA_GO.read_text(encoding="utf-8")
    match = _SQLITE_CHECK_PATTERN.search(src)
    if match is None:
        _parse_miss(
            "the `respond_policy ... CHECK (respond_policy IN (...))` constraint",
            _SQLITE_SCHEMA_GO,
        )
        raise AssertionError("unreachable")
    return set(re.findall(r"'(\w+)'", match.group(1)))


def _schema_respond_property() -> dict:
    """Walk ``channel.schema.json`` for the single ``respond`` enum property."""
    schema = json.loads(_CHANNEL_SCHEMA_JSON.read_text(encoding="utf-8"))
    found: list[dict] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            respond = node.get("respond")
            if isinstance(respond, dict) and "enum" in respond:
                found.append(respond)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(schema)
    if len(found) != 1:
        _parse_miss(
            f'exactly one `respond` property with an `enum` (found {len(found)})',
            _CHANNEL_SCHEMA_JSON,
        )
    return found[0]


def test_go_consts_and_python_constants_agree() -> None:
    """The Go ``RespondPolicy`` const block and the Python ``POLICY_*``
    constants MUST declare the same vocabulary, name-for-name.

    Compared as dicts (not value sets) so a crossed wiring — e.g. Go's
    ``RespondObserver`` carrying ``"addressed"`` — fails even though the
    value *sets* would still match.
    """
    go = _go_policy_consts()
    assert go == _PYTHON_POLICIES, (
        f"respond_policy vocabulary drifted: Go ({_CHANNELS_GO}) declares "
        f"{go}, Python (agents.response_gate POLICY_*) declares "
        f"{_PYTHON_POLICIES}. One side was edited without the other. "
        f"Update both — and if the vocabulary is operator-visible, update "
        f"schemas/channel.schema.json (`respond.enum`) and "
        f"docs/guides/channels.md too."
    )


def test_go_normalize_and_python_aliases_agree() -> None:
    """Go's ``Normalize()`` disposition→legacy mapping MUST equal the
    Python ``_DISPOSITION_ALIASES`` defence-in-depth mirror.

    Both doc-comments state the two "must be kept in lockstep"; this is
    the pin. A drift means the orchestrator and the receiver gate
    disagree about what a disposition *means*: Go fans out on one
    canonical policy while the gate enforces another (or fail-closes the
    member entirely via its ``unknown_policy`` branch — an agent that
    silently goes quiet).
    """
    go = _go_normalize_aliases()
    assert go == _DISPOSITION_ALIASES, (
        f"disposition→legacy normalization drifted: Go Normalize() "
        f"({_CHANNELS_GO}) maps {go}, Python "
        f"(agents.response_gate._DISPOSITION_ALIASES) maps "
        f"{_DISPOSITION_ALIASES}. One side was edited without the other; "
        f"update both."
    )


def test_normalize_targets_are_the_legacy_triple() -> None:
    """Every ``Normalize()`` target MUST be a legacy value, and every
    non-disposition const MUST be one of the legacy triple — together:
    the canonical form past the write boundary is exactly the triple the
    DB CHECK and the wire contract assume.
    """
    aliases = _go_normalize_aliases()
    assert set(aliases.values()) <= _LEGACY_TRIPLE
    non_dispositions = set(_go_policy_consts().values()) - set(aliases)
    assert non_dispositions == _LEGACY_TRIPLE


def test_sqlite_check_is_the_legacy_triple() -> None:
    """The ``memberships.respond_policy`` CHECK MUST accept exactly the
    legacy triple — the store's back-compat guarantee that a disposition
    value never reaches a membership row (``canonicalRespondPolicy``
    normalizes at every write path; the CHECK is the last line). Widening
    the CHECK without teaching every reader is the regression this pins.
    """
    assert _sqlite_check_policies() == _LEGACY_TRIPLE, (
        f"memberships CHECK constraint ({_SQLITE_SCHEMA_GO}) accepts "
        f"{_sqlite_check_policies()}, but the canonical persisted "
        f"vocabulary is the legacy triple {_LEGACY_TRIPLE}. If this is a "
        f"deliberate vocabulary migration, update the readers (the Python "
        f"gate, the wire validator) and this test together."
    )


def test_schema_enum_is_the_full_vocabulary() -> None:
    """The operator-facing ``respond`` enum MUST offer both vocabularies
    — exactly the values the Go loader accepts (``RespondPolicy.Valid``).
    A value added to one side only either 400s at the REST boundary
    despite being schema-valid, or is loadable but undocumented.
    """
    respond = _schema_respond_property()
    assert set(respond["enum"]) == set(_PYTHON_POLICIES.values()), (
        f"schema respond.enum ({_CHANNEL_SCHEMA_JSON}) = "
        f"{sorted(respond['enum'])}, but the declared vocabulary is "
        f"{sorted(_PYTHON_POLICIES.values())}. Update both sides together."
    )


def test_schema_default_normalizes_to_when_mentioned() -> None:
    """The schema's ``respond`` default MUST stay an alias of
    ``when_mentioned`` — the same conservative fallback the catch-up
    replay path (``channel_catchup._resolve_respond_policy``) and the
    membership column default assume for a member with no declared
    policy. A default that silently became open-floor would put every
    default-policy member on the floor of every conversation.
    """
    respond = _schema_respond_property()
    default = respond.get("default")
    assert isinstance(default, str), (
        f"schema respond property has no string `default` "
        f"({_CHANNEL_SCHEMA_JSON})"
    )
    aliases = _go_normalize_aliases()
    assert aliases.get(default, default) == POLICY_WHEN_MENTIONED


def test_wire_validator_is_the_triple_minus_never() -> None:
    """The agent-side inbound enum MUST be exactly the legacy triple
    minus ``never``: the orchestrator never dispatches to a
    ``respond: never`` member (RFC 0011 PR 4b / proto/task.proto), so a
    ``never`` on the wire is malformed by contract, and a disposition on
    the wire means a write boundary skipped ``Normalize()``. Widening the
    validator without amending that contract (or narrowing it without
    teaching the dispatcher) is the drift this pins.
    """
    assert _CHANNEL_RESPOND_POLICIES == _LEGACY_TRIPLE - {POLICY_NEVER}, (
        f"agents.channel_validation._CHANNEL_RESPOND_POLICIES = "
        f"{_CHANNEL_RESPOND_POLICIES}, expected the legacy triple minus "
        f"`never`. If the wire contract changed, update proto/task.proto's "
        f"respond_policy doc-comment and the Go dispatcher together."
    )


def test_mention_everyone_sentinel_agrees() -> None:
    """``MentionEveryone`` (Go) and ``MENTION_EVERYONE`` (Python) MUST be
    equal — both doc-comments state the two "must stay in lockstep". A
    skew silently breaks broadcasts: the receiver gate treats the Go
    sentinel as an ordinary mention of someone else, so a message meant
    for the whole room draws no replies from ``participant`` members.
    """
    go_value = _go_mention_everyone()
    assert go_value == MENTION_EVERYONE, (
        f"broadcast sentinel drifted: Go ({_CHANNELS_GO}) MentionEveryone "
        f"= {go_value!r}, Python (agents.response_gate.MENTION_EVERYONE) "
        f"= {MENTION_EVERYONE!r}. One side was edited without the other; "
        f"update both."
    )
