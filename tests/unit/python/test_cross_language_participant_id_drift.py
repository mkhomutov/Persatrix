"""Cross-language drift pin for the participant id rule (ISSUE-0209).

A participant id is written down in four languages' worth of places, and
for seven months two of them disagreed.  The orchestrator's channels
package and the channel schema accepted ``User_1``, ``alice_b``, ``a``,
``Bob`` and ``ops-``; the agents' own check refused them.  So a message
from such a member was stored, sent to every persona and refused by each,
and the member was told nothing.  No test compared the copies.

This file compares them.  Every copy of the rule must be the same pattern
text, and that text must behave as the rule says:

* Go, the channels package: ``participantIDPattern`` in
  ``internal/channels/identifiers.go``;
* Go, the accounts package: ``participantIDRegex`` in
  ``internal/accounts/accounts.go``;
* the channel schema, four places: a member as a string, a member's
  ``id``, ``escalation_chair_id`` and ``autonomous.convener``;
* the agent schema's agent ``id`` (an agent id is a participant id);
* Python: ``agents.participant`` and ``agents.channel_validation``;
* the web console: ``PARTICIPANT_ID_RE`` in ``web/src/lib/mentions.js``,
  which decides what the composer lifts into a mention.

Python copies are imported and compared directly.  The Go and JavaScript
copies have no way to be imported, so they are read as text, the sibling
drift files' posture, and a parse miss fails with what to do about it.

Equal text is not equal behaviour in one case: an id that ends in a line
break.  Go, JavaScript and the repository's config check hold ``$`` to the
end of the text; Python's ``re`` also matches before a final line break,
so the agents' two checks accept ``"ab\\n"`` (ISSUE-0280).  The tests of
what ``make validate`` refuses therefore use the validator that
``make validate`` runs (``agents/validate.py``), not plain ``jsonschema``.

Not compared here: the CLI's check (``validate_resource_id`` in
``cli/src/validation.rs``), which is hand-written code and not a pattern,
and three places that hold the same text for an agent id or an
organization id (``internal/planner/planner.go``,
``internal/security/middleware.go``, ``schemas/organization.schema.json``).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn

import pytest

from agents.channel_validation import _CHANNEL_PARTICIPANT_ID_RE
from agents.participant import _PARTICIPANT_ID_RE, validate_participant_id
from agents.validate import _Validator

_CHANNELS_GO = Path("internal/channels/identifiers.go")
_ACCOUNTS_GO = Path("internal/accounts/accounts.go")
_CHANNEL_SCHEMA = Path("schemas/channel.schema.json")
_AGENT_SCHEMA = Path("schemas/agent.schema.json")
_WEB_MENTIONS_JS = Path("web/src/lib/mentions.js")

#: The rule, asserted explicitly and not only as mutual equality: ids are
#: stored (memberships, messages, DM addresses), so even a change made to
#: every copy at once is a storage migration, and must land as a deliberate
#: edit here.
_RULE = "^[a-z0-9][a-z0-9-]*[a-z0-9]$"

#: The rule the channels package and the channel schema held until
#: ISSUE-0209.  The channels package keeps it under another name, for
#: reading what an earlier orchestrator stored.
_EARLIER_RULE = "^[A-Za-z0-9][A-Za-z0-9_-]*$"

#: The five ids of the issue: each fits the earlier rule and not the rule.
_LOOSE_IDS = ("User_1", "alice_b", "a", "Bob", "ops-")
_GOOD_IDS = ("ember-owl", "user-1", "a1", "0a", "ab")


def _parse_miss(what: str, where: Path) -> NoReturn:
    pytest.fail(
        f"could not find {what} in {where}. If it was renamed or "
        f"restructured, update the parse rule in this test to match the "
        f"new shape — the cross-language drift pin is part of the contract.",
    )


def _go_pattern(path: Path, name: str) -> str:
    """The pattern text of ``var <name> = regexp.MustCompile(`…`)`` in *path*."""
    found = re.search(
        rf"^var {re.escape(name)} = regexp\.MustCompile\(`([^`]+)`\)$",
        path.read_text(encoding="utf-8"), re.MULTILINE,
    )
    if found is None:
        _parse_miss(f"`var {name} = regexp.MustCompile(`…`)`", path)
    return found.group(1)


def _js_pattern(path: Path, name: str) -> str:
    """The pattern text of ``const <name> = /…/;`` in *path*."""
    found = re.search(
        rf"^const {re.escape(name)} = /([^/\n]+)/;$",
        path.read_text(encoding="utf-8"), re.MULTILINE,
    )
    if found is None:
        _parse_miss(f"`const {name} = /…/;`", path)
    return found.group(1)


def _channel_schema() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(_CHANNEL_SCHEMA.read_text(encoding="utf-8"))
    return loaded


def _channel_schema_patterns() -> dict[str, str]:
    """The four places of the channel schema that hold a participant id."""
    definitions = _channel_schema()["definitions"]
    as_string, as_object = definitions["member"]["oneOf"]
    channel = definitions["channel"]["properties"]
    return {
        "a member written as a string": as_string["pattern"],
        "a member's id": as_object["properties"]["id"]["pattern"],
        "escalation_chair_id": channel["escalation_chair_id"]["pattern"],
        "autonomous.convener": channel["autonomous"]["properties"]["convener"]["pattern"],
    }


def _agent_schema_id_pattern() -> str:
    schema = json.loads(_AGENT_SCHEMA.read_text(encoding="utf-8"))
    pattern: str = schema["definitions"]["agent"]["properties"]["id"]["pattern"]
    return pattern


def _every_copy() -> dict[str, str]:
    return {
        "Go channels participantIDPattern": _go_pattern(_CHANNELS_GO, "participantIDPattern"),
        "Go accounts participantIDRegex": _go_pattern(_ACCOUNTS_GO, "participantIDRegex"),
        "Python agents.participant": _PARTICIPANT_ID_RE.pattern,
        "Python agents.channel_validation": _CHANNEL_PARTICIPANT_ID_RE.pattern,
        "agent schema, an agent's id": _agent_schema_id_pattern(),
        "web console PARTICIPANT_ID_RE": _js_pattern(_WEB_MENTIONS_JS, "PARTICIPANT_ID_RE"),
        **{f"channel schema, {where}": p for where, p in _channel_schema_patterns().items()},
    }


def test_there_are_ten_copies_of_the_rule() -> None:
    """The comparison below is only as good as its list.  A copy that is
    added or removed changes this count, deliberately."""
    assert len(_every_copy()) == 10


def _patterns_in(node: Any) -> Iterator[str]:
    """Every ``pattern`` keyword's text anywhere under *node*."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "pattern" and isinstance(value, str):
                yield value
            else:
                yield from _patterns_in(value)
    elif isinstance(node, list):
        for item in node:
            yield from _patterns_in(item)


