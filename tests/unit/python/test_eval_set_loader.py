"""Unit tests for the RFC 0044 Phase 1 eval-set loader + ``evaluate``.

Covers: the eval-set recipe round-trips through ``load_eval_set`` into typed
dataclasses; the JSON schema (``schemas/eval_set.json``) rejects malformed
recipes at load time; the RFC 0044 §D structural rule (``match: exact`` is
never used for stochastic ``assistant:`` / transcript content); and the
``evaluate`` engine reports per-assertion pass/fail over an ``EvalRun``,
including the RFC-mandated self-test where flipping one observed value flips
exactly one assertion to failing.
"""

import textwrap
from pathlib import Path

import pytest

from evaluators.assertions import EvalRun, MatchOp
from evaluators.eval_set import EvalSet, evaluate, load_eval_set

# A complete, valid recipe used across the happy-path tests. It mirrors the
# RFC 0044 §A dementia-test shape but is trimmed to two interactions.
_VALID_RECIPE = textwrap.dedent(
    """
    id: EVAL-MEMORY-001
    title: Dementia test — recall across interactions
    description: |
      The persona must reference the named entity and stated affiliation
      when a later, keyword-disjoint trigger appears.
    spawned_from: ../manual-tests/MT-MEMORY-005-dementia-test.md
    tier: stable
    setup:
      persona: ember-owl
      user: alice
      channel: dm:alice-ember
      session_id: EVAL-MEMORY-001-S
      seed_state:
        persona:ember-owl:trust.scores.alice: 0.0
      llm_mode: replay
    interactions:
      - id: i1
        turns:
          - user: "Hi Ember — I'm Alice, I work on the data-platform team."
          - assistant: {match: contains, value: "Alice"}
            events:
              - {type: ModelOutput}
      - id: i2
        elapsed: 5m
        turns:
          - user: "What do you remember about me?"
          - assistant: {match: must_reference, values: ["Alice", "data-platform"]}
    assertions:
      terminal_state:
        persona:ember-owl:trust.scores.alice: {match: gt, value: 0.0}
      event_count:
        Error: 0
      final_transcript:
        must_reference: ["Alice", "data-platform"]
        must_not_reference: ["[error]", "I don't recall"]
    """
).strip()


def _write(tmp_path: Path, body: str, name: str = "eval.yaml") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def _minimal(assertions_block: str, eval_id: str = "EVAL-WORKING-001") -> str:
    """A one-interaction recipe whose sole assistant turn expects `contains: ok`,
    parameterized by an `assertions:` block — used by the evaluate-coverage tests.
    ``assertions_block`` is written at column 0 and indented two spaces under the
    `assertions:` key here."""
    header = textwrap.dedent(
        f"""
        id: {eval_id}
        title: coverage recipe
        setup:
          persona: ember-owl
        interactions:
          - id: i1
            turns:
              - user: "go"
              - assistant: {{match: contains, value: "ok"}}
        assertions:
        """
    ).strip("\n")
    block = textwrap.indent(textwrap.dedent(assertions_block).strip("\n"), "  ")
    return f"{header}\n{block}\n"


# ─── Happy-path parse ───────────────────────────────────────────────────────


def test_load_valid_recipe_populates_model(tmp_path: Path) -> None:
    es = load_eval_set(_write(tmp_path, _VALID_RECIPE))
    assert isinstance(es, EvalSet)
    assert es.id == "EVAL-MEMORY-001"
    assert es.tier == "stable"
    assert es.setup.persona == "ember-owl"
    assert es.setup.llm_mode == "replay"
    assert es.setup.seed_state["persona:ember-owl:trust.scores.alice"] == 0.0
    assert len(es.interactions) == 2
    assert es.interactions[1].elapsed == "5m"
    # Two user turns + two assistant turns across the two interactions.
    roles = [t.role for i in es.interactions for t in i.turns]
    assert roles == ["user", "assistant", "user", "assistant"]
    # The first assistant turn carries a content expectation + one event assertion.
    first_assistant = es.interactions[0].turns[1]
    assert first_assistant.expect is not None
    assert first_assistant.expect.op is MatchOp.CONTAINS
    assert first_assistant.events[0].type == "ModelOutput"


def test_tier_defaults_to_experimental_when_omitted(tmp_path: Path) -> None:
    body = _VALID_RECIPE.replace("tier: stable\n", "")
    es = load_eval_set(_write(tmp_path, body))
    assert es.tier == "experimental"


