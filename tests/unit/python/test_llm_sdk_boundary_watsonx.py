"""Contract tests for the seam between the watsonx adapter and its SDK.

The watsonx half of what ``test_llm_sdk_boundary_gemini.py`` describes:
``ibm-watsonx-ai`` comes with the optional ``providers`` extra, so these
tests skip where it is not installed and must run under ``make
test-providers``, which sets ``PERSATRIX_REQUIRE_PROVIDER_SDKS=1``
(ISSUE-0220). The SDK is the real one and sends as it does in the product,
through its own httpx client. Only that client's last step, the send, is
replaced, and a guard fails any test that looks up a name or opens a
connection.

What is covered and what is not: the SDK cannot build its client without
three calls of its own (an IAM token, the project or space, the model list).
They are answered here with the least the SDK reads. So the adapter's
``Credentials`` and ``ModelInference`` keywords, the ``chat`` keywords and
the reply it parses are covered. What IBM's services really answer to those
three calls is not. Nor is what the service accepts inside a message or a
tool: the SDK checks the names of ``chat``'s keywords and sends their
contents on as given, so those are pinned here as they reach the wire.

Not covered either: an error reply or a retry. The SDK retries some
failures itself, in a worker thread the adapter cannot cancel (ISSUE-0253).
"""

from __future__ import annotations

import base64
import importlib
import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from agents.llm_client import StopReason
from agents.llm_types import LLMToolResult
from agents.llm_watsonx import WatsonxProvider

from ._sdk_boundary_helpers import TOOLS, provider_sdk, refuse_network


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    attempts = refuse_network(monkeypatch)
    yield
    assert not attempts, f"the test reached for the network, and something caught it: {attempts}"


_WATSONX_MODEL = "meta-llama/llama-4-maverick"

_WATSONX_TEXT_BODY = {
    "id": "chatcmpl-boundary-text",
    "model_id": _WATSONX_MODEL,
    "created": 1_760_000_000,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hello from the boundary"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
}

_WATSONX_TOOL_BODY = {
    "id": "chatcmpl-boundary-tool",
    "model_id": _WATSONX_MODEL,
    "created": 1_760_000_000,
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_boundary",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"query": "persatrix"}'},
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 13, "completion_tokens": 9, "total_tokens": 22},
}


def _fake_iam_token() -> str:
    """A token shaped like IBM's: the SDK reads the expiry out of its middle part."""

    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return ".".join([part({"alg": "none"}), part({"exp": 4_102_444_800, "iat": 1}), "sig"])


class _WatsonxService:
    """Answers the calls the watsonx SDK makes, and keeps each request.

    The first three are the SDK's own set-up, made when ``ModelInference`` is
    built; each answer holds the least the SDK reads. Anything else gets a
    404, so a call a newer SDK adds fails the test by name.
    """

    def __init__(self, hx: Any, chat_body: dict[str, Any]):
        self._hx = hx
        self._chat_body = chat_body
        self.requests: list[Any] = []

    def answer(self, request: Any) -> Any:
        self.requests.append(request)
        path = request.url.path
        if path == "/identity/token":
            return self._json({"access_token": _fake_iam_token(), "expires_in": 3600})
        if path.startswith("/v2/projects/"):
            return self._json({"entity": {"storage": {"type": "bmcos_object_storage"}}})
        if path.startswith("/v2/spaces/"):
            return self._json({"entity": {"name": "boundary"}})
        if path == "/ml/v1/foundation_model_specs":
            return self._json({"resources": [{"model_id": _WATSONX_MODEL}]})
        if path == "/ml/v1/text/chat":
            return self._json(self._chat_body)
        return self._hx.Response(404, json={"errors": [{"message": f"no answer for {path}"}]})

    def _json(self, body: dict[str, Any]) -> Any:
        return self._hx.Response(200, json=body)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    @property
    def chats(self) -> list[dict[str, Any]]:
        """The JSON body of every chat request, in order."""
        return [
            json.loads(request.content)
            for request in self.requests
            if request.url.path == "/ml/v1/text/chat"
        ]


