"""
Tests for :func:`agents.persona_runtime.session_id.resolve_session_id_and_log`.

The helper mirrors the Go-side ``cmd/orchestrator/startup.go::resolveSessionID``
log contract:

* Empty / unset env → INFO log + return ``"legacy"``.
* Value outside ``[A-Za-z0-9_-]`` → WARN log + return the value verbatim.
* Well-formed non-empty value → silent + return the value verbatim.

PR 4 (RFC 0031 follow-ups) closes the Python-vs-Go log parity gap recorded
as finding #3 in [docs/rfcs/0031-pr-plan.md](../../../docs/rfcs/0031-pr-plan.md).
"""

from __future__ import annotations

import logging

import pytest

from agents.persona_runtime.session_id import (
    LEGACY_SESSION_ID,
    SESSION_ID_ENV_VAR,
    resolve_session_id_and_log,
)


@pytest.fixture
def session_logger() -> logging.Logger:
    return logging.getLogger("test.session_id_resolve")


class TestUnsetEnv:
    def test_unset_returns_legacy(self, monkeypatch, session_logger):
        monkeypatch.delenv(SESSION_ID_ENV_VAR, raising=False)
        assert resolve_session_id_and_log(session_logger) == LEGACY_SESSION_ID

    def test_unset_emits_info_log(self, monkeypatch, session_logger, caplog):
        monkeypatch.delenv(SESSION_ID_ENV_VAR, raising=False)
        with caplog.at_level(logging.INFO, logger=session_logger.name):
            resolve_session_id_and_log(session_logger)
        msgs = [r.getMessage() for r in caplog.records]
        assert any(
            SESSION_ID_ENV_VAR in m and LEGACY_SESSION_ID in m for m in msgs
        ), f"expected INFO log mentioning env var + legacy; got: {msgs!r}"

    def test_blank_string_emits_info_log(self, monkeypatch, session_logger, caplog):
        monkeypatch.setenv(SESSION_ID_ENV_VAR, "   ")
        with caplog.at_level(logging.INFO, logger=session_logger.name):
            result = resolve_session_id_and_log(session_logger)
        assert result == LEGACY_SESSION_ID
        msgs = [r.getMessage() for r in caplog.records]
        assert any(SESSION_ID_ENV_VAR in m for m in msgs)


class TestCanonicalValue:
    def test_simple_value_returns_verbatim(self, monkeypatch, session_logger):
        monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-a")
        assert resolve_session_id_and_log(session_logger) == "run-a"

    def test_canonical_chars_are_silent(self, monkeypatch, session_logger, caplog):
        # All chars from [A-Za-z0-9_-] must produce zero log output —
        # parity with the Go side.
        monkeypatch.setenv(SESSION_ID_ENV_VAR, "Run-1_b")
        with caplog.at_level(logging.DEBUG, logger=session_logger.name):
            resolve_session_id_and_log(session_logger)
        assert caplog.records == []


class TestNonCanonicalValue:
    """RFC 0031 PR 4 finding #3 — Python WARN parity with the Go side."""

    def test_space_emits_warn_but_accepts(self, monkeypatch, session_logger, caplog):
        # The canonical example from MT-SESSION-001 Edge Case 1.  The
        # value must still be returned verbatim — Phase 1 plumbing
        # accepts any non-empty value; only the warning is new.
        monkeypatch.setenv(SESSION_ID_ENV_VAR, "my session")
        with caplog.at_level(logging.WARNING, logger=session_logger.name):
            result = resolve_session_id_and_log(session_logger)
        assert result == "my session"
        warn_msgs = [
            r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING
        ]
        assert any(SESSION_ID_ENV_VAR in m for m in warn_msgs), (
            f"expected WARN mentioning env var; got: {warn_msgs!r}"
        )
        # Mirror the Go-side message string so operators grep for one
        # phrase across both binaries (RFC 0031 PR 4 finding #3).
        assert any(
            "[A-Za-z0-9_-]" in m for m in warn_msgs
        ), f"WARN should cite the canonical regex; got: {warn_msgs!r}"

    def test_punctuation_emits_warn(self, monkeypatch, session_logger, caplog):
        monkeypatch.setenv(SESSION_ID_ENV_VAR, "run.a")
        with caplog.at_level(logging.WARNING, logger=session_logger.name):
            result = resolve_session_id_and_log(session_logger)
        assert result == "run.a"
        warn = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warn, "non-canonical char must emit WARN"

    def test_unicode_emits_warn(self, monkeypatch, session_logger, caplog):
        monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-α")
        with caplog.at_level(logging.WARNING, logger=session_logger.name):
            result = resolve_session_id_and_log(session_logger)
        assert result == "run-α"
        warn = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warn, "non-ASCII char must emit WARN"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
