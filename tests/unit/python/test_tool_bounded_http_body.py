"""``http_request`` reads a response body no further than its cap needs,
and decodes it as aiohttp would (ISSUE-0215).

The tool used to call ``resp.text()``, which reads the whole body.  It
now reads in pieces and stops once the text is over the 100 KB cap.  A
body under the cap must come back exactly as ``resp.text(errors="replace")``
gives it, in every charset, and a body over it must be cut with the
notice.  The shell and file halves of the same change are in
``test_tool_bounded_output.py``.
"""

from __future__ import annotations

import asyncio
import importlib
import tracemalloc
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from agents.tools import builtin
from agents.tools.builtin import MAX_OUTPUT_BYTES
from agents.tools.permissions import PermissionGate
from agents.tools.registry import ToolResult, clear_registry

MB = 1024 * 1024
NOTICE = "\n... [truncated at 100 KB]"

#: The most a call may allocate in the agent while it handles a body far
#: over the cap.  The fixed code peaks near 1 MB.
MEMORY_BUDGET = 6 * MB


@pytest.fixture(autouse=True)
def _tools() -> Iterator[None]:
    clear_registry()
    importlib.reload(builtin)
    builtin.permission_gate = PermissionGate({"network": {"allow": ["127.0.0.1"]}})
    yield
    builtin.permission_gate = None
    clear_registry()


async def _measured(call: Callable[[], Awaitable[ToolResult]]) -> tuple[ToolResult, int]:
    """The call's result and the peak the agent allocated while it ran."""
    tracemalloc.start()
    try:
        result = await call()
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