@pytest.fixture
def watsonx_service(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[dict[str, Any]], _WatsonxService]:
    """Put a canned service behind every httpx client the watsonx SDK builds.

    The adapter builds ``ModelInference`` from credentials, and the SDK then
    builds its own http clients and calls out at once, so there is no client
    to swap a transport on beforehand. The transport class itself is patched
    for the test: still only the socket is replaced.
    """
    sdk_http = provider_sdk("ibm_watsonx_ai._wrappers.httpx")
    bases = [cls.__module__.split(".")[0] for cls in sdk_http.HTTPXClient.__mro__]
    flavours = [name for name in bases if name.startswith("httpx")]
    assert flavours, (
        f"expected the watsonx SDK's http client to be an httpx client, got {bases}: "
        "the SDK changed its HTTP layer and this harness needs rechecking"
    )
    hx = importlib.import_module(flavours[0])

    def serve(chat_body: dict[str, Any]) -> _WatsonxService:
        service = _WatsonxService(hx, chat_body)

        def handle(transport: Any, request: Any) -> Any:
            return service.answer(request)

        async def handle_async(transport: Any, request: Any) -> Any:
            return service.answer(request)

        monkeypatch.setattr(hx.HTTPTransport, "handle_request", handle)
        monkeypatch.setattr(hx.AsyncHTTPTransport, "handle_async_request", handle_async)
        return service

    return serve


def _watsonx_provider(**scope: str) -> WatsonxProvider:
    return WatsonxProvider(
        api_key="not-a-real-key", url="https://us-south.ml.cloud.ibm.com", **scope
    )


async def test_watsonx_request_kwargs_survive_the_real_sdk(
    watsonx_service: Callable[[dict[str, Any]], _WatsonxService],
) -> None:
    """The keywords given to Credentials, ModelInference and chat are accepted,
    and what create_message builds reaches the wire."""
    service = watsonx_service(_WATSONX_TEXT_BODY)
    provider = _watsonx_provider(project_id="proj-boundary")

    await provider.create_message(
        model=_WATSONX_MODEL,
        messages=[{"role": "user", "content": "hi"}],
        system="be brief",
        tools=provider.format_tool_definitions(TOOLS),
        max_tokens=64,
        temperature=0.4,
    )

    # Building the client: the key goes to IBM's token service, then the SDK
    # looks up the project and the model. Then the one chat call.
    assert service.paths() == [
        "/identity/token",
        "/v2/projects/proj-boundary",
        "/ml/v1/foundation_model_specs",
        "/ml/v1/text/chat",
    ]
    assert b"apikey=not-a-real-key" in service.requests[0].content
    chat_request = service.requests[-1]
    assert chat_request.url.host == "us-south.ml.cloud.ibm.com"
    assert chat_request.headers["authorization"] == f"Bearer {_fake_iam_token()}"
    (sent,) = service.chats
    assert sent["model_id"] == _WATSONX_MODEL
    assert sent["project_id"] == "proj-boundary"
    # `system` is folded into the message list, and the two `params` become
    # top-level fields of the request.
    assert sent["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]
    assert sent["max_tokens"] == 64
    assert sent["temperature"] == 0.4
    # The SDK checks the names of chat's keywords and passes their contents
    # through, so the tool's shape is pinned here as it reaches the wire.
    assert sent["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Look something up.",
                "parameters": TOOLS[0]["parameters"],
            },
        }
    ]


async def test_watsonx_normalizes_a_real_sdk_response(
    watsonx_service: Callable[[dict[str, Any]], _WatsonxService],
) -> None:
    watsonx_service(_WATSONX_TEXT_BODY)
    provider = _watsonx_provider(project_id="proj-boundary")

    result = await provider.create_message(
        model=_WATSONX_MODEL,
        messages=[{"role": "user", "content": "hi"}],
        system="",
        tools=[],
        max_tokens=64,
        temperature=0.0,
    )

    assert result.text == "hello from the boundary"
    assert result.tool_calls == []
    assert result.stop_reason is StopReason.END_TURN
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 5


