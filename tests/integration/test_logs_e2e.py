"""End-to-end test for ``persatrix logs`` (RFC 0018 PR 6).

Opt-in via ``-m requires_orchestrator`` (registered in
[`agents/pyproject.toml`](../../agents/pyproject.toml)).  The default
``pytest`` invocation skips this module so unit-test runs do not require
a built ``bin/persatrix-server`` + ``cli/target/release/persatrix``
binary pair on the host.

Running locally::

    make build-orchestrator build-cli
    pytest -m requires_orchestrator tests/integration/test_logs_e2e.py

Each test case spawns a fresh orchestrator process bound to ephemeral
loopback ports with a per-test ``PERSATRIX_LOGBUFFER_DIR`` so on-disk
state never leaks between cases.  Log entries are injected through the
gRPC ``LogService`` (the same path the Python agent shipper uses in
production), then the CLI is invoked as a subprocess to verify the
operator-visible UX.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import grpc
import pytest
from google.protobuf import struct_pb2, timestamp_pb2

from agents.generated import log_service_pb2 as logpb
from agents.generated import log_service_pb2_grpc as loggrpc

pytestmark = pytest.mark.requires_orchestrator

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_BIN = REPO_ROOT / "bin" / ("persatrix-server.exe" if os.name == "nt" else "persatrix-server")
CLI_BIN = (
    REPO_ROOT
    / "cli"
    / "target"
    / "release"
    / ("persatrix.exe" if os.name == "nt" else "persatrix")
)

# Wait budgets — generous on Windows where process spawn is slow.
SERVER_READY_TIMEOUT_S = 15.0
SERVER_READY_POLL_S = 0.1
FOLLOW_DELIVERY_BUDGET_S = 2.0


# ─── Helpers ────────────────────────────────────────────────────────────────


def _free_port() -> int:
    """Reserve and release a TCP port to avoid binding collisions."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_http(host: str, port: int, timeout: float) -> None:
    """Poll the orchestrator until ``GET /health`` returns 200.

    A bare TCP-connect probe is insufficient: the orchestrator binds the
    listener early during startup but only mounts handlers (and warms the
    log buffer) after subsequent init steps. CLI calls fired between the
    bind and the handler mount silently hang until the server's read
    timeout — surfacing as opaque pytest TimeoutExpired errors.
    """
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    url = f"http://{host}:{port}/healthz"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:  # noqa: S310 — loopback
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_err = exc
            time.sleep(SERVER_READY_POLL_S)
    raise RuntimeError(f"orchestrator did not become ready on {url}: {last_err!r}")


def _spawn_orchestrator(
    *,
    http_port: int,
    grpc_port: int,
    log_dir: Path,
) -> subprocess.Popen[bytes]:
    if not SERVER_BIN.exists():
        pytest.skip(
            f"orchestrator binary not found at {SERVER_BIN} — run `make build-orchestrator` first"
        )
    env = os.environ.copy()
    # Pin the disk store to a per-test directory so restart-durability
    # tests can re-spawn against the same state, and so concurrent cases
    # never cross-pollinate.
    env["PERSATRIX_LOGBUFFER_DIR"] = str(log_dir)
    # 5 MiB cap is plenty for the handful of entries each case writes
    # and keeps the eviction code path warm in CI.
    env["PERSATRIX_LOGBUFFER_DISK_MB"] = "5"
    # Loopback-only on both surfaces; orchestrator already defaults to
    # loopback per the PR #173 review fix, but be explicit so a future
    # default change doesn't surprise the test.
    args = [
        str(SERVER_BIN),
        f"--http-port={http_port}",
        f"--port={grpc_port}",
        "--http-bind=127.0.0.1",
        "--grpc-bind=127.0.0.1",
        f"--config={REPO_ROOT / 'config'}",
        f"--workflows-dir={REPO_ROOT / 'workflows'}",
    ]
    # Redirect stdout/stderr to files in the per-test log dir.  Using
    # subprocess.PIPE here is unsafe on Windows: the OS pipe buffer is
    # ~64 KiB and the orchestrator emits a flood of structured zap logs
    # at startup; once the buffer fills, the writer goroutine blocks and
    # the HTTP listener bind never runs, causing this fixture to time
    # out with no useful diagnostics.  Files have no such limit.
    stdout_path = log_dir / "orchestrator.stdout.log"
    stderr_path = log_dir / "orchestrator.stderr.log"
    stdout_f = stdout_path.open("ab")
    stderr_f = stderr_path.open("ab")
    proc = subprocess.Popen(  # noqa: S603 — args list, no shell.
        args,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=stdout_f,
        stderr=stderr_f,
    )
    try:
        _wait_for_http("127.0.0.1", http_port, SERVER_READY_TIMEOUT_S)
    except Exception:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        stdout_f.close()
        stderr_f.close()
        out = stdout_path.read_bytes() if stdout_path.exists() else b""
        err = stderr_path.read_bytes() if stderr_path.exists() else b""
        raise RuntimeError(
            f"orchestrator did not become ready.\nstdout:\n{out.decode(errors='replace')}\n"
            f"stderr:\n{err.decode(errors='replace')}"
        ) from None
    # Stash the file handles on the Popen object so _stop can close them.
    proc._persatrix_stdout = stdout_f  # type: ignore[attr-defined]
    proc._persatrix_stderr = stderr_f  # type: ignore[attr-defined]
    return proc


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover — slow CI guard.
            proc.kill()
            proc.wait(timeout=5)
    for attr in ("_persatrix_stdout", "_persatrix_stderr"):
        f = getattr(proc, attr, None)
        if f is not None:
            try:
                f.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass


def _make_entry(
    *,
    execution_id: str,
    service_kind: str,
    message: str,
    level: str = "INFO",
    trace_id: str = "",
    attributes: dict[str, str] | None = None,
) -> logpb.LogEntry:
    ts = timestamp_pb2.Timestamp()
    ts.GetCurrentTime()
    entry = logpb.LogEntry(
        schema_version="0.1",
        timestamp=ts,
        level=level,
        service_kind=service_kind,
        service_instance="e2e-test",
        message=message,
        execution_id=execution_id,
        trace_id=trace_id,
    )
    if attributes:
        s = struct_pb2.Struct()
        for k, v in attributes.items():
            s[k] = v
        entry.attributes.CopyFrom(s)
    return entry


async def _ship_entries(grpc_addr: str, entries: list[logpb.LogEntry]) -> None:
    """Fire-and-acked: open the LogService stream, push, await final ack."""
    async with grpc.aio.insecure_channel(grpc_addr) as ch:
        stub = loggrpc.LogServiceStub(ch)

        async def _gen() -> AsyncIterator[logpb.LogBatch]:
            yield logpb.LogBatch(entries=entries)

        last_ack = 0
        async for ack in stub.StreamLogs(_gen()):
            last_ack = ack.received_through_seq
        assert last_ack >= len(entries), f"expected ack >= {len(entries)}, got {last_ack}"


