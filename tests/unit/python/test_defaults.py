"""
Tests for agents/defaults.py — execution limit constants (RFC 0006 §B).

Verifies that all exported constants are positive and match the values
specified in the RFC. The Go-side counterparts live in
internal/defaults/defaults.go; both files should stay conceptually aligned.
"""

from agents.defaults import (
    DEFAULT_MAX_LLM_CALLS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TIMEOUT_SECONDS,
)


class TestDefaults:
    def test_default_max_llm_calls_positive(self):
        assert DEFAULT_MAX_LLM_CALLS > 0

    def test_default_max_tokens_positive(self):
        assert DEFAULT_MAX_TOKENS > 0

    def test_default_timeout_seconds_positive(self):
        assert DEFAULT_TIMEOUT_SECONDS > 0

    def test_default_max_llm_calls_value(self):
        # RFC 0006 §B: lowered from 10 to 5.
        assert DEFAULT_MAX_LLM_CALLS == 5

    def test_default_max_tokens_value(self):
        # RFC 0006 §B: raised from 4096 to 8192.
        assert DEFAULT_MAX_TOKENS == 8192

    def test_default_timeout_seconds_value(self):
        # RFC 0006 §B: matches Go DefaultTimeoutSeconds (60s).
        assert DEFAULT_TIMEOUT_SECONDS == 60