async def test_watsonx_reads_a_truncated_reply_off_the_finish_reason(
    watsonx_service: Callable[[dict[str, Any]], _WatsonxService],
) -> None:
    body = json.loads(json.dumps(_WATSONX_TEXT_BODY))
    body["choices"][0]["finish_reason"] = "length"
    watsonx_service(body)
    provider = _watsonx_provider(project_id="proj-boundary")

    result = await provider.create_message(
        model=_WATSONX_MODEL,
        messages=[{"role": "user", "content": "hi"}],
        system="",
        tools=[],
        max_tokens=8,
        temperature=0.0,
    )

    assert result.stop_reason is StopReason.MAX_TOKENS


async def test_watsonx_tool_call_round_trips_through_the_real_sdk(
    watsonx_service: Callable[[dict[str, Any]], _WatsonxService],
) -> None:
    """The tool call the SDK returns is parsed, and the round the adapter
    builds from it is accepted by the SDK and reaches the wire."""
    service = watsonx_service(_WATSONX_TOOL_BODY)
    provider = _watsonx_provider(project_id="proj-boundary")
    tools = provider.format_tool_definitions(TOOLS)
    messages: list[Any] = [{"role": "user", "content": "look it up"}]

    result = await provider.create_message(
        model=_WATSONX_MODEL,
        messages=messages,
        system="",
        tools=tools,
        max_tokens=64,
        temperature=0.0,
    )

    assert result.stop_reason is StopReason.TOOL_USE
    (call,) = result.tool_calls
    assert (call.id, call.name, call.input) == ("call_boundary", "lookup", {"query": "persatrix"})

    replayed = provider.append_tool_round(
        messages, result, [LLMToolResult(tool_call_id=call.id, content="found", is_error=False)]
    )
    await provider.create_message(
        model=_WATSONX_MODEL,
        messages=replayed,
        system="",
        tools=tools,
        max_tokens=64,
        temperature=0.0,
    )

    # One client for both calls: the set-up calls are not made again.
    assert service.paths().count("/identity/token") == 1
    _, second = service.chats
    assert second["messages"][1:] == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_boundary",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"query": "persatrix"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_boundary", "content": "found"},
    ]


async def test_watsonx_space_id_builds_a_client_for_a_space(
    watsonx_service: Callable[[dict[str, Any]], _WatsonxService],
) -> None:
    service = watsonx_service(_WATSONX_TEXT_BODY)
    provider = _watsonx_provider(space_id="space-boundary")

    await provider.create_message(
        model=_WATSONX_MODEL,
        messages=[{"role": "user", "content": "hi"}],
        system="",
        tools=[],
        max_tokens=64,
        temperature=0.0,
    )

    assert "/v2/spaces/space-boundary" in service.paths()
    (sent,) = service.chats
    assert sent["space_id"] == "space-boundary"
    assert "project_id" not in sent


# ─── The seam itself ────────────────────────────────────────


def test_the_sdk_under_test_is_the_real_one() -> None:
    """Guard against these tests quietly becoming mock tests.

    The other tests of this adapter put a ``MagicMock`` in ``sys.modules``
    while they build a provider. If one ever leaked, every assertion above
    would pass against the mock.
    """
    import importlib.metadata as metadata

    module = provider_sdk("ibm_watsonx_ai")
    assert metadata.version("ibm-watsonx-ai"), "ibm-watsonx-ai is not installed"
    assert type(module).__name__ == "module", (
        f"ibm_watsonx_ai resolved to {type(module).__name__}, not a real module: "
        "these tests would be asserting against a mock"
    )