def test_every_pattern_in_the_channel_schema_is_accounted_for() -> None:
    """The list above names four places of the channel schema.  This walks
    the whole schema, so a fifth place that holds a participant id cannot be
    added beside them unseen: a new ``pattern`` anywhere changes the count,
    and whoever adds it decides here whether it is a participant id.

    The rule's text is there five times: the four participant id places and
    a channel's ``name``, which has the same shape and is not a participant
    id.  The sixth pattern is a message's timestamp."""
    definitions = _channel_schema()["definitions"]
    timestamp = definitions["channelMessage"]["properties"]["timestamp"]["pattern"]
    assert definitions["channel"]["properties"]["name"]["pattern"] == _RULE
    assert Counter(_patterns_in(_channel_schema())) == Counter({_RULE: 5, timestamp: 1})


@pytest.mark.parametrize("where", sorted(_every_copy()))
def test_every_copy_is_the_rule(where: str) -> None:
    assert _every_copy()[where] == _RULE, (
        f"{where} holds another participant id rule than the rest. An id one "
        f"side accepts and another refuses is stored and then answered by nobody."
    )


@pytest.mark.parametrize("participant_id", _LOOSE_IDS)
def test_the_rule_refuses_each_id_of_the_issue(participant_id: str) -> None:
    """Checked on the Go pattern's own text, which Python's ``re`` reads as
    Go does for these five ids (not for an id that ends in a line break: the
    module docstring), and on the agents' validator."""
    assert re.match(_go_pattern(_CHANNELS_GO, "participantIDPattern"), participant_id) is None
    with pytest.raises(ValueError, match="Invalid participant_id"):
        validate_participant_id(participant_id)
    assert re.match(_EARLIER_RULE, participant_id), "and each did fit the earlier rule"


