"""Shared mock builders for the watsonx.ai provider tests.

Extracted so ``test_llm_watsonx.py`` (translation logic) and the
``create_provider`` watsonx-branch tests in ``test_llm_factory.py`` share one
set of ``ibm-watsonx-ai`` doubles (the ``_gemini_test_helpers`` precedent). The
``_`` prefix keeps pytest from collecting this module as tests.

No network is touched: the SDK is mocked via ``sys.modules`` exactly the way
:mod:`tests.unit.python.test_llm_client` mocks ``anthropic`` / ``openai`` and
``_gemini_test_helpers`` mocks ``google-genai``, so these run whether or not the
optional ``ibm-watsonx-ai`` extra is installed.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock, patch

from agents.llm_watsonx import WatsonxProvider


def _mock_watsonx_modules() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Return stand-in ``ibm_watsonx_ai`` / ``…foundation_models`` modules + the
    ``ModelInference`` double.

    ``WatsonxProvider.__init__`` does ``from ibm_watsonx_ai import Credentials``
    and ``from ibm_watsonx_ai.foundation_models import ModelInference``. The
    latter is a submodule import, so both names are placed in ``sys.modules``;
    ``fm_mod.ModelInference`` returns a single cached ``model_inference`` double
    (so ``_get_model`` for any model id resolves to it).
    """
    fm_mod = MagicMock()
    model_inference = MagicMock()
    fm_mod.ModelInference.return_value = model_inference
    ibm_mod = MagicMock()
    ibm_mod.foundation_models = fm_mod
    return ibm_mod, fm_mod, model_inference


def _make_watsonx_provider(
    *, project_id: str | None = "proj-1", space_id: str | None = None
) -> tuple[WatsonxProvider, MagicMock]:
    """Create a WatsonxProvider with a mocked SDK; return it + the ModelInference."""
    ibm_mod, fm_mod, model_inference = _mock_watsonx_modules()
    with patch.dict(
        sys.modules,
        {
            "ibm_watsonx_ai": ibm_mod,
            "ibm_watsonx_ai.foundation_models": fm_mod,
        },
    ):
        p = WatsonxProvider(
            api_key="test-key",
            url="https://us-south.ml.cloud.ibm.com",
            project_id=project_id,
            space_id=space_id,
        )
    return p, model_inference


def _watsonx_tool_call(
    *, call_id: str = "call_1", name: str = "file_read", arguments: Any = '{"path": "main.py"}'
) -> dict:
    """One OpenAI-shaped tool call as watsonx's chat API returns it (arguments is
    a JSON *string*)."""
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _watsonx_response(
    *,
    content: str | None = "Hello from watsonx",
    tool_calls: list | None = None,
    finish_reason: str | None = "stop",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    include_usage: bool = True,
) -> dict:
    """Build a mock ``ModelInference.chat`` response (OpenAI-compatible dict)."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    response: dict[str, Any] = {
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if include_usage:
        response["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    return response
