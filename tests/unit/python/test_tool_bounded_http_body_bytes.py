"""``http_request`` stops reading a response body at a bound on its bytes
(ISSUE-0215, point 3).

The tool reads a body until its text is over the 100 KB cap.  A body
that goes on and on and decodes to almost no text never got there: it
was read to its end, or to the request's 30-second timeout, which cost
the model the status and the headers too.  Reading now also stops once
40 times the cap in the body's own bytes have been read, and the body
the model is shown says that it was not read to its end.

These tests pin the stop, what the model is told, where exactly the
bound lies, and that it never cuts a body that is text from end to end,
whichever charset it is in.  What a body under the bound gives is in
``test_tool_bounded_http_body.py``, whose local server and set-up are
used here.
"""

from __future__ import annotations

import asyncio
import codecs
import unicodedata
from typing import Any

import pytest
from aiohttp import web

from agents.tools import bounded_io, builtin
from agents.tools.builtin import MAX_OUTPUT_BYTES

from . import test_tool_bounded_http_body as _body_tests

MB = _body_tests.MB
MEMORY_BUDGET = _body_tests.MEMORY_BUDGET
NOTICE = _body_tests.NOTICE
READ_BOUND = _body_tests.READ_BOUND
TO_KANJI = _body_tests.TO_KANJI
TO_ASCII = _body_tests.TO_ASCII

#: What the body ends with when reading stopped on bytes, not on text.
BYTES_NOTICE = "\n... [truncated: the response was not read past 4064 KB]"

# The other file's fixtures: the tool with a gate that allows 127.0.0.1
# (used by every test here) and a local server for one handler.
_tools = _body_tests._tools
serve = _body_tests.serve
_measured = _body_tests._measured


@pytest.mark.parametrize("start", [b"", b"what came first"], ids=["no text", "some text first"])
async def test_a_body_with_little_text_that_never_ends_is_cut_and_says_so(
    serve: Any, start: bytes,
) -> None:
    """The server sends escape sequences for as long as the client reads.
    Reading used to go on until the request's 30-second timeout, which
    cost the model the status and the headers as well.  It now stops at
    the bound on bytes, and the body says that it was not read to its
    end; the ten seconds allowed here are far more than that takes."""
    piece = TO_KANJI * (64 * 1024 // 3)
    sent = {"bytes": 0}
    done = asyncio.Event()

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            headers={"Content-Type": "text/plain; charset=iso2022_jp"},
        )
        await response.prepare(request)
        try:
            await response.write(start)
            while True:
                await response.write(piece)
                sent["bytes"] += len(piece)
        except (ConnectionError, RuntimeError):
            pass
        finally:
            done.set()
        return response

    url = await serve(handler)
    result, peak = await asyncio.wait_for(_measured(lambda: builtin.http_request(url)), 10)
    await asyncio.wait_for(done.wait(), 10)

    assert result.success is True
    assert result.data["status"] == 200
    assert result.data["headers"]["Content-Type"] == "text/plain; charset=iso2022_jp"
    assert result.data["body"] == start.decode() + BYTES_NOTICE
    assert sent["bytes"] >= READ_BOUND - len(start), "it stopped before the bound"
    assert peak < MEMORY_BUDGET, f"the agent allocated {peak / MB:.1f} MB"


@pytest.mark.parametrize(
    ("over", "ends_with"),
    [(0, ""), (1, BYTES_NOTICE)],
    ids=["exactly the bound: whole", "one byte more: marked"],
)
async def test_the_bound_on_bytes_read_is_exact(serve: Any, over: int, ends_with: str) -> None:
    """A body of exactly the bound is read to its end and is whole.  One
    byte more and reading stops; what was read is kept and the body says
    it was not read to its end, even if that byte was its last."""
    tail = TO_ASCII + b"end"
    total = READ_BOUND + over
    pad = b"x" * ((total - len(tail)) % 3)
    body = pad + TO_KANJI * ((total - len(tail) - len(pad)) // 3) + tail
    assert len(body) == total

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            headers={"Content-Type": "text/plain; charset=iso2022_jp"},
        )
        await response.prepare(request)
        for at in range(0, len(body), 64 * 1024):
            await response.write(body[at:at + 64 * 1024])
        return response

    result = await builtin.http_request(await serve(handler))
    assert result.data["body"] == pad.decode() + "end" + ends_with


#: A page of text, exactly the cap, spelt in the most bytes each charset
#: allows.  The last is the costliest spelling any charset Python decodes
#: has: 79 bytes on the wire for a letter that is two bytes of UTF-8.
_LONGEST_NAME = "ARABIC LETTER BEH WITH THREE DOTS POINTING UPWARDS BELOW AND TWO DOTS ABOVE"
_WASTEFUL_PAGES: dict[str, tuple[str, bytes, str]] = {
    "utf-32, four bytes a letter": ("utf-32", "x".encode("utf-32-le"), "x"),
    "utf-7, a shift sequence per character": ("utf-7", b"+AH4-", "~"),
    "unicode_escape, the eight-digit form": ("unicode_escape", rb"\U0000007e", "~"),
    "unicode_escape, the longest character name": (
        "unicode_escape",
        b"\\N{" + _LONGEST_NAME.encode() + b"}",
        unicodedata.lookup(_LONGEST_NAME),
    ),
}


