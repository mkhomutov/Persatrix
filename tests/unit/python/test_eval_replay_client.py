"""Unit tests for the RFC 0044 Phase 1 replay LLM client (PR 2, OQ #2).

The replay client is the *mock-as-LLM* that makes golden-trace replay CI-safe
(RFC 0044 §D): a recorded-response :class:`~agents.llm_types.LLMProvider` that
returns cassette responses keyed by a **canonicalized request hash**. This suite
pins the four properties the harness relies on:

- **Canonicalization is stable + volatile-field-stripping** (OQ #2): the same
  logical request hashes identically regardless of dict key order, and volatile
  keys (prompt-cache markers, provider round-trip signatures, timestamps) do not
  perturb the hash.
- **Payload (de)serialization is loss-free**: an ``LLMResponse`` survives the
  round-trip through the YAML-safe cassette payload — text, tool calls (incl. the
  opaque ``signature`` bytes), ``stop_reason``, and ``usage``.
- **Replay is deterministic + fails loud**: a hit returns the recorded response
  byte-stably across calls; a miss raises :class:`ReplayCassetteMissError` (a drifted
  recipe must not silently pass).
- **Record → replay is symmetric**: a cassette captured by ``RecordingProvider``
  wrapping a live provider replays to identical responses — the two sides share
  one canonicalization, so recording what replay will later key on is sound.
"""

import json

import pytest

from agents.llm_types import (
    LLMProvider,
    LLMResponse,
    LLMToolResult,
    StopReason,
    ToolCall,
    Usage,
)
from evaluators.replay_llm_client import (
    DEFAULT_VOLATILE_KEYS,
    RecordingProvider,
    ReplayCassetteMissError,
    ReplayProvider,
    canonicalize_request,
    dump_cassette,
    hash_request,
    load_cassette,
    payload_to_response,
    response_to_payload,
)

# A minimal, valid create_message request. Helpers below vary one field at a time.
_BASE_REQUEST = {
    "model": "claude-x",
    "messages": [{"role": "user", "content": "Hi Ember — I'm Alice."}],
    "system": "You are Ember.",
    "tools": [],
    "max_tokens": 1024,
    "temperature": 0.0,
}


def _req(**overrides):
    return {**_BASE_REQUEST, **overrides}


# ─── Canonicalization + hashing ──────────────────────────────────────────────


def test_hash_is_stable_across_dict_key_order():
    """A request hashes identically regardless of inner dict key insertion order."""
    a = _req(messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
    b = _req(messages=[{"role": "user", "content": [{"text": "hi", "type": "text"}]}])
    assert hash_request(**a) == hash_request(**b)


def test_hash_is_hex_sha256_and_deterministic():
    h = hash_request(**_BASE_REQUEST)
    assert isinstance(h, str)
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    assert h == hash_request(**_BASE_REQUEST)  # no per-process salt


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "claude-y"),
        ("system", "You are Owl."),
        ("messages", [{"role": "user", "content": "different"}]),
        ("max_tokens", 2048),
        ("temperature", 0.7),
        ("tools", [{"name": "search"}]),
    ],
)
def test_hash_changes_when_a_semantic_field_changes(field, value):
    """Every semantic request field participates in the identity."""
    assert hash_request(**_req(**{field: value})) != hash_request(**_BASE_REQUEST)


