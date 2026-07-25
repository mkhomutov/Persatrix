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
    merges additional member keys (e.g. the per-disposition ``threshold``).
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


def test_member_respond_accepts_chair_disposition():
    """The v0.3.8 Tier B ``chair`` disposition MUST validate.

    ``chair`` is a low-threshold facilitator: a ``participant`` (legacy
    ``always``) carrying a low default ``threshold`` so it clears the Tier B
    salience bid readily. The Go loader normalizes it to ``always`` + a low
    threshold at load time, but ``make validate`` is the first gate an
    operator hits when they adopt the new value.
    """
    _validate(_channel_with_member("chair"))


def test_member_respond_accepts_chair_with_explicit_threshold():
    """A ``chair`` with an explicit ``threshold`` override validates."""
    _validate(_channel_with_member("chair", extra={"threshold": 0.4}))


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


def test_member_threshold_accepts_number():
    """The per-disposition ``threshold`` field accepts a number.

    Activated in v0.3.8 Tier B (PR 2 reads it as the salience bid bar); the
    schema admitted the field already in v0.3.7 so the activation is purely
    additive. It must validate as a number when present.
    """
    _validate(_channel_with_member("participant", extra={"threshold": 0.5}))


def test_member_threshold_rejects_non_number():
    """A non-numeric ``threshold`` is wire-illegal."""
    with pytest.raises(jsonschema.ValidationError):
        _validate(_channel_with_member("participant", extra={"threshold": "high"}))


@pytest.mark.parametrize("boundary", [0.0, 1.0])
def test_member_threshold_accepts_unit_interval_boundaries(boundary: float):
    """The ``threshold`` accepts the ``[0, 1]`` salience range.

    The field is a per-disposition *salience* threshold for the Tier B bid,
    and salience is clipped to ``[0.0, 1.0]`` (RFC 0024 PR plan; mirrored by
    ``autonomy.salience_threshold`` in ``agent.schema.json``, which is bounded
    ``[0.0, 1.0]``). Both endpoints must validate.
    """
    _validate(_channel_with_member("participant", extra={"threshold": boundary}))


@pytest.mark.parametrize("out_of_range", [-0.1, 1.5])
def test_member_threshold_rejects_out_of_range(out_of_range: float):
    """A ``threshold`` outside ``[0, 1]`` is wire-illegal.

    The range was pinned in v0.3.7 (before Tier B read the field) so the
    v0.3.8 activation stays "purely additive": a config that set
    ``threshold: 5`` would already have failed under v0.3.7. A salience
    score lives in ``[0, 1]``, so any value outside it is meaningless.
    """
    with pytest.raises(jsonschema.ValidationError):
        _validate(_channel_with_member("participant", extra={"threshold": out_of_range}))


@pytest.mark.parametrize("non_open_floor", ["addressed", "observer", "when_mentioned"])
def test_member_threshold_on_non_open_floor_disposition_passes_schema(non_open_floor: str):
    """The schema deliberately does NOT enforce the cross-field invariant
    that ``threshold`` is only meaningful on an open-floor disposition.

    A ``threshold`` only has effect on ``participant``/``chair``/legacy
    ``always`` (the open-floor speakers that run the Tier B salience bid);
    on ``addressed``/``observer``/``when_mentioned`` no bid ever runs, so a
    bar there is a silent no-op. JSON Schema cannot express that one field's
    legality depends on another field's value, so such a config stays
    schema-valid — the Go loader is the *sole* enforcement point and rejects
    it with ``ErrThresholdNotApplicable`` (see ``internal/channels/config.go``
    ``Validate`` and the companion
    ``TestLoadConfig_RejectsThresholdOnNonOpenFloorDisposition``).

    This test pins that split so a future edit does not silently assume the
    schema already guards the invariant, nor tighten the schema in a way that
    makes the loader's check unreachable. A schema-valid config can still fail
    ``LoadConfig``; the schema ``threshold`` description says as much.
    """
    _validate(_channel_with_member(non_open_floor, extra={"threshold": 0.5}))


