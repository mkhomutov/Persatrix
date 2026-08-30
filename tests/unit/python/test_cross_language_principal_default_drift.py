"""Cross-language drift pin for the default principal (``local``).

ISSUE-0130 shape (b) (v0.3.15 PR B1) gave the channel store's
``messages`` table a ``principal_id`` column whose "no verified tenant"
value is the literal ``local``.  That literal is now spelled **three**
times, in two languages and one frozen SQL string:

* ``channels.DefaultPrincipalID`` (``internal/channels/sqlite.go``) —
  the orchestrator's write-side source of truth, stamped onto every
  publish that resolves no tenant.
* ``agents.principal_id.DEFAULT_PRINCIPAL_ID`` — what a persona resolves
  when no ``persatrix-principal`` header arrives, and the value the
  persona-memory migration v11 backfilled onto its tiers.
* the ``DEFAULT '<value>'`` inside ``migrateV11ToV12``
  (``internal/channels/sqlite_principal_migration.go``) — deliberately a
  literal rather than an interpolation of the Go constant, because
  migration SQL is frozen history: a later rename must not silently
  rewrite what v12 backfilled.

Why it needs a pin rather than a doc-comment.  The two stores are
disjoint today — nothing in ``agents/`` queries ``messages`` — so a
rename on either side is compile-green and test-green on both trees.
They meet at the wire the moment PR B2 seeds the replayed catch-up event
from this column: the persona binds ``principal_scope`` from that seed
and recall is **strict equality**.  So drift does not raise; it makes
every replayed recall silently return nothing, which is the hardest
possible failure to notice and the exact shape of the defect ISSUE-0130
exists to close.

The parse-the-Go-source approach mirrors the sibling pins
(``test_cross_language_respond_policy_drift.py``,
``test_cross_language_max_cascade_depth_drift.py``): it needs no Go
toolchain, so it runs anywhere the Python unit suite already runs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agents.principal_id import DEFAULT_PRINCIPAL_ID

_SQLITE_GO = Path("internal/channels/sqlite.go")
_MIGRATION_GO = Path("internal/channels/sqlite_principal_migration.go")

# Captures `const DefaultPrincipalID = "<value>"` on a single line. The
# declaration is intentionally single-line (not a parenthesised `const (
# ... )` group), so this anchored form is the narrowest pattern that
# still tolerates leading whitespace and a trailing comment. A refactor
# that moves it into a const block would force a deliberate update here
# — that is intended: the parse rule is part of the contract.
_GO_CONST_PATTERN = re.compile(
    r'^\s*const\s+DefaultPrincipalID\s*=\s*"([^"]*)"\s*(?://.*)?$',
    re.MULTILINE,
)

# Captures the frozen backfill literal out of the v11 -> v12 ALTER.
_GO_MIGRATION_DEFAULT_PATTERN = re.compile(
    r"ADD COLUMN principal_id TEXT NOT NULL DEFAULT '([^']*)'",
)


def _parse(path: Path, pattern: re.Pattern[str], what: str) -> str:
    """Pull a single capture group out of a Go source file.

    Fails the test (rather than returning ``None``) on a miss, so a
    refactor that hides the value lands as an actionable message instead
    of a confusing ``None``-vs-``str`` comparison.
    """
    match = pattern.search(path.read_text(encoding="utf-8"))
    if match is None:
        pytest.fail(
            f"could not find {what} in {path}. If it was renamed or "
            f"reshaped, update the parse rule in this test to match — the "
            f"cross-language drift pin is part of the contract, not "
            f"incidental tooling.",
        )
    return match.group(1)


def test_go_and_python_default_principal_agree():
    """The orchestrator and the persona MUST spell the tenant the same.

    A drift here does not raise anywhere.  The orchestrator writes rows
    under one spelling, the persona binds ``principal_scope`` to the
    other, and strict-equality recall matches nothing — a silent,
    total recall miss for every replayed span, on a column whose entire
    purpose is attributing that replay.
    """
    go_value = _parse(_SQLITE_GO, _GO_CONST_PATTERN, "`const DefaultPrincipalID = \"...\"`")
    assert go_value == DEFAULT_PRINCIPAL_ID, (
        f"default principal drifted: Go ({_SQLITE_GO}) = {go_value!r}, "
        f"Python (agents.principal_id.DEFAULT_PRINCIPAL_ID) = "
        f"{DEFAULT_PRINCIPAL_ID!r}. One side was edited without the other; "
        f"they meet at the wire where catch-up replay seeds "
        f"`principal_scope` from the channel store's `principal_id`."
    )


def test_migration_backfill_matches_the_go_constant():
    """The v12 backfill literal MUST equal the constant it stands in for.

    The migration deliberately spells ``local`` out instead of
    interpolating :data:`channels.DefaultPrincipalID`, because applied
    migrations are frozen history.  That is only safe while the two
    agree at HEAD: a rename that touches the constant and not the ALTER
    would leave freshly-migrated databases backfilled to one value and
    freshly-published rows stamped with another, inside one table.
    """
    const_value = _parse(_SQLITE_GO, _GO_CONST_PATTERN, "`const DefaultPrincipalID = \"...\"`")
    migration_value = _parse(
        _MIGRATION_GO,
        _GO_MIGRATION_DEFAULT_PATTERN,
        "the `DEFAULT '...'` in the v11 -> v12 ADD COLUMN",
    )
    assert migration_value == const_value, (
        f"the v11->v12 backfill literal ({migration_value!r}, {_MIGRATION_GO}) "
        f"no longer equals DefaultPrincipalID ({const_value!r}, {_SQLITE_GO}). "
        f"Migration SQL is frozen history and must not be rewritten — if the "
        f"constant genuinely changed, that needs a NEW migration, not an edit "
        f"to v12."
    )


def test_default_principal_is_the_documented_value():
    """Pins the absolute ``local``, independent of the equality tests.

    ``schemas/channel.schema.json`` (``channelMessage.principal_id``),
    ``docs/guides/channels.md`` and the CHANGELOG all name ``local`` in
    prose an operator reads.  Changing the value should update those
    surfaces too; this test is where the omission surfaces.
    """
    assert DEFAULT_PRINCIPAL_ID == "local"
