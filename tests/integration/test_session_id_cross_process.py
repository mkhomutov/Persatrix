"""RFC 0031 Phase 1 cross-process write contract.

Two persona-runtime *processes* started under different
``PERSATRIX_SESSION_ID`` values must produce storage rows each tagged
with their own session id when they share a SQLite memory file.  This
test asserts the write contract only — Phase 1 ships no recall-side
filtering (Phase 2 lands that, gated by RFC 0031 OQ #1's resolution 1a).

Why a cross-process test
------------------------

The single-process variant in
``tests/unit/python/test_session_id_persona_runtime.py`` proves the
constructor reads the env var and stamps it on calls in the same
interpreter.  This file's pin is different: it proves a fresh Python
process started under ``PERSATRIX_SESSION_ID=...`` (i.e. the production
shape — operator exports the env, then runs the persona binary) picks
up the value and writes it to disk.  Combined with the Go-side
cross-process pin from PR 2
(``cmd/orchestrator/session_env_test.go``) this closes the env-var →
storage contract on both halves of the Phase 1 wire.

The orchestrator binary is intentionally not started here — its
env-var-read path is owned by PR 2's Go tests, and joining the two
binaries in a single pytest fixture is heavier scaffolding than this
contract pin justifies.  The shared-binary path is covered by the
manual ``MT-SESSION-001`` walkthrough (executed at v0.3.1 release prep).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


# Worker that opens an in-memory persona stack pointed at a shared
# SQLite file, writes one episode + one interaction, and exits.  Runs
# as a child process so the ``PERSATRIX_SESSION_ID`` env var is read
# from a fresh interpreter — that's the production shape we are
# pinning, not an in-process ``monkeypatch.setenv`` round-trip.
_WORKER_SCRIPT = """
import asyncio
import json
import os
import sys

from agents.memory.episodic import EpisodicMemory
from agents.memory.relationship import RelationshipMemory


async def main() -> None:
    db_path = sys.argv[1]
    agent_id = sys.argv[2]
    peer_id = sys.argv[3]
    summary = sys.argv[4]
    # PERSATRIX_SESSION_ID is set by the parent test via the subprocess
    # ``env=`` kwarg; the persona-runtime is the consumer in production,
    # but we exercise the same env-read path by calling the kwarg-aware
    # store APIs directly with the resolved id.  The unit test in
    # ``test_session_id_persona_runtime.py`` covers the runtime
    # constructor's env read; here we cover the cross-process boundary.
    session_id = os.environ.get("PERSATRIX_SESSION_ID", "").strip() or "legacy"

    ep = EpisodicMemory(agent_id=agent_id, db_path=db_path)
    rel = RelationshipMemory(agent_id=agent_id, db_path=db_path)
    await ep.initialize()
    await rel.initialize()
    try:
        await ep.store_episode(summary, {"src": "test"}, session_id=session_id)
        await rel.record_interaction(
            peer_id, "chat", session_id=session_id,
        )
    finally:
        await ep.close()
        await rel.close()

    # Print the resolved session_id so the parent can assert the
    # child read the env, not just that the row landed.
    print(json.dumps({"session_id": session_id}))


asyncio.run(main())
"""


def _run_worker(
    *,
    db_path: Path,
    agent_id: str,
    peer_id: str,
    summary: str,
    session_id: str | None,
) -> str:
    """Run the worker subprocess; return the JSON-decoded session_id it saw."""
    env = os.environ.copy()
    # PR-219-style ``PYTHONPATH`` thread so the subprocess can import
    # ``agents.*`` from the repo root.  Conftest adds the workspace to
    # ``sys.path`` for the in-process suite; subprocesses inherit
    # neither that hook nor pytest's path setup.
    repo_root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = (
        f"{repo_root}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    )
    if session_id is None:
        env.pop("PERSATRIX_SESSION_ID", None)
    else:
        env["PERSATRIX_SESSION_ID"] = session_id

    result = subprocess.run(
        [
            sys.executable, "-c", _WORKER_SCRIPT,
            str(db_path), agent_id, peer_id, summary,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f"worker failed: rc={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    return payload["session_id"]


@pytest.fixture
def shared_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    p = Path(path)
    try:
        yield p
    finally:
        # WAL sidecars may linger if SQLite did not checkpoint cleanly.
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(p) + suffix)
            if f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass


class TestCrossProcessSessionIDWrites:
    def test_two_sessions_coexist_at_storage_layer(self, shared_db: Path):
        # Run A under PERSATRIX_SESSION_ID=run-a.
        seen_a = _run_worker(
            db_path=shared_db,
            agent_id="agent-x",
            peer_id="agent-y",
            summary="from run-a",
            session_id="run-a",
        )
        assert seen_a == "run-a"

        # Run B under PERSATRIX_SESSION_ID=run-b, same shared DB.
        seen_b = _run_worker(
            db_path=shared_db,
            agent_id="agent-x",
            peer_id="agent-z",
            summary="from run-b",
            session_id="run-b",
        )
        assert seen_b == "run-b"

        # Both runs' rows exist with their own session_id.  Using a
        # raw sqlite3 cursor mirrors the contract MT-SESSION-001 asks
        # the operator to validate: "open the DB, SELECT session_id".
        with sqlite3.connect(str(shared_db)) as conn:
            ep_rows = conn.execute(
                "SELECT summary, session_id FROM episodes "
                "WHERE summary LIKE 'from run-%' ORDER BY summary",
            ).fetchall()
            rel_rows = conn.execute(
                "SELECT other_participant_id, session_id FROM relationships "
                "WHERE participant_id = ? ORDER BY other_participant_id",
                ("agent-x",),
            ).fetchall()

        assert ep_rows == [
            ("from run-a", "run-a"),
            ("from run-b", "run-b"),
        ], f"episode rows mismatch: {ep_rows!r}"

        # Relationship rows: agent-y was first-seen under run-a, agent-z
        # under run-b.  The Phase 1 per-row contract is first-seen wins;
        # this assertion would catch a regression that wired session_id
        # into the ON CONFLICT UPDATE branch by accident.
        assert rel_rows == [
            ("agent-y", "run-a"),
            ("agent-z", "run-b"),
        ], f"relationship rows mismatch: {rel_rows!r}"

    def test_unset_env_lands_legacy(self, shared_db: Path):
        # Parity with the Go side's
        # ``TestResolveSessionID_UnsetDefaultsToLegacy``: a fresh
        # subprocess with no env var lands rows under "legacy".
        seen = _run_worker(
            db_path=shared_db,
            agent_id="agent-x",
            peer_id="agent-y",
            summary="from unset",
            session_id=None,
        )
        assert seen == "legacy"

        with sqlite3.connect(str(shared_db)) as conn:
            row = conn.execute(
                "SELECT session_id FROM episodes WHERE summary = ?",
                ("from unset",),
            ).fetchone()
            rel = conn.execute(
                "SELECT session_id FROM relationships "
                "WHERE participant_id = 'agent-x' "
                "AND other_participant_id = 'agent-y'",
            ).fetchone()
        assert row == ("legacy",)
        assert rel == ("legacy",)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
