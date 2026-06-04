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


def _channel_with_member(respond: object = None, *, extra: dict | None = None) -> dict:
    """Build a minimal valid channel config carrying one member.

    ``respond`` is omitted from the member when ``None`` (exercising the
    shorthand default); otherwise it is set verbatim so the test can feed
    a disposition / legacy / unknown value through the real schema. ``extra``
    merges additional member keys (e.g. the reserved ``threshold``).
    """
    member: dict = {"id": "alice"}
    if respond is not None:
        member["respond"] = respond
    if extra:
        member.update(extra)
    return {"max_channels": 50, "channels": [{"name": "planning", "members": [member]}]}


@pytest.mark.parametrize("disposition", ["participant", "addressed", "observer"])
def test_member_respond_accepts_disposition_vocabulary(disposition: str):
    """The RFC 0030 disposition vocabulary MUST validate.

    PR 1 of the relevance amendment adds ``participant``/``addressed``/
    ``observer`` to the member ``respond`` enum; the Go loader normalizes
    them back to the legacy three at load time, but ``make validate`` is
    the first gate an operator hits when they adopt the new surface.
    """
    _validate(_channel_with_member(disposition))


@pytest.mark.parametrize("legacy", ["always", "when_mentioned", "never"])
def test_member_respond_accepts_legacy_vocabulary(legacy: str):
    """Existing configs using the legacy ``respond`` values keep validating.

    Back-compat (D4) is non-negotiable: the disposition addition is purely
    additive on the enum, so an operator's existing ``always`` member must
    not start failing ``make validate``.
    """
    _validate(_channel_with_member(legacy))


def test_member_respond_rejects_unknown_value():
    """An unrecognised ``respond`` value still fails ``make validate``.

    The enum stays closed: adding the disposition vocabulary widens the
    allow-list to six values, but a typo like ``participent`` must surface
    at validate time rather than reach the Go loader.
    """
    with pytest.raises(jsonschema.ValidationError):
        _validate(_channel_with_member("participent"))  # typo, not the real value


def test_member_threshold_reserved_field_accepts_number():
    """The reserved per-disposition ``threshold`` field accepts a number.

    The field exists in the schema so v0.3.8 Tier B is additive; nothing
    reads it in v0.3.7 (it is documented reserved/no-op). It must validate
    as a number when present so an early adopter who sets it does not trip
    ``make validate``.
    """
    _validate(_channel_with_member("participant", extra={"threshold": 0.5}))


def test_member_threshold_rejects_non_number():
    """A non-numeric ``threshold`` is wire-illegal even while reserved."""
    with pytest.raises(jsonschema.ValidationError):
        _validate(_channel_with_member("participant", extra={"threshold": "high"}))


def test_member_unknown_key_still_rejected():
    """The member object remains ``additionalProperties: false``.

    Adding the reserved ``threshold`` key must not open the member object
    to arbitrary keys; a typo'd field still fails validation.
    """
    with pytest.raises(jsonschema.ValidationError):
        _validate(_channel_with_member("participant", extra={"treshold": 0.5}))


def test_unknown_root_key_still_rejected():
    """The root remains ``additionalProperties: false`` for unrecognised keys.

    The M1 fix only adds the one documented knob to the allow-list; the
    closed-root invariant for everything else stays put. This test
    pins that invariant so a future widening (e.g. ``additionalProperties:
    true``) lands with a deliberate test update.
    """
    with pytest.raises(jsonschema.ValidationError):
        _validate({"max_channels": 50, "max_cascade_dept": 5})  # typo, not the real key
