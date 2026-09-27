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
import logging

import pytest
from _log_probe_helpers import assert_absent, record_text

# An invented word: no checkout path, host, logger, thread or task name can
# contain it by accident, so a record that never carried it passes.
_SECRET = "Zyxwen"


class _Who(enum.Enum):
    """An ``extra`` value whose ``str()`` is only ``_Who.NAME``, while its
    ``repr()``, which the JSON renderer writes, carries the secret."""

    NAME = _SECRET


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
        said = f"{_SECRET} said \"don't\""
        with pytest.raises(AssertionError):
            assert_absent(logging.makeLogRecord({"msg": "tier_admitted", "note": said}), said)


class TestLetterCase:
    """Case is folded on both sides. Folding only the record's text would
    leave a mixed-case probe such as ``"Nightjar"`` unable to match, so the
    check could never fail."""

    @pytest.mark.parametrize(
        ("carried", "probe"),
        [(_SECRET.upper(), _SECRET), (_SECRET, _SECRET.upper())],
        ids=["mixed-case-probe", "upper-case-probe"],
    )
    def test_case_does_not_hide_a_leak(self, carried: str, probe: str) -> None:
        with pytest.raises(AssertionError):
            assert_absent(logging.makeLogRecord({"msg": f"admitted {carried}"}), probe)


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

    def test_every_probe_is_checked(self) -> None:
        rec = logging.makeLogRecord({"msg": "tier_admitted", "item_id": _SECRET})
        with pytest.raises(AssertionError):
            assert_absent(rec, "Vexmoor", _SECRET)


class TestWhatItReads:
    def test_a_rendered_line_is_probed_as_it_stands(self) -> None:
        with pytest.raises(AssertionError):
            assert_absent(f'{{"event": "tier_admitted", "who": "{_SECRET}"}}', _SECRET)
        assert_absent('{"event": "tier_admitted"}', _SECRET)

    def test_several_records_read_as_one_text(self) -> None:
        clean = logging.makeLogRecord({"msg": "tier_admitted"})
        leaky = logging.makeLogRecord({"msg": "tier_admitted", "item_id": _SECRET})
        with pytest.raises(AssertionError):
            assert_absent(record_text(clean, leaky), _SECRET)
        assert_absent(record_text(clean, clean), _SECRET)

    def test_no_records_read_as_empty_text(self) -> None:
        """A test that captured nothing has nothing to leak."""
        assert record_text() == ""


class TestHowItFails:
    def test_the_failure_shows_where_the_secret_sits(self) -> None:
        """pytest explains a failed ``assert`` only in modules it rewrites;
        a helper module is not one unless ``tests/conftest.py`` registers
        it, and without that the failure is a bare ``AssertionError``."""
        rec = logging.makeLogRecord({"msg": f"admitted {_SECRET}"})
        with pytest.raises(AssertionError) as caught:
            assert_absent(rec, _SECRET)
        explained = str(caught.value)
        assert "'zyxwen' is contained here" in explained

    def test_no_probe_is_an_error_not_a_pass(self) -> None:
        """With no probe the check would pass on any record."""
        rec = logging.makeLogRecord({"msg": "tier_admitted"})
        no_probes: tuple[str, ...] = ()
        with pytest.raises(TypeError):
            assert_absent(rec, *no_probes)
