"""RFC 0031 Phase 3 PR 5 — operator session-surface acceptance gate.

The end-to-end regression pin for the Phase 3 operator CLI (RFC §E): the
``persatrix session`` verb lifecycle driven against a **live** orchestrator
over the real REST surface, with the real ``persatrix`` binary, asserting both
halves of the surface Phase 3 adds — the ``sessions`` registry in
``channels.db`` (PR 1 REST + PR 2 verbs) and the CLI-local
``~/.persatrix/active-session`` pointer file (PR 3 ``use`` / ``current`` /
``new --activate``).

Opt-in via ``-m requires_orchestrator`` (registered in
[`agents/pyproject.toml`](../../agents/pyproject.toml)); the default ``pytest``
run skips this module so unit runs need no built binaries. Each case spawns a
fresh orchestrator bound to ephemeral loopback ports with a per-test
``--channels-db`` and a per-test ``PERSATRIX_ACTIVE_SESSION_FILE``, so neither
the registry nor the pointer leaks between cases or touches the operator's real
``~/.persatrix/``.

Running locally::

    make build-orchestrator build-cli
    pytest -m requires_orchestrator tests/integration/test_session_operator_surface.py

Scope — what this gate covers and what is pinned elsewhere
----------------------------------------------------------

This harness boots the **orchestrator** binary alone (the same shape as
``test_logs_e2e.py``); it does not stand up the persona society. So it pins the
four operator-surface properties that live entirely in the orchestrator + CLI +
pointer file:

* the registry lifecycle (``new`` → ``list`` → ``archive``) over real REST;
* the pointer-file lifecycle (``use`` writes it, ``current`` reads + enriches
  it, ``new --activate`` writes it, ``new`` alone does not);
* the OQ #2a reserved-``legacy`` guard, end-to-end (client fail-fast *and*
  server-authoritative rejection);
* that ``archive`` preserves the row (RFC 0031 §B): an archived session is
  still resolvable via ``GET {id}`` and ``current`` renders the archived
  marker, while ``use`` refuses to re-activate it.

The ``--session`` *override-beats-auto-binding* and *recall-isolation* legs
require a running persona with memory.db on the dispatch path, which this
orchestrator-only harness does not provide. Those are pinned where they live —
the override emission in ``internal/server/channel_session_handler_test.go`` +
``internal/channels/grpc_dispatcher_test.go`` (PR 4), and the concurrent
recall-isolation + ``legacy`` carve-out in
``tests/integration/test_session_emission_isolation.py`` (ISSUE-0082 PR 3) at
the gRPC servicer layer. Re-standing-up the full society here would be heavier
scaffolding than those pins justify — the same reasoning
``test_session_emission_isolation.py`` records for declining to start the Go
binary.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

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

# Generous on Windows where process spawn is slow.
SERVER_READY_TIMEOUT_S = 15.0
SERVER_READY_POLL_S = 0.1


# ─── Helpers ────────────────────────────────────────────────────────────────


def _free_port() -> int:
    """Reserve and release a TCP port to avoid binding collisions."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_http(host: str, port: int, timeout: float) -> None:
    """Poll the orchestrator until ``GET /healthz`` returns 200.

    A bare TCP-connect probe is insufficient: the orchestrator binds the
    listener early during startup but only mounts handlers after subsequent
    init steps, so a CLI call fired in that window hangs until the read
    timeout. Polling ``/healthz`` waits for the handlers to be live.
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
    *, http_port: int, grpc_port: int, channels_db: Path, log_dir: Path
) -> subprocess.Popen[bytes]:
    if not SERVER_BIN.exists():
        pytest.skip(
            f"orchestrator binary not found at {SERVER_BIN} — run `make build-orchestrator` first"
        )
    env = os.environ.copy()
    args = [
        str(SERVER_BIN),
        f"--http-port={http_port}",
        f"--port={grpc_port}",
        "--http-bind=127.0.0.1",
        "--grpc-bind=127.0.0.1",
        f"--config={REPO_ROOT / 'config'}",
        f"--workflows-dir={REPO_ROOT / 'workflows'}",
        # Per-test channels.db so the `sessions` registry starts empty and never
        # cross-pollinates between cases. config/channels.yaml is present, so the
        # SQLite-backed store (and thus the session registry) is wired live.
        f"--channels-db={channels_db}",
    ]
    # Files, not PIPE: the orchestrator floods structured zap logs at startup and
    # a full ~64 KiB OS pipe buffer would block the writer before the HTTP bind.
    stdout_path = log_dir / "orchestrator.stdout.log"
    stderr_path = log_dir / "orchestrator.stderr.log"
    stdout_f = stdout_path.open("ab")
    stderr_f = stderr_path.open("ab")
    proc = subprocess.Popen(  # noqa: S603 — args list, no shell.
        args, cwd=str(REPO_ROOT), env=env, stdout=stdout_f, stderr=stderr_f
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


def _run_cli(
    args: list[str], *, server: str, pointer_file: Path, timeout: float = 10.0
) -> subprocess.CompletedProcess[str]:
    """Invoke the real CLI with the active-session pointer redirected.

    ``PERSATRIX_ACTIVE_SESSION_FILE`` redirects the pointer to a per-test path so
    ``use`` / ``current`` / ``new --activate`` never touch the real
    ``~/.persatrix/``. ``NO_COLOR`` strips ANSI so stdout assertions match plain
    text regardless of the host's TTY/colour detection.
    """
    if not CLI_BIN.exists():
        pytest.skip(f"CLI binary not found at {CLI_BIN} — run `make build-cli` first")
    env = os.environ.copy()
    env["PERSATRIX_ACTIVE_SESSION_FILE"] = str(pointer_file)
    env["NO_COLOR"] = "1"
    full = [str(CLI_BIN), "--server", server, *args]
    return subprocess.run(  # noqa: S603 — args list, no shell.
        full, cwd=str(REPO_ROOT), capture_output=True, text=True,
        timeout=timeout, env=env, check=False,
    )


# ─── Fixture ────────────────────────────────────────────────────────────────


@pytest.fixture
def world(tmp_path: Path) -> Iterator[dict[str, object]]:
    http_port = _free_port()
    grpc_port = _free_port()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    channels_db = tmp_path / "channels.db"
    pointer_file = tmp_path / "active-session"
    proc = _spawn_orchestrator(
        http_port=http_port, grpc_port=grpc_port, channels_db=channels_db, log_dir=log_dir
    )
    try:
        yield {
            "server_url": f"http://127.0.0.1:{http_port}",
            "pointer_file": pointer_file,
        }
    finally:
        _stop(proc)


def _cli(world: dict[str, object], *args: str, **kw: object) -> subprocess.CompletedProcess[str]:
    return _run_cli(
        list(args),
        server=str(world["server_url"]),
        pointer_file=world["pointer_file"],  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


def _ok(result: subprocess.CompletedProcess[str]) -> subprocess.CompletedProcess[str]:
    assert result.returncode == 0, f"exit={result.returncode} stderr={result.stderr!r}"
    return result


# ─── Tests ──────────────────────────────────────────────────────────────────


def test_session_lifecycle_new_use_current_list_archive(world: dict[str, object]) -> None:
    """The full operator lifecycle round-trips against a live orchestrator.

    ``new --label arc`` registers the row, ``use`` activates it (pointer +
    registry resolution), ``current`` reads the pointer back enriched with the
    label, ``list`` surfaces it, ``archive`` flips the flag without deleting the
    row — the four properties the Phase 3 operator surface adds.
    """
    label = f"arc-{uuid.uuid4().hex[:8]}"
    pointer_file = world["pointer_file"]
    assert isinstance(pointer_file, Path)

    # new --json → the registered row's id is the registry's source of truth.
    created = json.loads(_ok(_cli(world, "session", "new", "--label", label, "--json")).stdout)
    sid = created["id"]
    assert created["label"] == label
    assert created["archived"] is False
    # `new` alone is registry-only: the pointer file must not exist yet.
    assert not pointer_file.exists(), "session new (no --activate) must not write the pointer"

    # use → resolves the label to its id and writes the pointer.
    use_out = _ok(_cli(world, "session", "use", label)).stdout
    assert sid in use_out and "Active session is now" in use_out
    assert pointer_file.read_text(encoding="utf-8").strip() == sid

    # current → reads the pointer and enriches it with the registry label.
    cur_out = _ok(_cli(world, "session", "current")).stdout
    assert sid in cur_out and label in cur_out and "Active session:" in cur_out

    # list → the session surfaces in the active set.
    rows = json.loads(_ok(_cli(world, "session", "list", "--json")).stdout)
    assert any(r["id"] == sid and r["label"] == label for r in rows)

    # archive → one-way; the row is flipped, not deleted (RFC 0031 §B).
    _ok(_cli(world, "session", "archive", label))
    active = json.loads(_ok(_cli(world, "session", "list", "--json")).stdout)
    assert all(r["id"] != sid for r in active), "archived session must drop out of the default list"
    with_archived = json.loads(
        _ok(_cli(world, "session", "list", "--include-archived", "--json")).stdout
    )
    row = next(r for r in with_archived if r["id"] == sid)
    assert row["archived"] is True


def test_new_activate_writes_pointer(world: dict[str, object]) -> None:
    """``new --activate`` is sugar for ``new`` + ``use``: it writes the pointer."""
    pointer_file = world["pointer_file"]
    assert isinstance(pointer_file, Path)
    label = f"arc-{uuid.uuid4().hex[:8]}"

    out = _ok(_cli(world, "session", "new", "--label", label, "--activate", "--json")).stdout
    sid = json.loads(out)["id"]
    assert pointer_file.read_text(encoding="utf-8").strip() == sid


def test_current_with_no_pointer_reports_legacy(world: dict[str, object]) -> None:
    """With no pointer, ``current`` names the ``legacy`` carve-out fallback."""
    pointer_file = world["pointer_file"]
    assert isinstance(pointer_file, Path)
    assert not pointer_file.exists()

    out = _ok(_cli(world, "session", "current")).stdout
    assert "No active session" in out and "legacy" in out


def test_reserved_legacy_label_rejected_end_to_end(world: dict[str, object]) -> None:
    """OQ #2a: ``new --label legacy`` is refused (client fail-fast / server guard).

    Either the client validator or the server's authoritative reserved-id guard
    must reject it; a ``legacy``-labelled row would silently merge an operator
    session into the always-visible §D carve-out. The row must not be created.
    """
    result = _cli(world, "session", "new", "--label", "legacy")
    assert result.returncode != 0
    assert "legacy" in (result.stderr + result.stdout).lower()

    # And nothing leaked into the registry.
    rows = json.loads(_ok(_cli(world, "session", "list", "--include-archived", "--json")).stdout)
    assert all(r["label"] != "legacy" for r in rows)


def test_use_refuses_archived_session(world: dict[str, object]) -> None:
    """``use`` validates against the registry and refuses an archived target.

    The stale-pointer footgun (RFC §Security: misconfiguration risk): activating
    an archived session would silently misroute the next channel. The pointer
    must stay unwritten when the target is archived.
    """
    pointer_file = world["pointer_file"]
    assert isinstance(pointer_file, Path)
    label = f"arc-{uuid.uuid4().hex[:8]}"
    sid = json.loads(_ok(_cli(world, "session", "new", "--label", label, "--json")).stdout)["id"]
    _ok(_cli(world, "session", "archive", label))

    result = _cli(world, "session", "use", label)
    assert result.returncode != 0
    assert "archived" in (result.stderr + result.stdout).lower()
    assert not pointer_file.exists(), "use must not write the pointer for an archived target"

    # The archived row is still resolvable; `current` (when pointed at it via the
    # registry) renders the archived marker — proving archive preserves the row
    # (RFC 0031 §B) rather than deleting it.
    pointer_file.write_text(sid, encoding="utf-8")
    cur = _ok(_cli(world, "session", "current")).stdout
    assert sid in cur and "archived" in cur.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "requires_orchestrator"])
