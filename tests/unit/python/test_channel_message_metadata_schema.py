"""JSON-schema validation for the publish-payload ``metadata`` bag.

RFC 0011 amendment "Cascade-depth wire propagation" introduced a
``metadata.cascade_depth`` key on the channel publish payload. The
amendment doc pins:

* the key is an **optional integer**;
* values below ``0`` are rejected at the schema gate;
* the documented operational range is ``[0, max_cascade_depth]`` but
  schema enforcement is permissive on the upper bound — the
  orchestrator clamps values above ``max_cascade_depth`` down at the
  publish boundary (PR 2 of this plan), so wire-acceptance of a large
  value is intentional and must round-trip the schema check.

This test pulls the ``messageMetadata`` definition out of
``schemas/channel.schema.json`` and exercises it directly. Schema
drift (e.g. someone tightens ``maximum`` and inadvertently rejects
clamp-target values) shows up here rather than as an opaque 4xx in
manual testing of PR 2.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema  # type: ignore[import-untyped]
import pytest

_SCHEMA_PATH = Path("schemas/channel.schema.json")


def _message_metadata_subschema() -> dict:
    """Extract the ``messageMetadata`` definition from the channel schema.

    The definition is housed in ``channel.schema.json`` per the amendment
    doc (not in a separate schema file) so the REST publish payload's
    metadata contract sits next to the channel config it routes against.
    """
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return {
        # Validators resolve ``$ref`` against the parent schema; passing
        # the full schema as the root means ``definitions/...`` refs are
        # reachable, but we point the validator's root at the subschema
        # so callers see the metadata shape directly.
        **schema["definitions"]["messageMetadata"],
        # Preserve the parent ``$schema`` so the right draft validator
        # is selected even when the subschema is exercised in isolation.
        "$schema": schema.get("$schema", "http://json-schema.org/draft-07/schema#"),
    }


def _validate(instance: dict) -> None:
    jsonschema.validate(instance=instance, schema=_message_metadata_subschema())


def test_cascade_depth_zero_is_accepted():
    """``cascade_depth: 0`` is the wire value for an un-incremented publish."""
    _validate({"cascade_depth": 0})


def test_cascade_depth_in_documented_range_is_accepted():
    """A value inside the documented ``[0, max_cascade_depth]`` band passes."""
    _validate({"cascade_depth": 5})


def test_cascade_depth_negative_is_rejected():
    """Negative depths are schema-illegal — no clamp covers this case."""
    with pytest.raises(jsonschema.ValidationError):
        _validate({"cascade_depth": -1})


def test_cascade_depth_above_max_is_accepted_for_server_clamp():
    """Large values are accepted at the schema gate so the server can clamp.

    The amendment is explicit: an above-cap value is **not** a malformed
    payload. The orchestrator clamps ``> max_cascade_depth`` down to the
    cap at the publish boundary (PR 2). Rejecting on the schema side
    would force every publisher to know the orchestrator's current cap
    before it could compose a payload, which inverts the trust model the
    amendment establishes.
    """
    _validate({"cascade_depth": 9999})


def test_cascade_depth_is_optional():
    """An empty metadata bag is a valid publish payload (most publishes).

    The vast majority of publishes do not need to set ``cascade_depth``
    — the agent-side executor only sets it for cascade-originating
    fanout. The schema must therefore accept an absent key without
    falling back on a synthesized default.
    """
    _validate({})


def test_cascade_depth_non_integer_is_rejected():
    """Strings and floats both miss the wire shape Python+Go agree on.

    ``int32`` on the gRPC side and ``integer`` on the schema side: a
    publisher emitting ``"5"`` or ``5.5`` would silently degrade the
    cap (Go would parse the metadata bag and refuse to coerce). Pin
    the type strictly at the schema gate so the failure surfaces at
    the REST boundary, not at the orchestrator's metadata read.
    """
    with pytest.raises(jsonschema.ValidationError):
        _validate({"cascade_depth": "5"})
    with pytest.raises(jsonschema.ValidationError):
        _validate({"cascade_depth": 5.5})


def test_cascade_depth_whole_number_float_is_accepted():
    """JSON Schema draft-07 treats ``5.0`` as a valid integer.

    Draft-07 defines ``integer`` as any number with a zero fractional
    part, so ``5.0`` passes the same gate as ``5``. This is a
    forward-looking pin for PR 2: the Go orchestrator parses the
    metadata bag as ``map[string]any`` and JSON unmarshalling yields
    ``float64`` for any unquoted number. The clamp-and-coerce path
    must therefore accept whole-number floats and convert them to
    ``int32`` — rejecting them would break wire-acceptance of
    payloads that already pass schema validation here.
    """
    _validate({"cascade_depth": 5.0})


def test_unknown_metadata_keys_are_permitted():
    """``messageMetadata`` is intentionally open for future extension.

    No ``additionalProperties: false`` on the subschema: a publisher
    emitting unknown keys (e.g. a future ``trace_id``) must not
    fail the gate. The trade-off is that a typo like ``cascadedepth``
    silently degrades to the implicit-zero cascade origin on the Go
    side rather than surfacing as a 4xx. Pin this expectation so a
    later tightening to ``additionalProperties: false`` lands with
    a deliberate test update rather than riding in unannotated.
    """
    _validate({"cascade_depth": 3, "unknown_future_key": "tolerated"})
    _validate({"cascadedepth": 3})  # typo passes — degrades to zero downstream


# RFC 0030 deterministic governance layers (v0.3.8), PR 1 — the optional
# ``metadata.interaction_id`` key. The amendment pins it as an optional opaque
# string bounded at 128 chars. This is the *documentary* contract (the
# ``messageMetadata`` definition is not ``$ref``'d into the validated config
# tree, so it is not a runtime publish gate): it declares the valid range, and
# a strict validator — like this test — rejects an out-of-range value. The
# runtime boundaries enforce the same 128-byte cap but *tolerantly*: the Go
# publish boundary (``readInteractionID``) and the agent receive seed
# (``seed_wire_metadata``) degrade an over-length claim to untracked (drop the
# value, keep dispatching) rather than failing — so a strict-reject here and a
# silent-drop at runtime are two expressions of the same bound, not the same
# behaviour. Char count here (JSON Schema ``maxLength`` counts code units);
# the runtime bound counts UTF-8 bytes — equal for the ASCII uuid4/ULID id.


def test_interaction_id_is_optional():
    """A publish without ``interaction_id`` is the untracked / pre-v0.3.8
    case and must pass — the feature is additive."""
    _validate({})


def test_interaction_id_valid_token_accepted():
    """A normal uuid4-shaped id rides the schema unchanged."""
    _validate({"interaction_id": "4e2b7c9a-1f3d-4a6b-8c2e-9d0f1a2b3c4d"})


def test_interaction_id_at_max_length_accepted():
    """A value exactly at the 128-char bound is legitimate and must pass —
    the bound rejects only strictly longer values (mirrors the Go
    ``TestChannelMessageToProto_InteractionID_AcceptsAtCap`` boundary)."""
    _validate({"interaction_id": "x" * 128})


def test_interaction_id_over_max_length_rejected():
    """An over-length id is rejected by the schema (the strict expression of
    the bound). The runtime boundaries enforce the same cap but degrade an
    over-length claim to untracked rather than rejecting it (see the Go
    ``TestChannelMessageToProto_InteractionID_RejectsOverlong`` and the Python
    ``test_overlong_interaction_id_not_seeded``). Either way an unbounded id
    never reaches the per-interaction maps Layers 2/4 key on."""
    with pytest.raises(jsonschema.ValidationError):
        _validate({"interaction_id": "x" * 129})


def test_interaction_id_non_string_rejected():
    """A non-string claim violates ``type: string``."""
    with pytest.raises(jsonschema.ValidationError):
        _validate({"interaction_id": 1234})