# ─── Schema rejection ───────────────────────────────────────────────────────


def test_missing_required_id_rejected(tmp_path: Path) -> None:
    body = "\n".join(
        line for line in _VALID_RECIPE.splitlines() if not line.startswith("id:")
    )
    with pytest.raises(ValueError):
        load_eval_set(_write(tmp_path, body))


def test_malformed_id_pattern_rejected(tmp_path: Path) -> None:
    body = _VALID_RECIPE.replace("EVAL-MEMORY-001", "memory-test-1")
    with pytest.raises(ValueError):
        load_eval_set(_write(tmp_path, body))


def test_unknown_llm_mode_rejected(tmp_path: Path) -> None:
    body = _VALID_RECIPE.replace("llm_mode: replay", "llm_mode: telepathy")
    with pytest.raises(ValueError):
        load_eval_set(_write(tmp_path, body))


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    body = _VALID_RECIPE + "\nunexpected_key: 1\n"
    with pytest.raises(ValueError):
        load_eval_set(_write(tmp_path, body))


# ─── RFC 0044 §D — exact is banned on stochastic content ─────────────────────


def test_exact_on_assistant_content_rejected(tmp_path: Path) -> None:
    body = _VALID_RECIPE.replace(
        "{match: contains, value: \"Alice\"}", "{match: exact, value: \"Alice\"}"
    )
    with pytest.raises(ValueError, match="exact"):
        load_eval_set(_write(tmp_path, body))


def test_exact_on_terminal_state_is_allowed(tmp_path: Path) -> None:
    # §D permits exact for state values — only assistant/transcript content is
    # off-limits. Swapping the numeric trust gate for an exact string state
    # value must load cleanly.
    body = _VALID_RECIPE.replace(
        "persona:ember-owl:trust.scores.alice: {match: gt, value: 0.0}",
        "persona:ember-owl:status: {match: exact, value: active}",
    )
    es = load_eval_set(_write(tmp_path, body))
    assert es.assertions.terminal_state["persona:ember-owl:status"].op is MatchOp.EXACT


# ─── evaluate() — the assertion report ───────────────────────────────────────


def _passing_run() -> EvalRun:
    return EvalRun(
        turn_outputs=[
            "Nice to meet you, Alice!",
            "You're Alice from the data-platform team.",
        ],
        terminal_state={"persona:ember-owl:trust.scores.alice": 0.4},
        events=[{"type": "ModelOutput"}, {"type": "ModelOutput"}],
    )


def test_evaluate_all_pass(tmp_path: Path) -> None:
    es = load_eval_set(_write(tmp_path, _VALID_RECIPE))
    report = evaluate(es, _passing_run())
    assert report.passed is True, report.failures()
    assert report.eval_id == "EVAL-MEMORY-001"


def test_evaluate_self_test_single_flip(tmp_path: Path) -> None:
    """RFC 0044 Test Strategy: flipping one observed value fails exactly the
    assertion that depends on it, and nothing else."""
    es = load_eval_set(_write(tmp_path, _VALID_RECIPE))
    run = _passing_run()
    # Regress the trust score below the terminal_state gt gate.
    run.terminal_state["persona:ember-owl:trust.scores.alice"] = 0.0
    report = evaluate(es, run)
    assert report.passed is False
    failures = report.failures()
    assert len(failures) == 1
    assert "terminal_state" in failures[0].name


def test_evaluate_final_transcript_must_reference_failure(tmp_path: Path) -> None:
    es = load_eval_set(_write(tmp_path, _VALID_RECIPE))
    run = _passing_run()
    # Drop "data-platform" from the transcript → must_reference fails.
    run.turn_outputs[1] = "You're Alice."
    report = evaluate(es, run)
    assert report.passed is False
    assert any("final_transcript" in f.name for f in report.failures())


def test_evaluate_per_turn_assistant_expectation(tmp_path: Path) -> None:
    es = load_eval_set(_write(tmp_path, _VALID_RECIPE))
    run = _passing_run()
    # First assistant turn must `contains: Alice`; remove it.
    run.turn_outputs[0] = "Nice to meet you!"
    report = evaluate(es, run)
    assert report.passed is False
    assert any(f.name.startswith("turn[") for f in report.failures())


