"""Shared leak probe for tests that check a log record carries no secret.

A leak test hands a log record (or a rendered log line) and the secret
words to :func:`assert_absent`, which fails if any of them appears. For a
record it reads everything on the record that a log sink could write,
because the sinks write more than the message: the JSON renderer
(``agents/observability/logging.py``) writes a record's ``extra`` fields
under their own names, and it and the log shipper (``log_shipper._coerce``)
write a value that is not plain JSON as its ``repr()``, which can say more
than its ``str()``. What the renderer adds from outside the record, such as
bound structlog context or a pretty traceback's local variables, is only on
the rendered line, so a test that needs it probes the line.

Probe for invented words, or a phrase no path or name could hold. The probe
ignores letter case, and a record also says where the log call ran (the
checkout path, the logger, thread and task names), so a real word can match
a folder name and fail a test that leaks nothing: a ``"Rust"`` preference
once matched a ``rustpin/`` checkout (PR #1010).

History: six leak tests each turned a whole record into text their own
way: joining each value's ``str()``, taking ``repr(rec.__dict__)``, or
(after PR #1010) reading each attribute's name, ``str()`` and ``repr()``.
Five of them matched the secret only in the letter case it was written
in. Review finding F-11 of #1010 asked for one shared probe: this module
keeps the last of those readings, ignores letter case on both sides, and
all six tests now use it, as do the shadow-trace tests.

Importable as ``_log_probe_helpers`` because ``tests/conftest.py`` puts
``tests/`` on ``sys.path``."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

__all__ = ["assert_absent", "record_text"]


def _texts(value: object) -> Iterator[str]:
    """``value`` as ``str()`` and ``repr()``, then each key and item held in
    it, the way the log shipper writes them: a string as the text itself,
    even when a ``str`` subclass hides it from ``str()``, and a dict key as
    its ``str()``. A container's ``repr()`` escapes a quote or a line break
    in a string it holds."""
    yield str.__str__(value) if isinstance(value, str) else str(value)
    yield repr(value)
    if isinstance(value, dict):
        for key, item in dict.items(value):
            yield from _texts(key)
            yield from _texts(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _texts(item)


def _json_texts(text: str) -> Iterator[str]:
    """Each JSON line of ``text`` decoded, read as :func:`_texts` reads a
    value: the JSON renderer escapes an accent, a quote or a backslash."""
    for line in text.splitlines():
        try:
            decoded = json.loads(line)
        except ValueError:
            continue
        yield from _texts(decoded)


def _probed_text(where: object) -> str:
    """What :func:`assert_absent` probes: a record read whole, or a text
    plus its JSON lines decoded."""
    if isinstance(where, logging.LogRecord):
        return record_text(where)
    if isinstance(where, str):
        return " ".join([where, *_json_texts(where)])
    raise TypeError(f"expected a log record or a text, got {type(where).__name__}")


def record_text(*records: logging.LogRecord) -> str:
    """Everything on the records that could reach a log sink, as one text:
    each attribute's name and its value as :func:`_texts` reads it, plus the
    formatted message. The structured ``extra`` fields land on a record as
    attributes, so they are read the same way."""
    parts: list[str] = []
    for rec in records:
        for name, value in vars(rec).items():
            parts.append(name)
            parts.extend(_texts(value))
        parts.append(rec.getMessage())
    return " ".join(parts)


def assert_absent(where: str | logging.LogRecord, probe: str, *more: str) -> None:
    """Fail if any probe appears in ``where``: a log record, read whole as
    :func:`record_text` reads it, or text such as a rendered log line, whose
    JSON lines are also read decoded.

    At least one probe is required, since a check with none passes on
    anything; anything but a record or a text is an error, since a mock
    would pass as text. Letter case is ignored on both sides
    (``str.casefold``), so ``"Zyxwen"`` also catches ``"ZYXWEN"``: folding
    only the text would leave a mixed-case probe unable to match, and the
    check could never fail. The failure names the probe and quotes the
    folded text around it, whatever pytest's verbosity."""
    text = _probed_text(where).casefold()
    for word in (probe, *more):
        secret = word.casefold()
        at = text.find(secret)
        if at != -1:
            around = text[max(0, at - 60):at + len(secret) + 60]
            raise AssertionError(f"{word!r} is contained here: ...{around}...")
