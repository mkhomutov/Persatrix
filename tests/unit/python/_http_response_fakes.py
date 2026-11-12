"""A body for a faked ``aiohttp`` response, as ``http_request`` reads one.

``http_request`` reads a response body in pieces from ``resp.content``
and stops at its output cap (ISSUE-0215); it no longer calls
``resp.text()``.  A test that fakes the response with a mock gives it a
body through :func:`give_body`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

__all__ = ["give_body"]


class _Body:
    """The part of ``aiohttp.StreamReader`` that ``http_request`` uses."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            n = len(self._raw)
        chunk, self._raw = self._raw[:n], self._raw[n:]
        return chunk


def give_body(mock_resp: Any, body: str | bytes, *, encoding: str = "utf-8") -> None:
    """Make *mock_resp* serve *body*, in *encoding* when it is given as text."""
    raw = body.encode(encoding) if isinstance(body, str) else body
    mock_resp.content = _Body(raw)
    mock_resp.get_encoding = MagicMock(return_value=encoding)
