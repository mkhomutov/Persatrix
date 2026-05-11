"""JSON-schema validation for the ``channels.yaml`` config root.

PR #319 deep review finding M1 ("``channels.yaml`` ``max_cascade_depth:``
key rejected by ``make validate``") motivated this file. PR 2 of the
v0.3.0 channel test-findings plan added the ``max_cascade_depth:`` knob
to ``internal/channels/config.go`` and wired it through to the Go
orchestrator, and documented it in ``docs/guides/channels.md`` and
``CHANGELOG.md`` — but the JSON schema's root is closed
(``additionalProperties: false`` declares only ``max_channels`` and
``channels``). An operator following the new docs and adding
``max_cascade_depth: 5`` to ``config/channels.yaml`` would trip
``make validate`` with "Additional properties are not allowed".

These tests pin the contract end-to-end against the real
``schemas/channel.schema.json`` (no parallel test schema) so a future
edit that drops the root-level knob from the schema lands as a hard
failure rather than as a silently broken operator-facing claim.

Companion test file: ``test_channel_message_metadata_schema.py`` covers
the ``messageMetadata`` *publish-payload* subschema; this file covers
the channels.yaml *config-root* schema. The two contracts are
intentionally separate (one rides on the REST publish wire, one rides
on the operator's YAML file) so they keep separate test files.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema  # type: ignore[import-untyped]
import pytest


_SCHEMA_PATH = Path("schemas/channel.schema.json")


def _root_schema() -> dict:
    """Return the parsed channel schema as the validator's root."""
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _validate(instance: dict) -> None:
    jsonschema.validate(instance=instance, schema=_root_schema())


def test_max_cascade_depth_at_default_is_accepted():
    """``max_cascade_depth: 5`` (the documented default) MUST validate.

    The default cap is the most-likely value an operator copy-pastes
    from ``docs/guides/channels.md`` §"Cascade-depth backstop". If this
    case fails, the documented copy-paste path is broken on day one.
    """
    _validate({"max_channels": 50, "max_cascade_depth": 5})


def test_max_cascade_depth_below_default_is_accepted():
    """An operator-tightened cap (e.g. ``max_cascade_depth: 3``) validates.

    The Go loader will adopt any positive integer; the schema's role is
    only to gate the wire shape, not to second-guess the operator's
    tightening decision.
    """
    _validate({"max_channels": 50, "max_cascade_depth": 3})


def test_max_cascade_depth_zero_is_accepted_for_loader_default_substitution():
    """``max_cascade_depth: 0`` MUST validate; the Go loader silently substitutes the default.

    Rationale: ``ChannelRouter.SetMaxCascadeDepth`` (router.go:178) is
    explicitly documented as "Non-positive values are ignored so a
    zero/negative config row cannot silently disable the backstop". The
    schema admits zero so the Go-side fall-through is the only place
    that decides "treat zero as 'use the default'". Rejecting zero at
    the schema would force operators who explicitly want the default
    to delete the key entirely — surprising given the docs show the
    key and the Go layer is already the authority on non-positive
    handling.
    """
    _validate({"max_channels": 50, "max_cascade_depth": 0})


def test_max_cascade_depth_negative_is_rejected():
    """A negative cap is wire-illegal: there is no sensible meaning.

    Distinct from zero (which is the loader's "use the default" sentinel),
    a negative value is unambiguously a mistake — no clamp-down can
    salvage it. The Go loader would also ignore it, but rejecting at
    the schema surfaces the operator error at ``make validate`` time
    rather than as a silent fall-back to the default at startup.
    """
    with pytest.raises(jsonschema.ValidationError):
        _validate({"max_channels": 50, "max_cascade_depth": -1})


def test_max_cascade_depth_string_is_rejected():
    """Non-integer types miss the wire shape.

    YAML's loose typing means a quoted ``"5"`` parses as a string and
    would otherwise reach the Go loader as ``string``, where it would
    panic the YAML decode rather than the schema validator. Pin the
    int-only contract at the schema gate so the failure surfaces with
    a clear ``make validate`` message instead of a startup-time
    unmarshal panic.
    """
    with pytest.raises(jsonschema.ValidationError):
        _validate({"max_channels": 50, "max_cascade_depth": "5"})


def test_max_cascade_depth_is_optional():
    """A config that omits ``max_cascade_depth`` MUST validate.

    Existing operators (and the default ``config/channels.yaml`` in
    this repo) do not set the key. The Go loader falls back to
    ``defaults.DefaultMaxCascadeDepth`` (5) when the field is absent.
    A schema that required the key would break every existing config.
    """
    _validate({"max_channels": 50})


def test_unknown_root_key_still_rejected():
    """The root remains ``additionalProperties: false`` for unrecognised keys.

    The M1 fix only adds the one documented knob to the allow-list; the
    closed-root invariant for everything else stays put. This test
    pins that invariant so a future widening (e.g. ``additionalProperties:
    true``) lands with a deliberate test update.
    """
    with pytest.raises(jsonschema.ValidationError):
        _validate({"max_channels": 50, "max_cascade_dept": 5})  # typo, not the real key
