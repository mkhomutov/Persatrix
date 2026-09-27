"""Unit tests for ``tests/_log_probe_helpers.py``.

Several leak tests turn a whole log record into text and check that a
secret is not in it; the helper module gives them one probe to do that
with. A probe that cannot fail proves nothing, so these tests pin the
ways it must fail: every route a record has to a log sink trips it,
letter case on either side does not hide a leak, and the failure shows
where the secret sits.
"""

from __future__ import annotations

import enum
import json
import logging
from datetime import date
from unittest.mock import MagicMock

import pytest
from _log_probe_helpers import assert_absent, record_text

# An invented word: no checkout path, host, logger, thread or task name can
# contain it by accident, so a record that never carried it passes.
_SECRET = "Zyxwen"

# A string holding both kinds of quote: its ``repr()``, and the ``repr()`` of
# any container holding it, escapes one of them.
_SAID = f"{_SECRET} said \"don't\""


class _Who(enum.Enum):
    """An ``extra`` value whose ``str()`` is only ``_Who.NAME``, while its
    ``repr()``, which the JSON renderer writes, carries the secret."""

    NAME = _SECRET


class _Masked(str):
    """A ``str`` whose ``str()`` and ``repr()`` hide its text, which the JSON
    renderer and the log shipper still write as it stands."""

    def __str__(self) -> str:
        return "***"

    def __repr__(self) -> str:
        return "_Masked(***)"


class TestEveryRouteTripsTheProbe:
    """Each way a record can carry text to a sink: the message (raw or
    formatted), its args, the exception and stack text, and an ``extra``
    field's value, its ``repr()`` or its name."""

    @pytest.mark.parametrize(
        "fields",
        [
            {"msg": f"admitted {_SECRET}"},
            {"msg": "admitted %s%s", "args": (_SECRET[:3], _SECRET[3:])},
            {"msg": "tier_admitted", "args": {"who": _SECRET}},
            {"msg": "tier_admitted", "exc_text": f"ValueError: {_SECRET}"},
            {"msg": "tier_admitted", "stack_info": f"Stack: {_SECRET}"},
            {"msg": "tier_admitted", "item_id": _SECRET},
            {"msg": "tier_admitted", "prefs": [_SECRET]},
            {"msg": "tier_admitted", "who": _Who.NAME},
            {"msg": "tier_admitted", f"pref_{_SECRET}": True},
        ],
        ids=[
            "message", "formatted-args", "unused-args", "exc_text", "stack_info",
            "str-extra", "list-extra", "repr-only-extra", "extra-name",
        ],
    )
    def test_the_route_trips_the_probe(self, fields: dict[str, object]) -> None:
        with pytest.raises(AssertionError):
            assert_absent(logging.makeLogRecord(fields), _SECRET)

    def test_a_string_is_read_as_written(self) -> None:
        """A string holding both kinds of quote has one escaped in its
        ``repr()``, so only its ``str()`` carries a quoted secret verbatim."""
        with pytest.raises(AssertionError):
            assert_absent(logging.makeLogRecord({"msg": "tier_admitted", "note": _SAID}), _SAID)

    @pytest.mark.parametrize(
        ("value", "probe"),
        [
            ([_SAID], _SAID),
            ({"note": _SAID}, _SAID),
            ({date(2026, 9, 27): 3}, "2026-09-27"),
            (_Masked(_SECRET), _SECRET),
        ],
        ids=["string-in-a-list", "string-in-a-dict", "dict-key", "masked-str"],
    )
    def test_what_the_shipper_writes_trips_the_probe(self, value: object, probe: str) -> None:
        """The log shipper writes a string held in a container, and a ``str``
        subclass, as the text itself, and a dict key as its ``str()``, where a
        ``repr()`` would escape the quote, hide the text or show the key's
        ``repr()``."""
        rec = logging.makeLogRecord({"msg": "tier_admitted", "note": value})
        with pytest.raises(AssertionError):
            assert_absent(rec, probe)