def test_evaluate_run_shorter_than_turns_reports_missing_output(tmp_path: Path) -> None:
    # _VALID_RECIPE has two assistant turns; a run with one output must trip the
    # `idx >= len(turn_outputs)` guard for the second turn.
    es = load_eval_set(_write(tmp_path, _VALID_RECIPE))
    run = _passing_run()
    del run.turn_outputs[1]
    report = evaluate(es, run)
    assert report.passed is False
    missing = [f for f in report.failures() if f.name.startswith("turn[1]")]
    assert missing and "no assistant output" in missing[0].detail


# ─── New load-time validations (review hardening) ────────────────────────────


def test_contains_without_value_rejected(tmp_path: Path) -> None:
    body = _VALID_RECIPE.replace('{match: contains, value: "Alice"}', "{match: contains}")
    with pytest.raises(ValueError, match="value"):
        load_eval_set(_write(tmp_path, body))


def test_must_reference_without_values_rejected(tmp_path: Path) -> None:
    body = _VALID_RECIPE.replace(
        '{match: must_reference, values: ["Alice", "data-platform"]}', "{match: must_reference}"
    )
    with pytest.raises(ValueError, match="values"):
        load_eval_set(_write(tmp_path, body))


def test_miskeyed_operand_rejected(tmp_path: Path) -> None:
    # `contains` takes `value`, not `values`; a mis-keyed operand would otherwise
    # coalesce to a vacuously-passing assertion.
    body = _VALID_RECIPE.replace(
        '{match: contains, value: "Alice"}', '{match: contains, values: ["Alice"]}'
    )
    with pytest.raises(ValueError):
        load_eval_set(_write(tmp_path, body))


def test_invalid_regex_operand_rejected_at_load(tmp_path: Path) -> None:
    body = _VALID_RECIPE.replace(
        '{match: contains, value: "Alice"}', '{match: regex, value: "([unclosed"}'
    )
    with pytest.raises(ValueError, match="regex"):
        load_eval_set(_write(tmp_path, body))


# ─── evaluate() coverage for the assertion branches ─────────────────────────


def test_evaluate_event_sequence_pass_and_fail(tmp_path: Path) -> None:
    es = load_eval_set(_write(tmp_path, _minimal('event_sequence: ["ModelOutput", "ToolCall"]')))
    assert es.assertions.event_sequence == ["ModelOutput", "ToolCall"]
    good = EvalRun(turn_outputs=["ok"], events=[{"type": "ModelOutput"}, {"type": "ToolCall"}])
    assert evaluate(es, good).passed is True
    bad = evaluate(es, EvalRun(turn_outputs=["ok"], events=[{"type": "ModelOutput"}]))
    assert bad.passed is False
    assert any(f.name == "event_sequence" for f in bad.failures())


def test_evaluate_exact_terminal_state_pass_and_fail(tmp_path: Path) -> None:
    es = load_eval_set(
        _write(
            tmp_path,
            _minimal("terminal_state:\n  persona:ember-owl:status: {match: exact, value: active}"),
        )
    )
    ok_run = EvalRun(turn_outputs=["ok"], terminal_state={"persona:ember-owl:status": "active"})
    assert evaluate(es, ok_run).passed is True
    bad = EvalRun(turn_outputs=["ok"], terminal_state={"persona:ember-owl:status": "idle"})
    report = evaluate(es, bad)
    assert report.passed is False
    assert any("terminal_state" in f.name for f in report.failures())


def test_evaluate_final_transcript_scalar_contains_and_regex(tmp_path: Path) -> None:
    block = 'final_transcript:\n  contains: "data-platform"\n  regex: "team$"'
    es = load_eval_set(_write(tmp_path, _minimal(block)))
    ops = {ca.op for ca in es.assertions.final_transcript}
    assert MatchOp.CONTAINS in ops and MatchOp.REGEX in ops
    assert evaluate(es, EvalRun(turn_outputs=["ok, the data-platform team"])).passed is True
    assert evaluate(es, EvalRun(turn_outputs=["ok"])).passed is False


def test_event_extra_fields_captured(tmp_path: Path) -> None:
    body = _VALID_RECIPE.replace(
        "- {type: ModelOutput}",
        '- {type: StateDelta, scope: persona, key_pattern: "ember-owl:trust.*"}',
    )
    es = load_eval_set(_write(tmp_path, body))
    ev = es.interactions[0].turns[1].events[0]
    assert ev.type == "StateDelta"
    assert ev.fields == {"scope": "persona", "key_pattern": "ember-owl:trust.*"}
