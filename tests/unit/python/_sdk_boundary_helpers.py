"""Shared pieces of the SDK boundary tests.

``test_llm_sdk_boundary.py`` covers the two SDKs every install has (Anthropic,
OpenAI); ``test_llm_sdk_boundary_gemini.py`` and
``test_llm_sdk_boundary_watsonx.py`` cover the two that come with the optional
``providers`` extra. All run the real SDK and replace only the socket, so they
share the pieces kept here. The ``_`` prefix keeps pytest from collecting
this module.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
from typing import Any

import pytest

# Set to "1" by `make test-providers`, the run that installs the optional
# SDKs: there a missing SDK is a failure. Everywhere else it is a skip.
REQUIRE_PROVIDER_SDKS_ENV = "PERSATRIX_REQUIRE_PROVIDER_SDKS"

# One tool in the protocol's own shape; each provider maps it to its SDK's.
TOOLS = [
    {
        "name": "lookup",
        "description": "Look something up.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
]


class Recorder:
    """Answers one canned body and keeps the requests the SDK actually built.

    ``hx`` is the httpx module the SDK itself uses (``httpx`` or ``httpx2``),
    read off the SDK's own client so the harness never guesses.
    """

    def __init__(self, body: dict[str, Any], hx: Any):
        self._body = body
        self._hx = hx
        self.requests: list[Any] = []

    def transport(self) -> Any:
        def handle(request: Any) -> Any:
            self.requests.append(request)
            return self._hx.Response(200, json=self._body)

        return self._hx.MockTransport(handle)

    @property
    def request(self) -> Any:
        """The last request the SDK issued."""
        if not self.requests:
            raise AssertionError("the SDK never issued a request")
        return self.requests[-1]

    @property
    def sent(self) -> dict[str, Any]:
        """The JSON body of the last request."""
        body: dict[str, Any] = json.loads(self.request.content)
        return body


def provider_sdk(module_name: str) -> Any:
    """Import an optional provider SDK, or skip the calling test.

    A test that skips everywhere proves nothing, so the run that installs the
    SDKs sets ``PERSATRIX_REQUIRE_PROVIDER_SDKS=1`` and a missing SDK fails
    there instead.
    """
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        if os.environ.get(REQUIRE_PROVIDER_SDKS_ENV) == "1":
            pytest.fail(
                f"{module_name} is not installed, and {REQUIRE_PROVIDER_SDKS_ENV}=1 says "
                f"this run must have it (make build-agents-providers): {exc}"
            )
        pytest.skip(f"{module_name} is not installed (the optional `providers` extra)")


def refuse_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any attempt to open a connection fail the test instead of reaching out."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an SDK boundary test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