class TestLetterCase:
    """Case is folded on both sides, for a record and for a rendered line
    alike. Folding only the record's text would leave a mixed-case probe
    such as ``"Zyxwen"`` unable to match, so the check could never fail. The
    fold is ``str.casefold``: ``str.lower`` keeps ``"ß"`` apart from ``"SS"``."""

    @pytest.mark.parametrize("as_line", [False, True], ids=["record", "rendered-line"])
    @pytest.mark.parametrize(
        ("carried", "probe"),
        [(_SECRET.upper(), _SECRET), (_SECRET, _SECRET.upper()), ("STRASSE", "Straße")],
        ids=["mixed-case-probe", "upper-case-probe", "sharp-s"],
    )
    def test_case_does_not_hide_a_leak(self, carried: str, probe: str, as_line: bool) -> None:
        where: str | logging.LogRecord = (
            f'{{"event": "admitted", "who": "{carried}"}}' if as_line
            else logging.makeLogRecord({"msg": f"admitted {carried}"})
        )
        with pytest.raises(AssertionError):
            assert_absent(where, probe)


class TestWhatPasses:
    def test_a_real_record_without_the_secret_passes(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The probe also reads where the log call ran (file, function,
        logger, thread), so this uses a real call, not a bare record."""
        with caplog.at_level(logging.INFO, logger="agents.probe_test"):
            logging.getLogger("agents.probe_test").info(
                "tier_admitted", extra={"item_id": "user:p-7f3a"},
            )
        (rec,) = caplog.records
        assert_absent(rec, _SECRET)

    @pytest.mark.parametrize(
        "probes",
        [
            (_SECRET, "Vexmoor", "Quillonlang"),
            ("Vexmoor", _SECRET, "Quillonlang"),
            ("Vexmoor", "Quillonlang", _SECRET),
        ],
        ids=["first", "middle", "last"],
    )
    def test_every_probe_is_checked(self, probes: tuple[str, ...]) -> None:
        rec = logging.makeLogRecord({"msg": "tier_admitted", "item_id": _SECRET})
        with pytest.raises(AssertionError):
            assert_absent(rec, *probes)


class TestWhatItReads:
    def test_a_rendered_line_is_probed_as_it_stands(self) -> None:
        with pytest.raises(AssertionError):
            assert_absent(f'{{"event": "tier_admitted", "who": "{_SECRET}"}}', _SECRET)
        assert_absent('{"event": "tier_admitted"}', _SECRET)

    @pytest.mark.parametrize("secret", ["Zyxwën", _SAID], ids=["accented", "quoted"])
    def test_a_json_line_is_also_read_decoded(self, secret: str) -> None:
        """The JSON renderer escapes an accent, a quote or a backslash, so the
        secret is not on the line as written."""
        line = json.dumps({"event": "tier_admitted", "who": secret})
        assert secret not in line
        with pytest.raises(AssertionError):
            assert_absent(line, secret)

    def test_several_records_read_as_one_text(self) -> None:
        clean = logging.makeLogRecord({"msg": "tier_admitted"})
        leaky = logging.makeLogRecord({"msg": "tier_admitted", "item_id": _SECRET})
        with pytest.raises(AssertionError):
            assert_absent(record_text(clean, leaky, clean), _SECRET)
        assert_absent(record_text(clean, clean), _SECRET)

    def test_no_records_read_as_empty_text(self) -> None:
        """A test that captured nothing has nothing to leak."""
        assert record_text() == ""


class TestHowItFails:
    def test_the_failure_shows_where_the_secret_sits(self) -> None:
        """The helper writes its own message: the probe and the text around
        it. pytest's explanation of a failed ``assert`` is cut to a line of
        spaces at ``-v`` once a record runs past about 640 characters."""
        rec = logging.makeLogRecord({"msg": f"admitted {_SECRET}", "pad": "x" * 2000})
        with pytest.raises(AssertionError) as caught:
            assert_absent(rec, _SECRET)
        explained = str(caught.value)
        assert "'Zyxwen' is contained here" in explained
        assert "admitted zyxwen" in explained

    def test_no_probe_is_an_error_not_a_pass(self) -> None:
        """With no probe the check would pass on any record."""
        rec = logging.makeLogRecord({"msg": "tier_admitted"})
        no_probes: tuple[str, ...] = ()
        with pytest.raises(TypeError):
            assert_absent(rec, *no_probes)

    def test_a_mock_is_an_error_not_a_pass(self) -> None:
        """A logger mock, or one of its calls, answers any attribute, so read
        as text the check could never fail."""
        logger = MagicMock()
        logger.info("admitted %s", _SECRET)
        for where in (logger, logger.info.call_args):
            with pytest.raises(TypeError):
                assert_absent(where, _SECRET)