@pytest.mark.parametrize("name", _WASTEFUL_PAGES)
@pytest.mark.parametrize("extra", [0, 1], ids=["a full page", "one character more"])
async def test_a_page_of_text_is_never_cut_by_the_bound_on_bytes(
    serve: Any, name: str, extra: int,
) -> None:
    """The bound is on the body's own bytes, the cap on its text.  However
    many bytes a charset spends on a character, a body that is text from
    end to end fills the page before it reaches the bound: a full page
    comes back whole, and one character more is cut by the cap, with the
    cap's notice and not the other one."""
    charset, spelling, letter = _WASTEFUL_PAGES[name]
    assert spelling.decode(charset) == letter
    letters = MAX_OUTPUT_BYTES // len(letter.encode()) + extra
    body = spelling * letters
    assert len(body) <= READ_BOUND, "the costliest page must fit under the bound"

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            headers={"Content-Type": f"text/plain; charset={charset}"},
        )
        await response.prepare(request)
        for at in range(0, len(body), 64 * 1024):
            await response.write(body[at:at + 64 * 1024])
        return response

    result = await builtin.http_request(await serve(handler))
    page = letter * (MAX_OUTPUT_BYTES // len(letter.encode()))
    assert result.data["body"] == page + (NOTICE if extra else "")


async def test_a_body_cut_on_bytes_with_no_way_to_measure_its_text_says_so(serve: Any) -> None:
    """Some charsets cannot be decoded piece by piece, so the text cannot
    be measured as it arrives.  Reading then stops at the bound on the
    bytes that are kept.  That stop was silent when the bytes held less
    than a page of text: the body came back short with nothing to say so.

    No charset Python ships gets there with real text, so the test
    registers one: it decodes to nothing, and only in one go."""

    def _decode(data: Any, errors: str = "strict") -> tuple[str, int]:
        return "", len(data)

    class _Fails(codecs.IncrementalDecoder):
        def decode(self, data: Any, final: bool = False) -> str:
            raise ValueError("this charset is not decoded in pieces")

    def _search(name: str) -> codecs.CodecInfo | None:
        if name != "x_persatrix_test_nothing":
            return None
        return codecs.CodecInfo(
            encode=lambda text, errors="strict": (b"", len(text)),
            decode=_decode, incrementaldecoder=_Fails, name=name,
        )

    codecs.register(_search)
    try:
        async def handler(request: web.Request) -> web.StreamResponse:
            response = web.StreamResponse(
                headers={"Content-Type": "text/plain; charset=x_persatrix_test_nothing"},
            )
            await response.prepare(request)
            for _ in range(16):
                await response.write(b"\0" * (64 * 1024))
            return response

        result = await builtin.http_request(await serve(handler))
    finally:
        codecs.unregister(_search)
    assert result.data["body"] == "\n... [truncated: the response was not read past 464 KB]"


class _FakeResponse:
    """A response whose body arrives in exactly the given reads."""

    def __init__(self, reads: list[bytes], charset: str) -> None:
        self._reads = list(reads)
        self._charset = charset
        self.content = self

    def get_encoding(self) -> str:
        return self._charset

    async def read(self, _size: int) -> bytes:
        return self._reads.pop(0) if self._reads else b""


def _reads_up_to(total: int, last: bytes) -> list[bytes]:
    """Reads of escape sequences that add up to *total* bytes, then *last*."""
    piece = TO_KANJI * (60_000 // 3)
    reads = [piece] * (total // len(piece))
    reads.append(TO_KANJI * ((total - len(reads) * len(piece)) // 3))
    assert sum(map(len, reads)) == total, "pick a total that is a multiple of three"
    return [*reads, last]


async def test_a_read_that_passes_both_bounds_is_a_cut_on_text() -> None:
    """The read that takes the bytes past their bound can also take the
    text past the cap.  That is the cap's cut, with the cap's notice: the
    result must not also say that reading stopped on bytes."""
    cap = 1_000
    bound = 40 * cap + 64 * 1024
    reads = _reads_up_to(bound - 2, TO_ASCII + b"x" * (cap + 1) + b"never read")
    body = await bounded_io.read_body_text(_FakeResponse(reads, "iso2022_jp"), cap)  # type: ignore[arg-type]
    assert body.stopped_at_bytes is None
    assert body.text == "x" * (cap + 1) + "never read"


async def test_a_character_half_read_at_the_stop_adds_nothing() -> None:
    """Reading stops in the middle of a character: one byte of a two-byte
    kanji has arrived.  The text ends with what was whole; the half is
    dropped, not turned into a replacement character."""
    cap = 1_000
    bound = 40 * cap + 64 * 1024
    kanji = "\u65e5".encode("iso2022_jp")[3:5]
    reads = _reads_up_to(bound - 2, kanji + kanji[:1])
    body = await bounded_io.read_body_text(_FakeResponse(reads, "iso2022_jp"), cap)  # type: ignore[arg-type]
    assert body == bounded_io.BodyText("\u65e5", bound)
