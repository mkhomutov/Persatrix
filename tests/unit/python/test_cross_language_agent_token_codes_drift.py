"""Cross-language drift pin for the codes of a refused agent token.

The orchestrator refuses a REST call whose ``X-Agent-Token`` is not good with
a 401 whose ``code`` says why (``internal/server/agent_token_check.go``,
RFC 0009 Phases 3–4 plan PR 8). The agents read that code and nothing else
to tell a refused token from any other 401 (``agents/orchestrator_session.py``,
PR 7): a code spelled differently on one side is read as some other refusal,
so the agent never replaces its token and every later call is refused too.
The Go side is pinned as text, the sibling drift files' posture, so this runs
wherever the Python unit suite runs.
"""

from __future__ import annotations

from pathlib import Path

from agents.orchestrator_session import (
    AGENT_TOKEN_HEADER,
    TOKEN_EXPIRED,
    TOKEN_INVALID,
    TOKEN_REVOKED,
)

from .test_cross_language_interaction_wire_drift import _go_const

_TOKEN_CHECK_GO = Path("internal/server/agent_token_check.go")


def test_refusal_codes_agree_with_go() -> None:
    assert _go_const(_TOKEN_CHECK_GO, "AgentTokenInvalidCode") == TOKEN_INVALID
    assert _go_const(_TOKEN_CHECK_GO, "AgentTokenExpiredCode") == TOKEN_EXPIRED
    assert _go_const(_TOKEN_CHECK_GO, "AgentTokenRevokedCode") == TOKEN_REVOKED


def test_token_header_agrees_with_go() -> None:
    """HTTP header names ignore case, so the agents' lower-case spelling is the
    orchestrator's."""
    assert _go_const(_TOKEN_CHECK_GO, "AgentTokenHeader").lower() == AGENT_TOKEN_HEADER