@pytest.mark.parametrize("participant_id", _GOOD_IDS)
def test_the_rule_accepts_an_ordinary_id(participant_id: str) -> None:
    assert re.match(_go_pattern(_CHANNELS_GO, "participantIDPattern"), participant_id)
    validate_participant_id(participant_id)


def test_the_earlier_rule_is_kept_in_one_place_only() -> None:
    """The channels package keeps the earlier rule to read what is already
    stored.  It guards no write, and the schema holds it nowhere: a second
    copy of it anywhere would be a second rule again."""
    assert _go_pattern(_CHANNELS_GO, "legacyParticipantIDPattern") == _EARLIER_RULE
    assert _CHANNELS_GO.read_text(encoding="utf-8").count(_EARLIER_RULE) == 1
    assert _EARLIER_RULE not in _CHANNEL_SCHEMA.read_text(encoding="utf-8")
    assert _EARLIER_RULE not in _ACCOUNTS_GO.read_text(encoding="utf-8")


def _channels_file(
    member: Any = "ada", chair: str | None = None, convener: str | None = None,
) -> dict[str, Any]:
    channel: dict[str, Any] = {"name": "planning", "members": [member, {"id": "bea"}]}
    if chair is not None:
        channel["escalation_chair_id"] = chair
    if convener is not None:
        channel["autonomous"] = {"enabled": False, "convener": convener}
    return {"max_channels": 50, "channels": [channel]}


def _make_validate_refusals(instance: dict[str, Any]) -> list[str]:
    """What ``make validate`` says against *instance*: the messages of the
    validator ``agents/validate.py`` runs each config file through.  Its
    ``pattern`` holds ``$`` to the end of the text, as Go's does; plain
    ``jsonschema`` would let an id that ends in a line break through."""
    return [error.message for error in _Validator(_channel_schema()).iter_errors(instance)]


#: An id that fits the rule but for a final line break.  Go refuses it, and
#: so must the config check; Python's own ``$`` would not.
_ENDS_IN_A_LINE_BREAK = "ab\n"


@pytest.mark.parametrize("participant_id", (*_LOOSE_IDS, _ENDS_IN_A_LINE_BREAK))
@pytest.mark.parametrize("place", ["member string", "member id", "chair", "convener"])
def test_make_validate_refuses_a_loose_id_in_each_place(place: str, participant_id: str) -> None:
    """The schema, read as ``make validate`` reads it, refuses the id
    wherever a channels file can hold one.  The members beside it fit the
    rule, so the refusal is of that one place."""
    instance = {
        "member string": _channels_file(member=participant_id),
        "member id": _channels_file(member={"id": participant_id}),
        "chair": _channels_file(chair=participant_id),
        "convener": _channels_file(convener=participant_id),
    }[place]
    refusals = _make_validate_refusals(instance)
    assert refusals, f"{participant_id!r} as {place} passed the config check"
    assert any(repr(participant_id) in message for message in refusals), refusals


@pytest.mark.parametrize("place", ["member string", "member id", "chair", "convener"])
def test_make_validate_accepts_an_id_that_fits_the_rule_in_each_place(place: str) -> None:
    """The positive control: the same four files with ``ada``, a member in
    each, pass, so a refusal above is the id's doing."""
    instance = {
        "member string": _channels_file(member="ada"),
        "member id": _channels_file(member={"id": "ada"}),
        "chair": _channels_file(chair="ada"),
        "convener": _channels_file(convener="ada"),
    }[place]
    assert _make_validate_refusals(instance) == []
