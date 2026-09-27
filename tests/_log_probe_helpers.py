"""Shared leak probe for tests that check a log record carries no secret.

A leak test hands a log record (or a rendered log line) and the secret
words to :func:`assert_absent`, which fails if any of them appears. It
reads everything a log sink could write, because the sinks write more
than the message: the JSON renderer (``agents/observability/logging.py``)
writes a record's ``extra`` fields under their own names, and it and the
log shipper (``log_shipper._coerce``) write a value that is not plain
JSON as its ``repr()``, which can say more than its ``str()``.

History: six leak tests each turned a whole record into text their own
way: joining each value's ``str()``, taking ``repr(rec.__dict__)``, or
(after PR #1010) reading each attribute's name, ``str()`` and ``repr()``.
Five of them matched the secret only in the letter case it was written
in. Review finding F-11 of #1010 asked for one shared probe: this module
keeps the last of those readings, ignores letter case on both sides, and
all six tests now use it.

Importable as ``_log_probe_helpers`` because ``tests/conftest.py`` puts
``tests/`` on ``sys.path``; that file also registers this module with
pytest, so a failed probe shows where the secret sits."""

from __future__ import annotations

import logging

__all__ = ["assert_absent", "record_text"]


def record_text(*records: logging.LogRecord) -> str:
    """Everything on the records that could reach a log sink, as one text:
    each attribute's name, and its value both as ``str()`` and as
    ``repr()``, plus the formatted message. The structured ``extra``
    fields land on a record as attributes, so they are read the same way."""
    parts: list[str] = []
    for rec in records:
        parts.extend(f"{k} {v} {v!r}" for k, v in vars(rec).items())
        parts.append(rec.getMessage())
    return " ".join(parts)


def assert_absent(where: str | logging.LogRecord, probe: str, *more: str) -> None:
    """Fail if any probe appears in ``where``: a log record, read whole as
    :func:`record_text` reads it, or text such as a rendered log line.

    At least one probe is required, since a check with none passes on
    anything. Letter case is ignored on both sides, so ``"Nightjar"`` also
    catches ``"NIGHTJAR"``: folding only the text would leave a mixed-case
    probe unable to match, and the check could never fail."""
    text = record_text(where) if isinstance(where, logging.LogRecord) else where
    text = text.lower()
    for word in (probe, *more):
        secret = word.lower()
        assert secret not in text
