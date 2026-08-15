"""Cross-language drift pin: Go channel payload structs ↔ JSON Schema.

RFC 0040 Phase 1 asks for the payload contract to be validated on the
agent send side *and* the orchestrator decode side. The send side does
it at runtime (:mod:`agents.channel_payload_contract`, fail-open). The
orchestrator side is pinned **here, at test time, by struct-tag parity**
rather than by runtime validation, for two reasons:

1. **No Go JSON-Schema dependency exists in this repo** (``go.mod`` has
   none). Adding a third-party validator to the publish hot path to
   power a fail-open WARN would buy no signal the send side and this
   pin do not already carry, at the cost of a new runtime dependency on
   the orchestrator's most-trafficked handler.
2. **Drift is a development-time event, not a request-time one.** Two
   payload schemas drifting in parallel (RFC 0040 Motivation 1) happens
   when someone edits one side; it is caught when that edit is made.
   A red build names the offending field; a production WARN would name
   it only after the release.

So this file parses ``internal/server/channel_types.go`` as text and
asserts the Go structs and the schema definitions describe the same
shape. It follows the established idiom of the other cross-language
pins in this suite (e.g. ``test_cross_language_max_cascade_depth_drift.py``):
text-parsing the Go source keeps the check runnable anywhere the Python
unit suite runs — no Go toolchain, no build artefacts.

**What drift looks like when it lands here.** Someone adds a field to
``publishMessageRequest`` in Go and ships it; the agent never learns to
send it, or sends it under a different name. Today nothing would notice
until the Phase 2 proto was mirrored off a shape that had already
diverged. After this file, that edit is a red test naming the field.

*(Recorded deviation from the RFC's PR-1 scope line "Validate/asserts
the decoded body against the same schema on the orchestrator side" —
the assertion is test-time, not runtime. Reversible at review: if PR
review wants runtime Go validation, it costs a new dependency and a
decision on fail-open vs fail-closed at the handler.)*
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_TYPES_GO = Path("internal/server/channel_types.go")
_SCHEMA_PATH = Path("schemas/channel.schema.json")

# Go struct name → schema definition name.
_STRUCT_TO_DEFINITION = {
    "publishMessageRequest": "publishMessageRequest",
    "channelMessageResponse": "channelMessage",
    "historyResponse": "channelHistoryResponse",
}

# `json:"name,omitempty"` / `json:"name"` on a struct field line.
_JSON_TAG = re.compile(r'json:"(?P<name>[^",]+)(?P<opts>[^"]*)"')


def _struct_body(source: str, name: str) -> str:
    """Return the body of ``type <name> struct { ... }``.

    Brace-counted rather than lazily regex-matched: the struct bodies
    carry doc comments containing braces, and a non-greedy ``.*?\\}``
    would stop at the first one.
    """
    start = source.index(f"type {name} struct {{")
    cursor = source.index("{", start)
    depth = 0
    for i in range(cursor, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[cursor + 1:i]
    raise AssertionError(f"unterminated struct body for {name}")


def _go_fields(struct_name: str) -> dict[str, bool]:
    """Map JSON field name → ``True`` when the field is always present.

    "Always present" means the tag carries no ``omitempty``, so the key
    appears in the marshalled object unconditionally — which is exactly
    the schema's notion of ``required``.
    """
    body = _struct_body(_TYPES_GO.read_text(encoding="utf-8"), struct_name)
    fields: dict[str, bool] = {}
    for line in body.splitlines():
        # Skip comment lines so a `json:"..."` mentioned in prose (the
        # doc comments here quote wire keys freely) is never read as a
        # field declaration.
        if line.lstrip().startswith("//"):
            continue
        match = _JSON_TAG.search(line)
        if match:
            fields[match.group("name")] = "omitempty" not in match.group("opts")
    return fields


def _definition(name: str) -> dict:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema["definitions"][name]


@pytest.mark.parametrize(
    ("struct_name", "definition_name"), sorted(_STRUCT_TO_DEFINITION.items()),
)
class TestPayloadStructParity:

    def test_field_sets_match(self, struct_name, definition_name):
        """Every Go JSON key is in the schema and vice versa.

        The failure this catches: a field added to one side only. With
        `additionalProperties: false` on the schema, a Go-only field
        would also be *rejected* by the send-side validator — so a
        one-sided edit shows up twice, here by name.
        """
        go_fields = set(_go_fields(struct_name))
        schema_fields = set(_definition(definition_name).get("properties", {}))
        assert go_fields == schema_fields, (
            f"{struct_name} (internal/server/channel_types.go) and "
            f"definitions.{definition_name} (schemas/channel.schema.json) "
            f"describe different shapes.\n"
            f"  only in Go:     {sorted(go_fields - schema_fields)}\n"
            f"  only in schema: {sorted(schema_fields - go_fields)}\n"
            "Update both sides — the schema is the agent↔orchestrator "
            "contract (RFC 0040 Phase 1), not documentation of one side."
        )

    def test_required_matches_omitempty(self, struct_name, definition_name):
        """Schema `required` == the Go fields lacking `omitempty`.

        These are the same statement in two languages: a field with no
        `omitempty` always marshals, so a consumer may rely on it. If
        someone adds `omitempty` to a field the schema still calls
        required, clients that stopped null-guarding it break — this is
        where that shows up.
        """
        always_present = {
            name for name, required in _go_fields(struct_name).items() if required
        }
        schema_required = set(_definition(definition_name).get("required", []))
        assert always_present == schema_required, (
            f"{struct_name}: fields without `omitempty` "
            f"{sorted(always_present)} != definitions.{definition_name} "
            f"required {sorted(schema_required)}"
        )


class TestParserIntegrity:
    """The parser must not silently read nothing.

    A regex that matches zero fields would make every parity assertion
    above trivially pass on two empty sets — the classic way a
    cross-language pin rots into a no-op.
    """

    @pytest.mark.parametrize("struct_name", sorted(_STRUCT_TO_DEFINITION))
    def test_parser_finds_fields(self, struct_name):
        fields = _go_fields(struct_name)
        assert len(fields) >= 2, (
            f"parsed {len(fields)} fields from {struct_name} — the struct "
            "layout in internal/server/channel_types.go likely changed and "
            "this pin has stopped reading it"
        )

    def test_sender_id_is_seen(self):
        """A specific known key, so a regex that matched only comments
        or whitespace cannot satisfy the count check above."""
        assert "sender_id" in _go_fields("publishMessageRequest")
