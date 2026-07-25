"""RFC 0037 §C (v0.3.12 PR 3) — the task-local acting-classification axis
and the §A ``acting_at_or_below_internal`` rule helper.

:mod:`agents.acting_classification` is the ``sender_type`` twin: the PR 2
wire seed (``channel_classification`` on ``AgentEvent.metadata``) is bound
task-locally for an event's life by
:func:`agents.request_scope.request_scope_from_metadata`, so the identity
write-through at the ``store_note`` tool boundary can recover the acting
level without threading the event through tool dispatch.  The bound value
is VERBATIM (no lattice, no default) — resolution direction belongs to the
named §A resolvers, and an unbound axis reads ``None`` (the rule-(b)
``public`` floor at every consumer).
"""

from __future__ import annotations

import pytest

from agents.acting_classification import (
    acting_classification_scope_from_metadata,
    current_acting_classification,
)
from agents.channel_event_classification import (
    CHANNEL_CLASSIFICATION_METADATA_KEY,
)
from agents.persona_runtime.classification import (
    acting_at_or_below_internal,
)
from agents.request_scope import request_scope_from_metadata

# ─── The contextvar seam ────────────────────────────────────


class TestActingClassificationScope:
    def test_default_is_none(self):
        assert current_acting_classification() is None

    def test_binds_from_metadata_and_restores(self):
        with acting_classification_scope_from_metadata(
            {CHANNEL_CLASSIFICATION_METADATA_KEY: "restricted"},
        ):
            assert current_acting_classification() == "restricted"
        assert current_acting_classification() is None

    def test_absent_key_is_noop(self):
        with acting_classification_scope_from_metadata({}):
            assert current_acting_classification() is None

    def test_non_string_or_empty_is_noop(self):
        with acting_classification_scope_from_metadata(
            {CHANNEL_CLASSIFICATION_METADATA_KEY: ""},
        ):
            assert current_acting_classification() is None
        with acting_classification_scope_from_metadata(
            {CHANNEL_CLASSIFICATION_METADATA_KEY: 42},
        ):
            assert current_acting_classification() is None

    def test_verbatim_no_allowlist(self):
        # Seed-verbatim discipline: garbage binds as-is; every consumer
        # resolves it through a named §A rule (here rule (b), which can
        # only UNDER-privilege — garbage floors to ``public``).
        with acting_classification_scope_from_metadata(
            {CHANNEL_CLASSIFICATION_METADATA_KEY: "not-a-level"},
        ):
            assert current_acting_classification() == "not-a-level"

    def test_restored_on_exception(self):
        with pytest.raises(RuntimeError):
            with acting_classification_scope_from_metadata(
                {CHANNEL_CLASSIFICATION_METADATA_KEY: "secret"},
            ):
                raise RuntimeError("boom")
        assert current_acting_classification() is None


class TestRequestScopeBindsFifthAxis:
    def test_request_scope_binds_acting_classification(self):
        with request_scope_from_metadata(
            {CHANNEL_CLASSIFICATION_METADATA_KEY: "secret"},
        ):
            assert current_acting_classification() == "secret"
        assert current_acting_classification() is None

    def test_request_scope_without_key_leaves_axis_unbound(self):
        with request_scope_from_metadata({"session_id": "conv-a"}):
            assert current_acting_classification() is None


# ─── The §C rule helper ─────────────────────────────────────


class TestActingAtOrBelowInternal:
    @pytest.mark.parametrize(
        ("level", "expected"),
        [
            # Known levels rank as themselves.
            ("public", True),
            ("internal", True),
            ("restricted", False),
            ("secret", False),
            # Rule (b): absent/unknown acting floors to ``public`` —
            # the write-through proceeds, preserving every
            # pre-classification path's behaviour byte-for-byte.
            (None, True),
            ("", True),
            ("not-a-level", True),
        ],
    )
    def test_bound(self, level: str | None, expected: bool):
        assert acting_at_or_below_internal(level) is expected


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