@pytest.mark.parametrize("volatile_key", sorted(DEFAULT_VOLATILE_KEYS))
def test_default_volatile_key_does_not_perturb_hash(volatile_key):
    """A volatile key anywhere in the request is stripped before hashing."""
    noisy = _req(
        messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "hi", volatile_key: "noise"}],
            }
        ]
    )
    clean = _req(messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
    assert hash_request(**noisy) == hash_request(**clean)


def test_drop_keys_override_replaces_the_default_set():
    """A custom ``drop_keys`` replaces (not augments) the default volatile set."""
    # With a custom set naming only ``nonce``, a ``nonce`` difference is ignored…
    n1 = _req(messages=[{"role": "user", "content": "x", "nonce": "1"}])
    n2 = _req(messages=[{"role": "user", "content": "x", "nonce": "2"}])
    drop = frozenset({"nonce"})
    assert hash_request(**n1, drop_keys=drop) == hash_request(**n2, drop_keys=drop)
    # …but a default-volatile key (``cache_control``) now *does* perturb it,
    # because the override replaced the default set rather than adding to it.
    c1 = _req(messages=[{"role": "user", "content": "x", "cache_control": {"type": "ephemeral"}}])
    c2 = _req(messages=[{"role": "user", "content": "x"}])
    assert hash_request(**c1, drop_keys=drop) != hash_request(**c2, drop_keys=drop)


def test_canonicalize_request_is_compact_sorted_json():
    canon = canonicalize_request(**_BASE_REQUEST)
    # Round-trips as JSON and is the sorted/compact/UTF-8 form (idempotent re-dump).
    parsed = json.loads(canon)
    assert canon == json.dumps(
        parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


# ─── Payload (de)serialization round-trip ────────────────────────────────────


def test_text_response_round_trips():
    resp = LLMResponse(
        text="Hello Alice",
        tool_calls=[],
        stop_reason=StopReason.END_TURN,
        usage=Usage(input_tokens=12, output_tokens=5),
    )
    payload = response_to_payload(resp)
    # The payload is YAML/JSON-safe (no enums, no raw bytes).
    json.dumps(payload)
    back = payload_to_response(payload)
    assert back == resp


def test_tool_call_response_round_trips_including_signature_bytes():
    resp = LLMResponse(
        text=None,
        tool_calls=[
            ToolCall(
                id="tool_1",
                name="recall",
                input={"query": "Alice"},
                signature=b"\x00\x01\xfe\xff",
            )
        ],
        stop_reason=StopReason.TOOL_USE,
        usage=Usage(input_tokens=3, output_tokens=4),
    )
    payload = response_to_payload(resp)
    json.dumps(payload)  # signature must be base64, not raw bytes
    back = payload_to_response(payload)
    assert back == resp
    assert back.tool_calls[0].signature == b"\x00\x01\xfe\xff"
    assert back.stop_reason is StopReason.TOOL_USE


# ─── ReplayProvider: hit / miss / determinism / protocol ─────────────────────


def test_replay_provider_satisfies_llmprovider_protocol():
    provider = ReplayProvider({})
    assert isinstance(provider, LLMProvider)
    assert provider.name == "replay"


async def test_replay_hit_returns_recorded_response_byte_stably():
    resp = LLMResponse(text="Hi Alice!", stop_reason=StopReason.END_TURN, usage=Usage(1, 2))
    cassette = {hash_request(**_BASE_REQUEST): response_to_payload(resp)}
    provider = ReplayProvider(cassette)
    first = await provider.create_message(**_BASE_REQUEST)
    second = await provider.create_message(**_BASE_REQUEST)
    assert first == resp
    assert first == second  # deterministic across calls


async def test_replay_miss_raises_cassette_miss_with_actionable_detail():
    provider = ReplayProvider({})  # empty cassette
    with pytest.raises(ReplayCassetteMissError) as exc:
        await provider.create_message(**_BASE_REQUEST)
    msg = str(exc.value)
    assert hash_request(**_BASE_REQUEST)[:12] in msg  # names the missing hash
    assert "0" in msg  # reports how many responses are recorded (zero here)


# ─── Cassette file load / dump ───────────────────────────────────────────────


def test_cassette_dump_load_round_trip(tmp_path):
    resp = LLMResponse(text="recorded", stop_reason=StopReason.END_TURN, usage=Usage(2, 3))
    cassette = {hash_request(**_BASE_REQUEST): response_to_payload(resp)}
    path = tmp_path / "EVAL-X-001.golden.yaml"
    dump_cassette(cassette, path)
    assert path.exists()
    assert load_cassette(path) == cassette


async def test_replay_provider_from_file(tmp_path):
    resp = LLMResponse(text="from disk", stop_reason=StopReason.END_TURN, usage=Usage(1, 1))
    cassette = {hash_request(**_BASE_REQUEST): response_to_payload(resp)}
    path = tmp_path / "c.golden.yaml"
    dump_cassette(cassette, path)
    provider = ReplayProvider.from_file(path)
    assert (await provider.create_message(**_BASE_REQUEST)) == resp


# ─── Record → replay symmetry ────────────────────────────────────────────────


class _FakeLiveProvider:
    """A deterministic stand-in for a live provider, used to drive recording.

    Returns a scripted response per latest user message; if that message
    contains ``"tool"`` it emits a tool call (to exercise multi-round replay).
    """

    name = "fake-live"

    def __init__(self):
        self.received_tools = []  # what the live create_message was handed each call

    async def create_message(self, *, model, messages, system, tools, max_tokens, temperature):
        self.received_tools.append(tools)
        last = messages[-1]
        content = last.get("content") if isinstance(last, dict) else ""
        text = content if isinstance(content, str) else json.dumps(content)
        if "tool" in text:
            return LLMResponse(
                text=None,
                tool_calls=[ToolCall(id="t1", name="recall", input={"q": "Alice"})],
                stop_reason=StopReason.TOOL_USE,
                usage=Usage(4, 4),
            )
        return LLMResponse(
            text=f"echo:{text}",
            stop_reason=StopReason.END_TURN,
            usage=Usage(len(text), 3),
        )

    def format_tool_definitions(self, tools):
        return list(tools)

    def append_tool_round(self, messages, response, tool_results):
        return [*messages, {"role": "assistant", "content": "x"}]


async def test_record_then_replay_reproduces_responses():
    fake = _FakeLiveProvider()
    recorder = RecordingProvider(fake)
    assert recorder.name == fake.name  # traces stay attributed during record

    requests = [
        _req(messages=[{"role": "user", "content": "Hi, I'm Alice."}]),
        _req(messages=[{"role": "user", "content": "What do you remember?"}]),
        _req(messages=[{"role": "user", "content": "different topic"}]),
    ]
    recorded = [await recorder.create_message(**r) for r in requests]
    assert len(recorder.cassette) == 3  # one entry per distinct request

    replay = ReplayProvider(recorder.cassette)
    for req, original in zip(requests, recorded):
        assert (await replay.create_message(**req)) == original


async def test_record_then_replay_multi_round_tool_loop():
    """A tool-use round-trip: the second request (built by ``append_tool_round``)
    must canonicalize identically on record and replay so both rounds hit."""
    fake = _FakeLiveProvider()
    recorder = RecordingProvider(fake)

    r1 = _req(messages=[{"role": "user", "content": "please use a tool"}])
    resp1 = await recorder.create_message(**r1)
    assert resp1.stop_reason is StopReason.TOOL_USE
    msgs2 = recorder.append_tool_round(
        r1["messages"], resp1, [LLMToolResult("t1", "Alice works on data-platform", False)]
    )
    r2 = _req(messages=msgs2)
    resp2 = await recorder.create_message(**r2)

    replay = ReplayProvider(recorder.cassette)
    rep1 = await replay.create_message(**r1)
    assert rep1 == resp1
    rep_msgs2 = replay.append_tool_round(
        r1["messages"], rep1, [LLMToolResult("t1", "Alice works on data-platform", False)]
    )
    assert (await replay.create_message(**_req(messages=rep_msgs2))) == resp2


class _ToolFormattingFake(_FakeLiveProvider):
    """A live-provider stand-in whose ``format_tool_definitions`` STRUCTURALLY
    rewrites the defs (``parameters`` → ``input_schema``), exactly like the real
    Anthropic provider. This is what exposes a record/replay tool-hash asymmetry."""

    name = "tool-formatting-fake"

    def format_tool_definitions(self, tools):
        return [{"name": t["name"], "input_schema": t["parameters"]} for t in tools]


async def test_record_then_replay_hits_for_tool_bearing_request():
    """Regression: a tools-bearing recipe must replay to a HIT.

    The runtime pipes ``format_tool_definitions()`` output into
    ``create_message(tools=…)``, and ``tools`` is hashed. If RecordingProvider
    keyed on the vendor-formatted tool shape while ReplayProvider keys on the raw
    shape, every tool-bearing golden would raise ReplayCassetteMissError. Both
    sides must key on the same (raw) shape, and the live call must still receive
    the vendor-native shape."""
    raw_tools = [
        {"name": "recall", "description": "recall a fact", "parameters": {"type": "object"}}
    ]
    fake = _ToolFormattingFake()
    recorder = RecordingProvider(fake)

    # Mirror the runtime: format, then create_message(tools=<formatter output>).
    rec_tools = recorder.format_tool_definitions(raw_tools)
    recorded = await recorder.create_message(
        **_req(tools=rec_tools, messages=[{"role": "user", "content": "hi"}])
    )
    # The LIVE provider still saw the vendor-native (input_schema) shape…
    assert fake.received_tools[-1] == [{"name": "recall", "input_schema": {"type": "object"}}]

    # …while replay (no wrapped provider) formats identically and HITS.
    replay = ReplayProvider(recorder.cassette)
    rep_tools = replay.format_tool_definitions(raw_tools)
    assert (
        await replay.create_message(
            **_req(tools=rep_tools, messages=[{"role": "user", "content": "hi"}])
        )
    ) == recorded


# ─── Cassette load edge cases ────────────────────────────────────────────────


def test_load_cassette_empty_file_returns_empty(tmp_path):
    path = tmp_path / "empty.golden.yaml"
    path.write_text("", encoding="utf-8")
    assert load_cassette(path) == {}


def test_load_cassette_rejects_non_mapping(tmp_path):
    path = tmp_path / "bad.golden.yaml"
    path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_cassette(path)
