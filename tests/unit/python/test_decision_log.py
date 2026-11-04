"""The decision log and the checkpoint span (RFC 0028 §D; PR plan PR 3, E-10).

Each decision recorded at a decision checkpoint goes to two sinks while
recording is on: one JSON line in the agent's decision log, which replay
(PR 8) reads, and one span, which is sampled and only for watching live.
With recording off, neither sink runs and no metric moves. A sink that fails
is counted and logged, and the turn goes on.
"""

from __future__ import annotations

import json
import logging
import os
import threading
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
    # PR 1's record has no field for either text, so this loop guards those
    # types end to end; the protection level and hashes below test the log.
    for value in (MESSAGE, *ARGUMENTS.values()):
        assert value.encode() not in raw
    line = json.loads(raw)
    assert line["context"]["protection_level"] == "internal"
    assert line["candidates"][0]["content_hash"] == hash_content(MESSAGE)
    assert line["candidates"][1]["argument_names"] == ["query"]


def _ids(path: Path) -> list[str]:
    return [json.loads(line)["record_id"]
            for line in path.read_text(encoding="utf-8").splitlines()]


def test_rotation_at_the_cap_keeps_the_newest_records_in_order(tmp_path: Path) -> None:
    # Two lines and half of a third: lines differ by a few bytes of time.
    size = len(_record().to_json()) + 1
    recorder = _recorder(tmp_path, max_bytes=2 * size + size // 2, keep=3)
    records = [_record() for _ in range(9)]
    for record in records:
        recorder.record(record)
    ids = [r.record_id for r in records]
    log = tmp_path / "ember-owl.jsonl"
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "ember-owl.jsonl", "ember-owl.jsonl.1", "ember-owl.jsonl.2", "ember-owl.jsonl.3",
    ]
    # The newest in the file being written, then older ones in .1, .2 and
    # .3; the oldest two are dropped.
    assert [_ids(log), *(_ids(log.with_name(f"{log.name}.{n}")) for n in (1, 2, 3))] == [
        ids[8:], ids[6:8], ids[4:6], ids[2:4],
    ]


def test_a_line_longer_than_the_cap_is_written_alone(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path, max_bytes=10)
    first, second = _record(), _record()
    recorder.record(first)
    recorder.record(second)
    log = tmp_path / "ember-owl.jsonl"
    assert (_ids(log), _ids(log.with_name(f"{log.name}.1"))) == (
        [second.record_id], [first.record_id],
    )


def test_an_empty_file_at_the_cap_is_not_rotated(tmp_path: Path) -> None:
    log = tmp_path / "ember-owl.jsonl"
    older = _record()
    log.with_name(f"{log.name}.1").write_text(older.to_json() + "\n", encoding="utf-8")
    log.touch()
    _recorder(tmp_path, max_bytes=10).record(_record())
    assert _ids(log.with_name(f"{log.name}.1")) == [older.record_id]
    assert not log.with_name(f"{log.name}.2").exists()


def test_rotated_files_above_a_lowered_keep_are_removed(tmp_path: Path) -> None:
    log = tmp_path / "ember-owl.jsonl"
    for n in (1, 2, 3, 4):
        log.with_name(f"{log.name}.{n}").write_text(_record().to_json() + "\n")
    log.write_text(_record().to_json() + "\n")
    _recorder(tmp_path, max_bytes=10, keep=1).record(_record())
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ember-owl.jsonl", "ember-owl.jsonl.1"]


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


def test_a_new_log_folder_is_its_owners_only(tmp_path: Path) -> None:
    _recorder(tmp_path / "data" / "decisions").record(_record())
    assert (tmp_path / "data" / "decisions").stat().st_mode & 0o777 == 0o700


def test_an_existing_log_file_open_to_others_is_made_owner_only(tmp_path: Path) -> None:
    log = tmp_path / "ember-owl.jsonl"
    log.touch()
    log.chmod(0o666)
    _recorder(tmp_path).record(_record())
    assert log.stat().st_mode & 0o777 == 0o600


async def test_a_link_at_the_log_path_is_not_followed(tmp_path: Path, reader: Any) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("kept\n")
    (tmp_path / "ember-owl.jsonl").symlink_to(victim)
    _recorder(tmp_path).record(_record())
    assert victim.read_text() == "kept\n"
    assert _points(reader, "agent.decision.sink_errors") == [
        ({"agent.id": "ember-owl", "sink": "log"}, 1),
    ]


def test_a_short_write_is_carried_on_to_the_end_of_the_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_write = os.write
    calls = 0

    def short_once(fd: int, data: Any) -> int:
        nonlocal calls
        calls += 1
        return real_write(fd, data[: len(data) // 2] if calls == 2 else data)

    recorder = _recorder(tmp_path)
    records = [_record() for _ in range(3)]
    monkeypatch.setattr(decision_log.os, "write", short_once)
    for record in records:
        recorder.record(record)
    monkeypatch.undo()
    assert _ids(tmp_path / "ember-owl.jsonl") == [r.record_id for r in records]


async def test_a_write_that_fails_part_way_leaves_only_whole_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: Any,
) -> None:
    real_write = os.write
    calls = 0

    def full_disk(fd: int, data: Any) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            return real_write(fd, data[: len(data) // 2])
        if calls == 3:
            raise OSError(28, "No space left on device")
        return real_write(fd, data)

    recorder = _recorder(tmp_path)
    first, lost, last = _record(), _record(), _record()
    monkeypatch.setattr(decision_log.os, "write", full_disk)
    for record in (first, lost, last):
        recorder.record(record)
    monkeypatch.undo()
    assert _ids(tmp_path / "ember-owl.jsonl") == [first.record_id, last.record_id]
    assert _points(reader, "agent.decision.sink_errors") == [
        ({"agent.id": "ember-owl", "sink": "log"}, 1),
    ]


def test_two_logs_on_one_file_share_its_lock(tmp_path: Path) -> None:
    # Two recorders for one agent (PR 5 records from two places) rotate the
    # same files; every line must survive.
    size = len(_record().to_json()) + 1
    path = tmp_path / "ember-owl.jsonl"
    logs = [DecisionLog(path, max_bytes=size, keep=100) for _ in range(2)]
    errors: list[BaseException] = []

    def write(log: DecisionLog) -> None:
        for _ in range(12):
            try:
                log.write(_record())
            except BaseException as exc:  # noqa: BLE001 - collected for the assert
                errors.append(exc)

    threads = [threading.Thread(target=write, args=(logs[i % 2],)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sum(len(_ids(p)) for p in tmp_path.iterdir()) == 96


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
    {"keep": -1}, {"keep": 1.5}, {"keep": 101}, {"dir": ""}, {"size": 10},
    {True: 5, "size": 3}, {1: 2, "a": 3},
])
def test_a_bad_log_block_is_refused(log: dict[Any, Any]) -> None:
    with pytest.raises(ValueError):
        DecisionLogConfig.from_agent_config({"decision": {"log": log}})


def test_an_agent_id_that_is_not_a_file_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        DecisionRecorder.for_agent("../ember-owl", None, recording=True)


def test_an_agent_id_too_long_for_its_rotated_file_names_is_refused(tmp_path: Path) -> None:
    config = {"decision": {"log": {"dir": str(tmp_path)}}}
    assert DecisionRecorder.for_agent("a" * 200, config, recording=True).recording
    with pytest.raises(ValueError):
        DecisionRecorder.for_agent("a" * 201, config, recording=True)


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
    assert _points(reader, "agent.decision.sink_errors") == []

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