# ─── http_request ────────────────────────────────────────────────────

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@pytest.fixture
async def serve() -> AsyncIterator[Callable[[Handler], Awaitable[str]]]:
    """Starts a local server for one handler and returns its URL."""
    runners: list[web.AppRunner] = []

    async def start(handler: Handler) -> str:
        app = web.Application()
        app.router.add_get("/", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        port = runner.addresses[0][1]
        return f"http://127.0.0.1:{port}/"

    yield start
    for runner in runners:
        await runner.cleanup()


@pytest.mark.parametrize(
    ("content_type", "letter"),
    [
        ("text/plain", b"x"),
        # No byte-order mark: the text cannot be measured as it arrives,
        # and the read still has to stop.
        ("text/plain; charset=utf-16", "x".encode("utf-16-le")),
    ],
    ids=["utf-8", "utf-16 with no mark"],
)
async def test_a_body_over_the_cap_is_not_read_to_its_end(
    serve: Any, content_type: str, letter: bytes,
) -> None:
    total = 48 * MB
    sent = {"bytes": 0}
    done = asyncio.Event()

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": content_type})
        await response.prepare(request)
        chunk = letter * (MB // len(letter))
        try:
            while sent["bytes"] < total:
                await response.write(chunk)
                sent["bytes"] += len(chunk)
        except (ConnectionError, RuntimeError):
            pass
        finally:
            done.set()
        return response

    url = await serve(handler)
    result, peak = await _measured(lambda: builtin.http_request(url))
    await asyncio.wait_for(done.wait(), 30)

    assert result.success is True
    assert result.data["status"] == 200
    assert result.data["body"] == "x" * MAX_OUTPUT_BYTES + NOTICE
    assert result.data["headers"]["Content-Type"] == content_type
    assert sent["bytes"] < total, "the whole body was downloaded"
    assert peak < MEMORY_BUDGET, f"the agent allocated {peak / MB:.1f} MB"


async def _as_aiohttp_reads_it(url: str) -> str:
    """What ``resp.text(errors="replace")`` gives, asked of aiohttp itself."""
    async with aiohttp.ClientSession() as session, session.get(url) as resp:
        return await resp.text(errors="replace")


_BODIES: dict[str, tuple[bytes, str]] = {
    "utf-8, no charset": ("caf\u00e9 \u20ac".encode(), "text/plain"),
    "json": (b'{"ok": true}', "application/json"),
    "latin-1 charset": ("caf\u00e9".encode("latin-1"), "text/plain; charset=iso-8859-1"),
    "utf-16 charset": ("caf\u00e9 \u20ac".encode("utf-16"), "text/plain; charset=utf-16"),
    "unknown charset": (b"plain", "text/plain; charset=no-such-charset"),
    "bytes that are not utf-8": (b"\x89PNG\r\n\x1a\n", "image/png"),
    "a character cut short at the end": (b"abc\xe2\x82", "text/plain"),
    "empty": (b"", "text/plain"),
    # A charset whose piece-by-piece decoder disagrees with a decode of
    # the whole body: no byte-order mark, a mark cut short, a body that
    # ends inside a character or an escape sequence.
    "utf-16, no byte-order mark": (
        "caf\u00e9 \u20ac".encode("utf-16-le"), "text/plain; charset=utf-16",
    ),
    "utf-16 big-endian bytes, no mark": (
        "caf\u00e9 \u20ac".encode("utf-16-be"), "text/plain; charset=utf-16",
    ),
    "utf-32, no byte-order mark": (
        "caf\u00e9 \u20ac".encode("utf-32-le"), "text/plain; charset=utf-32",
    ),
    "utf-16, one byte": (b"a", "text/plain; charset=utf-16"),
    "utf-16, an odd number of bytes": (
        "abc".encode("utf-16") + b"x", "text/plain; charset=utf-16",
    ),
    "utf-8-sig, half a mark": (b"\xef\xbb", "text/plain; charset=utf-8-sig"),
    "utf-8-sig, a mark and text": (
        "\ufeffcaf\u00e9".encode(), "text/plain; charset=utf-8-sig",
    ),
    "iso2022_jp, cut inside an escape": (
        "\u65e5\u672c".encode("iso2022_jp")[:2], "text/plain; charset=iso2022_jp",
    ),
    "iso2022_jp, cut inside a character": (
        "\u65e5\u672c".encode("iso2022_jp")[:4], "text/plain; charset=iso2022_jp",
    ),
    "utf-7": ("caf\u00e9 \u20ac".encode("utf-7"), "text/plain; charset=utf-7"),
}


@pytest.mark.parametrize("name", _BODIES)
async def test_a_body_under_the_cap_decodes_as_aiohttp_decodes_it(
    serve: Any, name: str,
) -> None:
    raw, content_type = _BODIES[name]
    expected: dict[str, str] = {}

    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(body=raw, headers={"Content-Type": content_type})

    url = await serve(handler)
    expected["body"] = await _as_aiohttp_reads_it(url)

    result = await builtin.http_request(url)
    assert result.data["body"] == expected["body"]


async def test_a_character_split_between_two_network_reads_is_whole(serve: Any) -> None:
    euro = "\u20ac".encode()

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/plain; charset=utf-8"})
        await response.prepare(request)
        for piece in (b"a" + euro[:1], euro[1:2], euro[2:] + b"b"):
            await response.write(piece)
            await asyncio.sleep(0.05)
        return response

    result = await builtin.http_request(await serve(handler))
    assert result.data["body"] == "a\u20acb"


@pytest.mark.parametrize("codec", ["base64", "hex", "zlib", "rot13"])
async def test_a_charset_that_is_not_a_text_encoding_fails_as_it_did(
    serve: Any, codec: str,
) -> None:
    """Python knows these as codecs, so aiohttp accepts the name, and
    then refuses to decode text with them.  The call fails with that same
    error, and the model is shown its message, which says what is wrong."""

    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(body=b"aGVsbG8=", headers={
            "Content-Type": f"text/plain; charset={codec}",
        })

    url = await serve(handler)
    with pytest.raises(LookupError) as theirs:
        await _as_aiohttp_reads_it(url)
    result = await builtin.http_request(url)
    assert result.success is False
    assert result.error == str(theirs.value)
    assert "not a text encoding" in result.error


async def test_an_empty_body_in_such_a_charset_is_empty_as_it_was(serve: Any) -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(body=b"", headers={"Content-Type": "text/plain; charset=base64"})

    url = await serve(handler)
    assert await _as_aiohttp_reads_it(url) == ""
    result = await builtin.http_request(url)
    assert result.success is True
    assert result.data["body"] == ""


async def test_a_byte_order_mark_split_between_two_network_reads_is_still_a_mark(
    serve: Any,
) -> None:
    raw = "caf\u00e9".encode("utf-16")

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/plain; charset=utf-16"})
        await response.prepare(request)
        for piece in (raw[:1], raw[1:3], raw[3:]):
            await response.write(piece)
            await asyncio.sleep(0.05)
        return response

    result = await builtin.http_request(await serve(handler))
    assert result.data["body"] == "caf\u00e9"


@pytest.mark.parametrize("mark", [True, False], ids=["with a mark", "no mark"])
async def test_a_utf_16_body_is_measured_as_text_not_as_bytes(serve: Any, mark: bool) -> None:
    """80,000 characters are 160 KB on the wire and 80 KB as the text the
    cap is measured on: under the cap, so whole and with no notice."""
    text = "x" * 80_000
    raw = text.encode("utf-16" if mark else "utf-16-le")

    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(body=raw, headers={"Content-Type": "text/plain; charset=utf-16"})

    result = await builtin.http_request(await serve(handler))
    assert result.data["body"] == text


@pytest.mark.parametrize("mark", [True, False], ids=["with a mark", "no mark"])
async def test_a_utf_16_body_over_the_cap_is_cut_with_the_notice(serve: Any, mark: bool) -> None:
    text = "x" * (MAX_OUTPUT_BYTES + 500)
    raw = text.encode("utf-16" if mark else "utf-16-le")

    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(body=raw, headers={"Content-Type": "text/plain; charset=utf-16"})

    result = await builtin.http_request(await serve(handler))
    assert result.data["body"] == "x" * MAX_OUTPUT_BYTES + NOTICE


async def test_a_body_that_pauses_exactly_at_the_cap_and_goes_on_is_cut(serve: Any) -> None:
    """The read that brings the text to exactly the cap must not be the
    last: one more byte follows, and the result has to say so."""

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/plain"})
        await response.prepare(request)
        await response.write(b"x" * MAX_OUTPUT_BYTES)
        await asyncio.sleep(0.2)
        await response.write(b"y")
        return response

    result = await builtin.http_request(await serve(handler))
    assert result.data["body"] == "x" * MAX_OUTPUT_BYTES + NOTICE


async def test_a_body_of_exactly_the_cap_is_whole(serve: Any) -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.Response(body=b"x" * MAX_OUTPUT_BYTES, headers={"Content-Type": "text/plain"})

    result = await builtin.http_request(await serve(handler))
    assert result.data["body"] == "x" * MAX_OUTPUT_BYTES


async def test_a_body_that_decodes_to_little_text_is_not_held_in_memory(serve: Any) -> None:
    """A body can be megabytes on the wire and almost no text: here every
    three bytes are an escape sequence that produces nothing.  Its bytes
    are let go once there are more of them than any text under the cap
    could need, and the text is still right.

    The server sends the body in pieces and waits for the client between
    them.  It runs in this process, so a body handed over whole would sit
    in the server's own send buffer and be counted here."""
    to_kanji = "\u65e5".encode("iso2022_jp")[:3]
    to_ascii = "\u65e5".encode("iso2022_jp")[-3:]
    piece = to_kanji * (64 * 1024 // 3)
    pieces = 8 * MB // len(piece)

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            headers={"Content-Type": "text/plain; charset=iso2022_jp"},
        )
        await response.prepare(request)
        for _ in range(pieces):
            await response.write(piece)
        await response.write(to_ascii + b"end")
        return response

    url = await serve(handler)
    result, peak = await _measured(lambda: builtin.http_request(url))
    assert result.data["body"] == "end"
    assert peak < MEMORY_BUDGET, f"the agent allocated {peak / MB:.1f} MB"
