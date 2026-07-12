"""Shared mock builders for the Gemini provider tests.

Extracted so ``test_llm_gemini.py`` (core translation logic) and
``test_llm_gemini_edge.py`` (thinking-budget lever, prompt-block / truncation
edge cases) share one set of ``google-genai`` doubles without either file
crossing the 500-line review cap (the ``redactor_google_test.go`` split
precedent). The ``_`` prefix keeps pytest from collecting this module as tests.

No network is touched: the SDK is mocked via ``sys.modules`` exactly the way
:mod:`tests.unit.python.test_llm_client` mocks ``anthropic`` / ``openai``, so
these run whether or not the optional ``google-genai`` extra is installed.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agents.llm_gemini import GeminiProvider


def _mock_genai_modules() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Return stand-in ``google`` / ``google.genai`` modules + the client double.

    ``from google import genai`` resolves ``genai`` as an attribute of the
    ``google`` package, so the fake ``google`` module carries a ``genai``
    attribute pointing at the fake ``genai`` module whose ``Client`` returns
    the client double.
    """
    genai_mod = MagicMock()
    client = MagicMock()
    genai_mod.Client.return_value = client
    google_mod = MagicMock()
    google_mod.genai = genai_mod
    return google_mod, genai_mod, client


def _make_gemini_provider(
    provider_config: dict | None = None,
) -> GeminiProvider:
    """Create a GeminiProvider with a mocked SDK client adopted as ``_client``."""
    google_mod, _genai_mod, client = _mock_genai_modules()
    with patch.dict(
        sys.modules, {"google": google_mod, "google.genai": _genai_mod}
    ):
        p = GeminiProvider(api_key="test-key", provider_config=provider_config)
    p._client = client
    return p


def _gemini_part(
    *,
    text: str | None = None,
    function_call: SimpleNamespace | None = None,
    thought_signature: bytes | None = None,
) -> SimpleNamespace:
    # ``thought_signature`` is a Part-level field (sibling of ``function_call``)
    # the Gemini 3.x API emits on the tool-call part and requires echoed back;
    # left ``None`` it mirrors 2.x / non-thinking parts.
    return SimpleNamespace(
        text=text, function_call=function_call, thought_signature=thought_signature
    )


def _gemini_response(
    parts: list | None = None,
    finish_reason: str | None = "STOP",
    prompt_tokens: int = 100,
    output_tokens: int = 50,
) -> SimpleNamespace:
    """Build a mock ``google-genai`` generate_content response."""
    if parts is None:
        parts = [_gemini_part(text="Hello from Gemini")]
    content = SimpleNamespace(role="model", parts=parts)
    fr = SimpleNamespace(name=finish_reason) if finish_reason is not None else None
    candidate = SimpleNamespace(content=content, finish_reason=fr)
    usage = SimpleNamespace(
        prompt_token_count=prompt_tokens, candidates_token_count=output_tokens
    )
    return SimpleNamespace(candidates=[candidate], usage_metadata=usage)