def test_member_unknown_key_still_rejected():
    """The member object remains ``additionalProperties: false``.

    Adding the ``threshold`` key must not open the member object
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


# ─── escalation_chair_id (chair-stall-escalation amendment, v0.3.8) ──────


def test_escalation_chair_id_pattern_matches_member_id_pattern():
    """``escalation_chair_id``'s pattern IS the member-id pattern — drift pin.

    PR #609 deep review: the knob shipped with the channel-*name* pattern
    (lowercase-only, no underscore, minimum two chars), so a perfectly legal
    member id (``Iron_Fox``, ``x``) could never be named as escalation chair —
    schema validation rejected a config the Go loader (membership check in
    ``Config.Validate``) accepts. The knob's domain is "one of this channel's
    member ids", so its pattern must be the member ``id`` pattern, verbatim.
    This compares the two pattern strings directly so the contract cannot
    drift apart silently again.
    """
    schema = _root_schema()
    member_pattern = schema["definitions"]["member"]["oneOf"][1]["properties"]["id"][
        "pattern"
    ]
    chair_pattern = schema["definitions"]["channel"]["properties"][
        "escalation_chair_id"
    ]["pattern"]
    assert chair_pattern == member_pattern


@pytest.mark.parametrize("chair_id", ["Iron_Fox", "x", "ada-7"])
def test_escalation_chair_id_accepts_member_style_ids(chair_id: str):
    """Any id the member ``id`` pattern admits is a legal chair value.

    The Go loader requires the chair to be a declared member; the schema's
    only job is the wire shape, so every member-legal spelling (uppercase,
    underscore, single char) must validate here too.
    """
    _validate(
        {
            "max_channels": 50,
            "channels": [
                {
                    "name": "planning",
                    "escalation_chair_id": chair_id,
                    "members": [{"id": chair_id}, {"id": "ada"}],
                }
            ],
        }
    )


# ── RFC 0037 (v0.3.12) classification fields ──────────────────────────────
#
# The per-channel `classification` and the root `dm_default_classification`
# knob land together in RFC 0037 PR 1 (channel classification at rest, dark).
# Both roots are closed (`additionalProperties: false`), so the schema MUST
# declare them or `make validate` rejects the documented operator config —
# the exact failure mode this file exists to pin (see module docstring).
# The enum is the fixed §A lattice; the Go loader mirrors the rejection via
# `ErrInvalidClassification` (config_validate.go).
#
# NOTE — schema-valid is deliberately WIDER than loadable during v0.3.12. This
# enum is the post-Phase-1 contract and accepts all four levels, but the Go
# loader additionally rejects `restricted`/`secret` (the item-8 dark-window
# ceiling, `ErrClassificationAboveDarkWindow`) until the §D gate arms at RFC
# 0037 PR 4. Tightening the enum instead would mean churning it back at PR 4
# and would make `make validate` disagree with itself across the window, so the
# temporary ceiling lives on the Go side only. The tests below therefore pin
# the enum, not the current startup behaviour.

_CLASSIFICATION_LEVELS = ["public", "internal", "restricted", "secret"]


@pytest.mark.parametrize("level", _CLASSIFICATION_LEVELS)
def test_channel_classification_accepts_lattice_levels(level: str):
    """Every §A lattice level validates on a declared group channel."""
    _validate(
        {
            "max_channels": 50,
            "channels": [
                {"name": "planning", "classification": level, "members": ["ada"]}
            ],
        }
    )


def test_channel_classification_rejects_unknown_level():
    """An out-of-vocabulary level (`confidential`) is rejected by the enum.

    The §A vocabulary is fixed for v0.3.x (RFC 0037 OQ #1); a typo must
    surface at `make validate`, not load silently and stamp `internal`.
    """
    with pytest.raises(jsonschema.ValidationError):
        _validate(
            {
                "max_channels": 50,
                "channels": [
                    {
                        "name": "planning",
                        "classification": "confidential",
                        "members": ["ada"],
                    }
                ],
            }
        )


@pytest.mark.parametrize("level", _CLASSIFICATION_LEVELS)
def test_dm_default_classification_accepts_lattice_levels(level: str):
    """The root-level DM stamping knob accepts every §A lattice level."""
    _validate({"max_channels": 50, "dm_default_classification": level})


def test_dm_default_classification_rejects_unknown_level():
    """Same enum posture for the fleet-wide DM knob."""
    with pytest.raises(jsonschema.ValidationError):
        _validate({"max_channels": 50, "dm_default_classification": "top-secret"})


def test_classification_enums_match_across_both_fields():
    """The two declaration points encode ONE vocabulary.

    RFC 0037 §A defines a single lattice; the per-channel field and the DM
    knob must never drift apart. Compared directly (the
    `escalation_chair_id` pattern-parity precedent above) so a future edit
    to one enum fails here rather than shipping a config surface where a
    level is legal on channels but not on DMs.
    """
    schema = _root_schema()
    channel_enum = schema["definitions"]["channel"]["properties"]["classification"][
        "enum"
    ]
    dm_enum = schema["properties"]["dm_default_classification"]["enum"]
    assert channel_enum == dm_enum == _CLASSIFICATION_LEVELS
