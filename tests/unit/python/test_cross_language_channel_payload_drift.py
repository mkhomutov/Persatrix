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
_HANDLERS_GO = Path("internal/server/channel_handlers.go")
_SCHEMA_PATH = Path("schemas/channel.schema.json")

# Structs Go MARSHALS. Their `omitempty` tags decide what appears on the
# wire, so "no omitempty" and schema `required` are the same statement in
# two languages (see :meth:`TestResponseParity.test_required_matches_omitempty`).
_RESPONSE_STRUCTS = {
    "channelMessageResponse": "channelMessage",
    "historyResponse": "channelHistoryResponse",
}

# Structs Go only UNMARSHALS. `omitempty` says nothing here —
# `encoding/json` ignores it when decoding — so requiredness is a
# property of the HANDLER's explicit guards, and that is what
# :class:`TestRequestParity` pins it against.
_REQUEST_STRUCTS = {"publishMessageRequest": "publishMessageRequest"}

_STRUCT_TO_DEFINITION = {**_REQUEST_STRUCTS, **_RESPONSE_STRUCTS}

# The handler whose 400s define requiredness for the publish body.
_PUBLISH_HANDLER = "handlePublishMessage"

# `json:"name,omitempty"` / `json:"name"` on a struct field line, with the
# Go field identifier that leads it — the identifier is what the handler's
# guards name (`req.SenderID`), the tag is what the schema names.
_FIELD_LINE = re.compile(
    r'^\s*(?P<go>[A-Z]\w*)\s+\S+.*json:"(?P<name>[^",]+)(?P<opts>[^"]*)"',
)

# `if req.SenderID == "" {` — the handler's non-empty guard. Each one is a
# 400 the schema must mirror as `required` + `minLength: 1`.
_EMPTY_GUARD = re.compile(r'if\s+req\.(?P<go>\w+)\s*==\s*""\s*\{')


