"""Generated `_pb2.py` / `_pb2_grpc.py` parity test (ISSUE-0023).

Companion to [`test_task_pb2_pyi_parity.py`](test_task_pb2_pyi_parity.py)
which already gates the `*.pyi` mypy stubs. That gate left two
classes of drift uncaught:

1. A hand-edit to a generated `*_pb2.py` or `*_pb2_grpc.py` that
   does not survive a `make proto-python` regen.
2. A `.proto` change that the contributor regenerated the `.pyi`
   for (because the existing CI step runs `make proto-python-check`
   which only emits `--mypy_out`) but forgot to also commit the
   updated `_pb2.py` / `_pb2_grpc.py`.

This test asserts that every checked-in `_pb2.py` and `_pb2_grpc.py`
in `agents/generated/` is byte-equivalent to what `make proto-python`
would produce now from `proto/*.proto`. Line endings are normalised
(CRLF → LF) before comparison so the gate works on Windows checkouts
where ``core.autocrlf=true`` rewrites LF to CRLF in the working tree.

Why a freshness gate is necessary
---------------------------------
The repo's source-of-truth contract is "`.proto` is authoritative;
generated stubs are derived". Without a regen-and-diff gate, hand-
edits to the generated files survive review and silently re-export
shapes the proto no longer describes. ISSUE-0016 + ISSUE-0017
already paid this cost twice — see those issues' "Notes" sections
for the prior incidents.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PROTO_DIR = REPO_ROOT / "proto"
GENERATED_DIR = REPO_ROOT / "agents" / "generated"

# `grpc_tools.protoc` emits top-level `import X_pb2` in every
# `_pb2_grpc.py`. The Makefile's `proto-python` target rewrites
# these to relative form so the stubs work in the installed
# layout (`persatrix_agents.generated.*`). Mirror the rewrite in
# the test so the comparison is apples-to-apples.
_REL_IMPORT_REWRITE = re.compile(r"^import (\w+_pb2)\b", re.MULTILINE)


def _have_grpc_tools() -> bool:
    try:
        import grpc_tools  # noqa: F401
    except ImportError:
        return False
    return True


def _proto_files() -> list[Path]:
    return sorted(PROTO_DIR.glob("*.proto"))


def _normalise_eol(data: bytes) -> bytes:
    """Strip CRLF → LF so Windows checkouts compare equal.

    ``core.autocrlf=true`` (the default Git for Windows install
    setting) rewrites LF to CRLF on checkout. The committed file
    on disk is therefore CRLF on Windows, while ``protoc`` always
    emits LF. The semantic content is identical; the gate
    compares post-normalisation.
    """
    return data.replace(b"\r\n", b"\n")


def _apply_relative_import_rewrite(text: str) -> str:
    """Rewrite ``import X_pb2`` to ``from . import X_pb2`` to
    match what `make proto-python` produces.
    """
    return _REL_IMPORT_REWRITE.sub(r"from . import \1", text)


@pytest.mark.skipif(
    not _have_grpc_tools(),
    reason="grpc_tools not installed; freshness check deferred to CI.",
)
class TestGeneratedPb2Freshness:
    """Re-running the generator must produce the checked-in
    `_pb2.py` byte-for-byte (modulo line endings). If this fails,
    run `make proto-python` and commit the result.
    """

    @pytest.mark.parametrize("proto_file", _proto_files(), ids=lambda p: p.name)
    def test_pb2_matches_checked_in(
        self, proto_file: Path, tmp_path: Path,
    ) -> None:
        result = subprocess.run(
            [
                sys.executable, "-m", "grpc_tools.protoc",
                f"--python_out={tmp_path}",
                f"-I{PROTO_DIR}",
                str(proto_file),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode != 0:
            pytest.fail(
                f"protoc --python_out failed for {proto_file.name} "
                f"(rc={result.returncode}).\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )

        pb2_name = proto_file.stem + "_pb2.py"
        regenerated = tmp_path / pb2_name
        committed = GENERATED_DIR / pb2_name

        if not regenerated.exists():
            pytest.fail(
                f"protoc --python_out produced no {pb2_name} in {tmp_path}; "
                f"contents: {list(tmp_path.iterdir())}",
            )
        if not committed.exists():
            pytest.fail(
                f"agents/generated/{pb2_name} is missing. Run:\n"
                f"    make proto-python\n"
                f"and commit the resulting file.",
            )

        if _normalise_eol(committed.read_bytes()) != _normalise_eol(
            regenerated.read_bytes(),
        ):
            pytest.fail(
                f"agents/generated/{pb2_name} is stale relative to "
                f"proto/{proto_file.name}. Run:\n"
                f"    make proto-python\n"
                f"and commit the resulting diff.",
            )


@pytest.mark.skipif(
    not _have_grpc_tools(),
    reason="grpc_tools not installed; freshness check deferred to CI.",
)
class TestGeneratedPb2GrpcFreshness:
    """Re-running the generator (then applying the relative-import
    rewrite the Makefile applies) must produce the checked-in
    `_pb2_grpc.py` byte-for-byte (modulo line endings).
    """

    @pytest.mark.parametrize("proto_file", _proto_files(), ids=lambda p: p.name)
    def test_pb2_grpc_matches_checked_in(
        self, proto_file: Path, tmp_path: Path,
    ) -> None:
        result = subprocess.run(
            [
                sys.executable, "-m", "grpc_tools.protoc",
                f"--python_out={tmp_path}",
                f"--grpc_python_out={tmp_path}",
                f"-I{PROTO_DIR}",
                str(proto_file),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode != 0:
            pytest.fail(
                f"protoc --grpc_python_out failed for {proto_file.name} "
                f"(rc={result.returncode}).\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )

        grpc_name = proto_file.stem + "_pb2_grpc.py"
        regenerated_path = tmp_path / grpc_name
        committed = GENERATED_DIR / grpc_name

        if not regenerated_path.exists():
            pytest.fail(
                f"protoc --grpc_python_out produced no {grpc_name} in {tmp_path}; "
                f"contents: {list(tmp_path.iterdir())}",
            )
        if not committed.exists():
            pytest.fail(
                f"agents/generated/{grpc_name} is missing. Run:\n"
                f"    make proto-python\n"
                f"and commit the resulting file.",
            )

        regenerated_text = _apply_relative_import_rewrite(
            regenerated_path.read_text(encoding="utf-8"),
        )
        if _normalise_eol(committed.read_bytes()) != _normalise_eol(
            regenerated_text.encode("utf-8"),
        ):
            pytest.fail(
                f"agents/generated/{grpc_name} is stale relative to "
                f"proto/{proto_file.name}. Run:\n"
                f"    make proto-python\n"
                f"and commit the resulting diff.",
            )


class TestNoOrphanGeneratedFiles:
    """Every committed `_pb2.py` / `_pb2.pyi` / `_pb2_grpc.py` in
    `agents/generated/` and every package directory in
    `internal/generated/` must trace back to a `proto/<stem>.proto`
    source. Catches the deletion drift class.

    The pure helper is unit-tested in
    [`test_proto_drift_helpers.py`](test_proto_drift_helpers.py);
    this test is the live-repo wiring.
    """

    def test_repo_has_no_orphans(self) -> None:
        # Defensive: shell out to the same CLI CI invokes so a
        # green here matches a green CI run exactly.
        check_script = REPO_ROOT / "scripts" / "checks" / "proto_drift.py"
        result = subprocess.run(
            [sys.executable, str(check_script)],
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode != 0:
            pytest.fail(
                f"proto_drift.py reported orphan generated artifacts "
                f"(rc={result.returncode}).\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )


__all__ = [
    "TestGeneratedPb2Freshness",
    "TestGeneratedPb2GrpcFreshness",
    "TestNoOrphanGeneratedFiles",
]