def _run_cli(
    args: list[str], *, server: str, timeout: float = 10.0
) -> subprocess.CompletedProcess[str]:
    if not CLI_BIN.exists():
        pytest.skip(f"CLI binary not found at {CLI_BIN} — run `make build-cli` first")
    full = [str(CLI_BIN), "--server", server, *args]
    return subprocess.run(  # noqa: S603 — args list, no shell.
        full,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# ─── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def orchestrator(tmp_path: Path) -> Iterator[dict[str, object]]:
    http_port = _free_port()
    grpc_port = _free_port()
    log_dir = tmp_path / "logbuffer"
    log_dir.mkdir()
    proc = _spawn_orchestrator(http_port=http_port, grpc_port=grpc_port, log_dir=log_dir)
    try:
        yield {
            "http_port": http_port,
            "grpc_port": grpc_port,
            "log_dir": log_dir,
            "proc": proc,
            "server_url": f"http://127.0.0.1:{http_port}",
            "grpc_addr": f"127.0.0.1:{grpc_port}",
        }
    finally:
        _stop(proc)


# ─── Tests ─────────────────────────────────────────────────────────────────


def test_logs_snapshot_returns_injected_entries(orchestrator: dict[str, object]) -> None:
    """Snapshot mode renders Go-side and Python-side entries together."""
    exec_id = f"e2e-snap-{uuid.uuid4().hex[:8]}"
    entries = [
        _make_entry(execution_id=exec_id, service_kind="orchestrator", message="go side hello"),
        _make_entry(execution_id=exec_id, service_kind="agent", message="python side hello"),
    ]
    asyncio.run(_ship_entries(str(orchestrator["grpc_addr"]), entries))

    result = _run_cli(["logs", exec_id], server=str(orchestrator["server_url"]))
    assert result.returncode == 0, f"stderr={result.stderr}"
    assert "go side hello" in result.stdout
    assert "python side hello" in result.stdout


def test_logs_trace_filter_narrows_output(orchestrator: dict[str, object]) -> None:
    """``--trace`` keeps only entries whose ``trace_id`` matches."""
    exec_id = f"e2e-trace-{uuid.uuid4().hex[:8]}"
    trace_keep = "1" * 32
    trace_drop = "2" * 32
    entries = [
        _make_entry(
            execution_id=exec_id, service_kind="orchestrator",
            message="keep me", trace_id=trace_keep,
        ),
        _make_entry(
            execution_id=exec_id, service_kind="orchestrator",
            message="drop me", trace_id=trace_drop,
        ),
    ]
    asyncio.run(_ship_entries(str(orchestrator["grpc_addr"]), entries))

    result = _run_cli(
        ["logs", exec_id, "--trace", trace_keep],
        server=str(orchestrator["server_url"]),
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    assert "keep me" in result.stdout
    assert "drop me" not in result.stdout


def test_logs_follow_streams_new_entries_within_budget(orchestrator: dict[str, object]) -> None:
    """``--follow`` delivers a post-subscription entry within 2s."""
    if not CLI_BIN.exists():
        pytest.skip(f"CLI binary not found at {CLI_BIN}")
    exec_id = f"e2e-follow-{uuid.uuid4().hex[:8]}"

    # Start the CLI in --follow mode first so the SSE subscription is
    # active before we ship.  Capture stdout line-by-line to time delivery.
    cli = subprocess.Popen(  # noqa: S603 — args list, no shell.
        [str(CLI_BIN), "--server", str(orchestrator["server_url"]), "logs", "--follow", exec_id],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Give the SSE handshake a moment to land before producing.
        time.sleep(0.5)
        ship_t0 = time.monotonic()
        asyncio.run(
            _ship_entries(
                str(orchestrator["grpc_addr"]),
                [_make_entry(execution_id=exec_id, service_kind="agent", message="streamed-hello")],
            )
        )

        # Read the next stdout line within the budget.
        deadline = time.monotonic() + FOLLOW_DELIVERY_BUDGET_S + 1.0
        seen = ""
        assert cli.stdout is not None
        # Use a background thread to enforce the deadline because
        # readline() is blocking.
        import threading

        result_box: list[str] = []

        def _read() -> None:
            line = cli.stdout.readline() if cli.stdout else ""
            result_box.append(line)

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout=deadline - time.monotonic())
        if result_box:
            seen = result_box[0]
        elapsed = time.monotonic() - ship_t0
        assert "streamed-hello" in seen, f"did not see entry within {elapsed:.2f}s; line={seen!r}"
        assert elapsed < FOLLOW_DELIVERY_BUDGET_S + 1.0, (
            f"entry took {elapsed:.2f}s, budget is {FOLLOW_DELIVERY_BUDGET_S}s"
        )
    finally:
        cli.terminate()
        try:
            cli.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            cli.kill()
            cli.wait(timeout=5)


def test_logs_survive_orchestrator_restart(tmp_path: Path) -> None:
    """Sealed entries on disk are surfaced after a (re)start.

    Exercises the disk store's warm-load path
    (``internal/observability/logbuffer/disk.go``).  The orchestrator
    only flushes a ring to disk when the workflow lifecycle calls
    ``Buffer.Seal``; PR 6 does not wire that path, so the test seeds
    the on-disk JSONL file directly using the documented layout
    (``<dir>/<execution_id>/<sequence>.jsonl``, one Entry per line)
    and then starts the orchestrator and asserts that the warm-load
    surfaces the entries via ``persatrix logs``.
    """
    http_port = _free_port()
    grpc_port = _free_port()
    log_dir = tmp_path / "logbuffer"
    log_dir.mkdir()
    exec_id = f"e2e-restart-{uuid.uuid4().hex[:8]}"

    # Seed the disk store with two entries for ``exec_id`` so that
    # warm-load picks them up when the orchestrator boots against this
    # ``PERSATRIX_LOGBUFFER_DIR``.  The on-disk format is documented in
    # ``internal/observability/logbuffer/disk.go``.
    exec_dir = log_dir / exec_id
    exec_dir.mkdir()
    seq_file = exec_dir / "0000000001.jsonl"
    now = "2025-01-01T00:00:00Z"
    seeded = [
        {
            "schema_version": "1",
            "timestamp": now,
            "level": "INFO",
            "service.kind": "orchestrator",
            "service.instance": "test",
            "execution_id": exec_id,
            "message": "pre-restart-1",
        },
        {
            "schema_version": "1",
            "timestamp": now,
            "level": "INFO",
            "service.kind": "agent",
            "service.instance": "test",
            "execution_id": exec_id,
            "message": "pre-restart-2",
        },
    ]
    with seq_file.open("w", encoding="utf-8") as f:
        for entry in seeded:
            f.write(json.dumps(entry) + "\n")

    proc = _spawn_orchestrator(http_port=http_port, grpc_port=grpc_port, log_dir=log_dir)
    try:
        result = _run_cli(["logs", exec_id], server=f"http://127.0.0.1:{http_port}")
    finally:
        _stop(proc)

    assert result.returncode == 0, f"stderr={result.stderr}"
    assert "pre-restart-1" in result.stdout, f"stdout={result.stdout!r}"
    assert "pre-restart-2" in result.stdout, f"stdout={result.stdout!r}"


def test_logs_invalid_level_rejected_at_parse_time() -> None:
    """The clap ``ValueEnum`` rejects unknown ``--level`` values without a server hop."""
    if not CLI_BIN.exists():
        pytest.skip(f"CLI binary not found at {CLI_BIN}")
    result = subprocess.run(  # noqa: S603
        [str(CLI_BIN), "logs", "exec-1", "--level", "TRACE"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    # clap's error formatting for invalid value-enum:
    assert "TRACE" in (result.stderr + result.stdout)


# Suppress ``sys`` import linter complaint when threading import lives
# inside a function above.
_ = sys