def _braced_body(source: str, header: str) -> str:
    """Return the ``{ ... }`` body that follows ``header``.

    Brace-counted rather than lazily regex-matched: the bodies carry doc
    comments containing braces, and a non-greedy ``.*?\\}`` would stop at
    the first one.
    """
    start = source.index(header)
    cursor = source.index("{", start)
    depth = 0
    for i in range(cursor, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[cursor + 1:i]
    raise AssertionError(f"unterminated body for {header!r}")


def _struct_body(source: str, name: str) -> str:
    return _braced_body(source, f"type {name} struct {{")


def _field_lines(struct_name: str):
    """Yield the parsed ``(go_ident, json_name, tag_opts)`` of each field."""
    body = _struct_body(_TYPES_GO.read_text(encoding="utf-8"), struct_name)
    for line in body.splitlines():
        # Skip comment lines so a `json:"..."` mentioned in prose (the
        # doc comments here quote wire keys freely) is never read as a
        # field declaration.
        if line.lstrip().startswith("//"):
            continue
        match = _FIELD_LINE.match(line)
        if match:
            yield match.group("go"), match.group("name"), match.group("opts")


def _go_fields(struct_name: str) -> dict[str, bool]:
    """Map JSON field name → ``True`` when the field carries no ``omitempty``.

    For a struct Go MARSHALS that means "always present", which is
    exactly the schema's notion of ``required``. For a struct Go only
    unmarshals it means nothing at all — see :class:`TestRequestParity`.
    """
    return {
        name: "omitempty" not in opts for _, name, opts in _field_lines(struct_name)
    }


def _go_ident_to_json(struct_name: str) -> dict[str, str]:
    """Map Go field identifier → JSON key, e.g. ``SenderID`` → ``sender_id``."""
    return {go: name for go, name, _ in _field_lines(struct_name)}


def _handler_required_json_names(struct_name: str, handler: str) -> set[str]:
    """JSON keys the handler rejects as empty with a 400.

    This — not the struct tags — is what makes a field required on a
    body Go only decodes. Reads the `if req.X == "" {` guards out of
    the handler and translates them through the struct's tags.
    """
    body = _braced_body(
        _HANDLERS_GO.read_text(encoding="utf-8"), f"func (s *Server) {handler}(",
    )
    idents = _go_ident_to_json(struct_name)
    return {
        idents[m.group("go")]
        for m in _EMPTY_GUARD.finditer(body)
        if m.group("go") in idents
    }


def _definition(name: str) -> dict:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema["definitions"][name]


@pytest.mark.parametrize(
    ("struct_name", "definition_name"), sorted(_STRUCT_TO_DEFINITION.items()),
)
class TestPayloadStructParity:
    """Field-set parity, which holds in BOTH directions.

    Tags name the wire keys whether Go is encoding or decoding, so this
    assertion is valid for the request struct and the response structs
    alike. Requiredness is where the two directions part company — see
    :class:`TestRequestParity` and :class:`TestResponseParity`.
    """

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


@pytest.mark.parametrize(
    ("struct_name", "definition_name"), sorted(_RESPONSE_STRUCTS.items()),
)
class TestResponseParity:
    """Requiredness for the structs Go MARSHALS."""

    def test_required_matches_omitempty(self, struct_name, definition_name):
        """Schema `required` == the Go fields lacking `omitempty`.

        These are the same statement in two languages *for a response*:
        a field with no `omitempty` always marshals, so a consumer may
        rely on it. If someone adds `omitempty` to a field the schema
        still calls required, clients that stopped null-guarding it
        break — this is where that shows up.
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


@pytest.mark.parametrize(
    ("struct_name", "definition_name"), sorted(_REQUEST_STRUCTS.items()),
)
class TestRequestParity:
    """Requiredness for a struct Go only UNMARSHALS.

    `omitempty` is a *marshalling* directive; `encoding/json` ignores it
    when decoding, so the request struct's tags say nothing about which
    fields a caller must send. Applying the response rule here reads a
    coincidence as a contract: `SenderID`/`Content` happen to carry no
    `omitempty`, but what actually makes them required is the handler's
    explicit `== ""` 400. Pinning against the tags would mean a
    behaviour-free Go edit (adding `,omitempty` while reusing the struct
    as a response body, or tidying it for consistency with its siblings)
    turns this red and invites "fixing" it by dropping the field from
    the schema's `required` — silently deleting the send-side guard.

    So the assertion reads the guards themselves. That also makes this
    the pin that catches an under-constrained schema: a handler `== ""`
    check with no `minLength` counterpart is a 400 the send-side
    validator cannot see coming.
    """

    def test_required_matches_handler_guards(self, struct_name, definition_name):
        guarded = _handler_required_json_names(struct_name, _PUBLISH_HANDLER)
        schema_required = set(_definition(definition_name).get("required", []))
        assert guarded == schema_required, (
            f"{_PUBLISH_HANDLER} (internal/server/channel_handlers.go) 400s on "
            f"empty {sorted(guarded)}, but definitions.{definition_name} "
            f"(schemas/channel.schema.json) requires {sorted(schema_required)}.\n"
            "  only guarded:  "
            f"{sorted(guarded - schema_required)} — publishable past the "
            "validator, then rejected on the wire\n"
            "  only required: "
            f"{sorted(schema_required - guarded)} — the schema invents a rule "
            "the orchestrator does not enforce"
        )

    def test_guarded_fields_are_pinned_non_empty(self, struct_name, definition_name):
        """`required` alone is not the same guard.

        A key present with an empty string satisfies `required` and
        still earns the handler's 400, so every guarded field needs
        `minLength: 1` too — the floor `sender_id` and `content` both
        carry.
        """
        properties = _definition(definition_name).get("properties", {})
        missing = sorted(
            name
            for name in _handler_required_json_names(struct_name, _PUBLISH_HANDLER)
            if properties.get(name, {}).get("minLength", 0) < 1
        )
        assert not missing, (
            f"definitions.{definition_name}: {missing} are rejected as empty by "
            f"{_PUBLISH_HANDLER} but carry no `minLength: 1`, so an empty value "
            "validates clean and is then 400ed on the wire"
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

    def test_go_identifiers_are_seen(self):
        """The identifier→key map is what translates a handler guard.

        If `_FIELD_LINE` stopped capturing the leading identifier, every
        guard would fail the `in idents` filter and
        :class:`TestRequestParity` would compare two empty sets.
        """
        idents = _go_ident_to_json("publishMessageRequest")
        assert idents.get("SenderID") == "sender_id"
        assert idents.get("Content") == "content"

    def test_handler_guards_are_seen(self):
        """The same no-op guard, one layer up: a handler body that parsed
        to zero `== ""` checks would make the request-side pin vacuous."""
        guarded = _handler_required_json_names(
            "publishMessageRequest", _PUBLISH_HANDLER,
        )
        assert "sender_id" in guarded, (
            f"parsed no `sender_id` guard out of {_PUBLISH_HANDLER} — the "
            "handler layout in internal/server/channel_handlers.go likely "
            "changed and this pin has stopped reading it"
        )
