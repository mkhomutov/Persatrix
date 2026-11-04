"""The decision log and the checkpoint span (RFC 0028 §D; PR plan PR 3, E-10).

Each decision a checkpoint takes goes to two sinks while recording is on: one
JSON line in the agent's decision log, which replay (PR 8) reads, and one
span, which is sampled and only for watching live. With recording off,
neither sink runs and no metric moves. A sink that fails is counted and
logged, and the turn goes on.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from _otel_test_helpers import build_meter
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agents.decision import (
    ActionClass,
    Checkpoint,
    DecisionCandidate,
    DecisionContext,
    DecisionOutcome,
    DecisionRecord,
    PolicyMode,
    hash_arguments,
    hash_content,
)
from agents.observability import decision_log
from agents.observability.decision_log import (
    DecisionLog,
    DecisionLogConfig,
    DecisionRecorder,
)
from agents.observability.spans import DECISION_CHECKPOINT_SPAN

from ._redact_parity_samples import SAMPLES

MESSAGE = "the launch moves to Thursday"
ARGUMENTS = {"query": "salary bands for the design team"}
#: A Stripe key, split as the samples module writes it, and Go's mask for it.
_, SECRET, SECRET_MASKED = next(s for s in SAMPLES if s[0] == "stripe-key")


def _record(*, target: str = "general", confidence: float | None = None) -> DecisionRecord:
    mode = PolicyMode.MANUAL if confidence is None else PolicyMode.SEMI_AUTOMATED
    context = DecisionContext(
        checkpoint=Checkpoint.PRE_ACT, agent_id="ember-owl", mode=mode,
        channel_id="general", protection_level="internal",
    )
    candidates = (
        DecisionCandidate("reply", ActionClass.PUBLISH_CHANNEL, "send_channel_message",
                          target=target, content_hash=hash_content(MESSAGE)),
        DecisionCandidate("search", ActionClass.TOOL_CALL, "use_tool", tool_name="web_search",
                          argument_names=tuple(ARGUMENTS), content_hash=hash_arguments(ARGUMENTS)),
    )
    outcome = DecisionOutcome(ActionClass.PUBLISH_CHANNEL, selected_id="reply",
                              reason="runtime_proposal", confidence=confidence)
    return DecisionRecord(context, candidates, outcome)


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    yield exporter
    processor.shutdown()


@pytest.fixture
async def reader() -> AsyncIterator[Any]:
    reader, metrics_mod = build_meter()
    try:
        yield reader
    finally:
        await metrics_mod.shutdown()


def _points(reader: Any, name: str) -> list[tuple[dict[str, Any], int]]:
    data = reader.get_metrics_data()
    found: list[tuple[dict[str, Any], int]] = []
    for resource in data.resource_metrics if data else ():
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name:
                    found += [(dict(p.attributes), p.value) for p in metric.data.data_points]
    return found


def _decision_spans(exporter: InMemorySpanExporter) -> list[Any]:
    return [s for s in exporter.get_finished_spans() if s.name == DECISION_CHECKPOINT_SPAN]


def _recorder(tmp_path: Path, **log: Any) -> DecisionRecorder:
    config = {"decision": {"log": {"dir": str(tmp_path), **log}}}
    return DecisionRecorder.for_agent("ember-owl", config, recording=True)


# ─── The log line ──────────────────────────────────────────


def test_one_line_per_record_in_one_file_per_agent(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    records = [_record(), _record(target="random")]
    for record in records:
        recorder.record(record)
    lines = (tmp_path / "ember-owl.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines == [r.to_json() for r in records]
    assert [DecisionRecord.from_json(line) for line in lines] == records


def test_a_secret_in_a_candidate_is_masked_by_the_secret_redactor(tmp_path: Path) -> None:
    _recorder(tmp_path).record(_record(target=SECRET))
    text = (tmp_path / "ember-owl.jsonl").read_text(encoding="utf-8")
    assert SECRET not in text
    assert json.loads(text)["candidates"][0]["target"] == SECRET_MASKED


def test_no_message_or_argument_value_reaches_the_file(tmp_path: Path) -> None:
    _recorder(tmp_path).record(_record())
    raw = (tmp_path / "ember-owl.jsonl").read_bytes()
    for value in (MESSAGE, *ARGUMENTS.values()):
        assert value.encode() not in raw
    line = json.loads(raw)
    assert line["context"]["protection_level"] == "internal"
    assert line["candidates"][0]["content_hash"] == hash_content(MESSAGE)
    assert line["candidates"][1]["argument_names"] == ["query"]


def test_rotation_at_the_cap_keeps_the_configured_number_of_files(tmp_path: Path) -> None:
    size = len(_record().to_json()) + 1
    recorder = _recorder(tmp_path, max_bytes=2 * size, keep=2)
    for _ in range(9):
        recorder.record(_record())
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["ember-owl.jsonl", "ember-owl.jsonl.1", "ember-owl.jsonl.2"]
    for path in tmp_path.iterdir():
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) <= 2
        for line in lines:
            DecisionRecord.from_json(line)


def test_keep_zero_starts_the_file_again_at_the_cap(tmp_path: Path) -> None:
    size = len(_record().to_json()) + 1
    recorder = _recorder(tmp_path, max_bytes=size, keep=0)
    for _ in range(3):
        recorder.record(_record())
    assert [p.name for p in tmp_path.iterdir()] == ["ember-owl.jsonl"]
    assert len((tmp_path / "ember-owl.jsonl").read_text().splitlines()) == 1


def test_the_log_is_readable_by_its_owner_only(tmp_path: Path) -> None:
    _recorder(tmp_path).record(_record())
    assert (tmp_path / "ember-owl.jsonl").stat().st_mode & 0o777 == 0o600


# ─── Configuration ─────────────────────────────────────────


def test_the_defaults_with_no_log_block() -> None:
    assert DecisionLogConfig.from_agent_config(None) == DecisionLogConfig()
    assert DecisionLogConfig.from_agent_config({"decision": {"mode": "manual"}}) == (
        DecisionLogConfig()
    )
    config = DecisionLogConfig()
    assert (config.dir, config.max_bytes, config.keep) == (
        "data/decisions", 10 * 1024 * 1024, 5,
    )


@pytest.mark.parametrize("log", [
    {"max_bytes": 0}, {"max_bytes": -1}, {"max_bytes": True}, {"max_bytes": "10MB"},
    {"keep": -1}, {"keep": 1.5}, {"dir": ""}, {"size": 10},
])
def test_a_bad_log_block_is_refused(log: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        DecisionLogConfig.from_agent_config({"decision": {"log": log}})


def test_an_agent_id_that_is_not_a_file_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        DecisionRecorder.for_agent("../ember-owl", None, recording=True)


# ─── Recording off ─────────────────────────────────────────


async def test_with_recording_off_nothing_is_written_and_no_span_or_metric(
    tmp_path: Path, spans: InMemorySpanExporter, reader: Any,
) -> None:
    config = {"decision": {"log": {"dir": str(tmp_path / "decisions")}}}
    recorder = DecisionRecorder.for_agent("ember-owl", config, recording=False)
    assert not recorder.recording
    recorder.record(_record())
    assert not (tmp_path / "decisions").exists()
    assert _decision_spans(spans) == []
    assert _points(reader, "agent.decision.records") == []

    # The control: the same record with recording on moves all three.
    _recorder(tmp_path).record(_record())
    assert (tmp_path / "ember-owl.jsonl").exists()
    assert len(_decision_spans(spans)) == 1
    assert _points(reader, "agent.decision.records") == [(
        {"agent.id": "ember-owl", "checkpoint": "pre_act", "action_class": "publish_channel"}, 1,
    )]


# ─── The span ──────────────────────────────────────────────


def test_the_span_carries_class_count_and_confidence(
    tmp_path: Path, spans: InMemorySpanExporter,
) -> None:
    record = _record(confidence=0.75)
    _recorder(tmp_path).record(record)
    (span,) = _decision_spans(spans)
    assert dict(span.attributes) == {
        "agent.id": "ember-owl",
        "decision.checkpoint": "pre_act",
        "decision.action_class": "publish_channel",
        "decision.candidate_count": 2,
        "decision.confidence": 0.75,
        "decision.record_id": record.record_id,
    }


def test_a_manual_record_gives_its_span_no_confidence(
    tmp_path: Path, spans: InMemorySpanExporter,
) -> None:
    _recorder(tmp_path).record(_record())
    (span,) = _decision_spans(spans)
    assert "decision.confidence" not in span.attributes


# ─── A failing sink ────────────────────────────────────────


async def test_a_log_that_cannot_be_written_is_counted_and_the_span_still_runs(
    tmp_path: Path, spans: InMemorySpanExporter, reader: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (tmp_path / "ember-owl.jsonl").mkdir()
    recorder = _recorder(tmp_path)
    with caplog.at_level(logging.WARNING, logger=decision_log.__name__):
        recorder.record(_record())  # does not raise
    assert len(_decision_spans(spans)) == 1
    assert _points(reader, "agent.decision.sink_errors") == [
        ({"agent.id": "ember-owl", "sink": "log"}, 1),
    ]
    assert "IsADirectoryError" in caplog.text


async def test_a_failing_span_or_redactor_never_reaches_the_turn(
    tmp_path: Path, reader: Any, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    probe = "PROBE-text-from-the-turn"

    class Broken:
        def redact(self, record: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError(probe)

    def no_span(*args: object, **kwargs: object) -> object:
        raise RuntimeError(probe)

    monkeypatch.setattr(decision_log._tracer, "start_span", no_span)
    log = DecisionLog(tmp_path / "ember-owl.jsonl", redactor=Broken())
    with caplog.at_level(logging.WARNING, logger=decision_log.__name__):
        DecisionRecorder("ember-owl", log).record(_record())
    assert sorted(a["sink"] for a, _ in _points(reader, "agent.decision.sink_errors")) == [
        "log", "span",
    ]
    assert not (tmp_path / "ember-owl.jsonl").exists()
    assert probe not in caplog.text
    assert "RuntimeError" in caplog.text
