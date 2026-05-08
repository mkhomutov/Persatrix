#!/usr/bin/env python3
"""Detect orphan generated protobuf artifacts (ISSUE-0023).

When a `proto/<name>.proto` source file is deleted or renamed, the
matching generated stubs in ``agents/generated/`` and
``internal/generated/`` do not vanish on their own. Without an
explicit gate the orphans live on indefinitely, ambiguating the
"`.proto` is the source of truth" contract: a future contributor
reading the orphan stub may treat it as a live wire surface.

This module exposes pure helpers that, given the current file/
directory listings and the set of expected names derived from the
source `.proto`s, return the orphans. The helpers are intentionally
I/O-free so the logic is unit-testable; the CLI wrapper at the
bottom is the only thing that walks the filesystem.

Naming conventions assumed by this module
-----------------------------------------
* Python: every `proto/<stem>.proto` produces three files in
  ``agents/generated/``: ``<stem>_pb2.py``, ``<stem>_pb2.pyi``, and
  ``<stem>_pb2_grpc.py``. Files that do not match any of these three
  suffixes (e.g., ``__init__.py``) are not considered protoc output
  and are left alone by the orphan check.
* Go: every `proto/<stem>.proto` declares ``option go_package =
  "<import-path>/<pkg>";`` (per [proto/log_service.proto] and
  [proto/task.proto]). The directory under ``internal/generated/``
  is the basename of that path. The mapping is not algorithmic —
  ``log_service.proto`` declares ``logpb``, not ``log_servicepb``
  — so the helper takes the set of expected package names as input
  and the CLI wrapper does the parsing.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROTO_DIR = REPO_ROOT / "proto"
PYTHON_GENERATED_DIR = REPO_ROOT / "agents" / "generated"
GO_GENERATED_DIR = REPO_ROOT / "internal" / "generated"

_PYTHON_GENERATED_SUFFIXES = ("_pb2.py", "_pb2.pyi", "_pb2_grpc.py")

# Match `option go_package = "<value>";` allowing whitespace anywhere
# the proto grammar permits it. The value is captured raw and
# post-processed to strip any `;alias` suffix (proto's import-alias
# form: `option go_package = "path;alias";`).
_GO_PACKAGE_RE = re.compile(
    r'option\s+go_package\s*=\s*"([^"]+)"\s*;',
)


def _python_stem(filename: str) -> str | None:
    """Return the proto stem a generated Python file traces back to.

    Returns ``None`` for files that are not protoc output.
    """
    for suffix in _PYTHON_GENERATED_SUFFIXES:
        if filename.endswith(suffix):
            return filename[: -len(suffix)]
    return None


def find_orphan_python_generated(
    generated_filenames: set[str],
    proto_stems: set[str],
) -> set[str]:
    """Return the subset of ``generated_filenames`` that do not trace
    back to a `.proto` in ``proto_stems``.

    Files whose name does not match any known protoc output suffix
    are ignored — they are not generated artifacts and never count
    as orphans.
    """
    orphans: set[str] = set()
    for filename in generated_filenames:
        stem = _python_stem(filename)
        if stem is None:
            continue
        if stem not in proto_stems:
            orphans.add(filename)
    return orphans


def find_orphan_go_generated(
    package_dir_names: set[str],
    expected_package_names: set[str],
) -> set[str]:
    """Return the subset of ``package_dir_names`` that do not appear
    in ``expected_package_names``.
    """
    return package_dir_names - expected_package_names


def parse_go_package_dir(proto_text: str) -> str | None:
    """Extract the basename of the ``option go_package = "..."``
    value from a `.proto` file's text.

    Returns ``None`` if no ``go_package`` line is present (the
    `.proto` declares no Go output and therefore expects no
    package directory).

    Strips any ``;alias`` suffix per proto syntax — the alias is
    the import name, not the directory.
    """
    match = _GO_PACKAGE_RE.search(proto_text)
    if not match:
        return None
    value = match.group(1)
    # Strip the import-alias suffix: "path/to/pkg;alias" → "path/to/pkg".
    path_part = value.split(";", 1)[0]
    # The directory is the last path segment.
    return path_part.rsplit("/", 1)[-1]


def _scan_repo() -> tuple[set[str], set[str], set[str], set[str]]:
    """Walk the repo and return ``(python_generated, go_packages,
    proto_stems, expected_go_packages)``.
    """
    proto_files = list(PROTO_DIR.glob("*.proto"))
    proto_stems = {p.stem for p in proto_files}

    expected_go_packages: set[str] = set()
    for proto_file in proto_files:
        pkg = parse_go_package_dir(proto_file.read_text(encoding="utf-8"))
        if pkg is not None:
            expected_go_packages.add(pkg)

    python_generated: set[str] = set()
    if PYTHON_GENERATED_DIR.is_dir():
        python_generated = {p.name for p in PYTHON_GENERATED_DIR.iterdir() if p.is_file()}

    go_packages: set[str] = set()
    if GO_GENERATED_DIR.is_dir():
        go_packages = {p.name for p in GO_GENERATED_DIR.iterdir() if p.is_dir()}

    return python_generated, go_packages, proto_stems, expected_go_packages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail if generated protobuf artifacts have no source .proto.",
    )
    parser.parse_args(argv)

    python_generated, go_packages, proto_stems, expected_go_packages = _scan_repo()
    py_orphans = find_orphan_python_generated(python_generated, proto_stems)
    go_orphans = find_orphan_go_generated(go_packages, expected_go_packages)

    if not py_orphans and not go_orphans:
        print("[OK] no orphan generated protobuf artifacts")
        return 0

    if py_orphans:
        print("[FAIL] orphan Python generated files (no source .proto):")
        for name in sorted(py_orphans):
            print(f"  - agents/generated/{name}")
    if go_orphans:
        print("[FAIL] orphan Go generated package dirs (no source .proto):")
        for name in sorted(go_orphans):
            print(f"  - internal/generated/{name}/")
    print(
        "\nFix: delete the orphan(s) above, or restore the corresponding "
        "proto/<stem>.proto source. See docs/issues/ISSUE-0023.",
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
