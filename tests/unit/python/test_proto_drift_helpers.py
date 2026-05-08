"""Unit tests for the proto-drift orphan-detection helper (ISSUE-0023).

The helper is a pure function: given the basenames of files currently
present in a generated/ directory and the basenames of source `.proto`
files, return the set of generated files with no source. The helper
backs the CI gate that catches the drift class of "a `.proto` was
deleted but its generated stubs survived in the working tree."
"""

from __future__ import annotations

from scripts.checks.proto_drift import (
    find_orphan_go_generated,
    find_orphan_python_generated,
    parse_go_package_dir,
)


class TestFindOrphanPythonGenerated:
    """Python generated files in agents/generated/ are
    `<stem>_pb2.py`, `<stem>_pb2.pyi`, and `<stem>_pb2_grpc.py`. Each
    must trace back to a `proto/<stem>.proto` source.
    """

    def test_returns_empty_when_every_generated_has_source(self) -> None:
        generated = {
            "task_pb2.py",
            "task_pb2.pyi",
            "task_pb2_grpc.py",
            "log_service_pb2.py",
            "log_service_pb2.pyi",
            "log_service_pb2_grpc.py",
        }
        proto_stems = {"task", "log_service"}

        assert find_orphan_python_generated(generated, proto_stems) == set()

    def test_flags_pb2_with_no_proto_source(self) -> None:
        generated = {"task_pb2.py", "ghost_pb2.py"}
        proto_stems = {"task"}

        assert find_orphan_python_generated(generated, proto_stems) == {
            "ghost_pb2.py",
        }

    def test_flags_pb2_pyi_and_pb2_grpc_independently(self) -> None:
        generated = {
            "task_pb2.py",
            "ghost_pb2.pyi",
            "ghost_pb2_grpc.py",
        }
        proto_stems = {"task"}

        assert find_orphan_python_generated(generated, proto_stems) == {
            "ghost_pb2.pyi",
            "ghost_pb2_grpc.py",
        }

    def test_ignores_non_generated_files(self) -> None:
        # __init__.py and other Python files in agents/generated/ are
        # not protoc output and must not be flagged as orphans.
        generated = {"__init__.py", "task_pb2.py", "README.md"}
        proto_stems = {"task"}

        assert find_orphan_python_generated(generated, proto_stems) == set()

    def test_empty_inputs_return_empty(self) -> None:
        assert find_orphan_python_generated(set(), set()) == set()


class TestFindOrphanGoGenerated:
    """Go generated files live in `internal/generated/<pkg>/` —
    one package directory per `.proto` source. The directory name
    comes from each proto's ``option go_package = "..../<pkg>"``
    line (the stem-to-package mapping is not algorithmic; e.g.,
    ``log_service.proto`` → ``logpb``). The helper takes the set
    of expected package names so the parsing of ``go_package``
    stays in the CLI wrapper and the pure logic stays trivial.
    """

    def test_returns_empty_when_every_package_has_source(self) -> None:
        package_dirs = {"taskpb", "logpb"}
        expected = {"taskpb", "logpb"}

        assert find_orphan_go_generated(package_dirs, expected) == set()

    def test_flags_package_dir_with_no_expected_match(self) -> None:
        package_dirs = {"taskpb", "ghostpb"}
        expected = {"taskpb"}

        assert find_orphan_go_generated(package_dirs, expected) == {"ghostpb"}

    def test_empty_inputs_return_empty(self) -> None:
        assert find_orphan_go_generated(set(), set()) == set()


class TestParseGoPackageDir:
    """``parse_go_package_dir`` extracts the basename of the
    ``option go_package = "..."`` value from a `.proto` file's text,
    so the orphan helper can be told which directory each proto
    expects to populate. Returns ``None`` if no ``go_package`` line
    is present (a `.proto` with no Go output is not an orphan
    source — it just means no Go package is expected).
    """

    def test_extracts_basename_from_full_import_path(self) -> None:
        text = (
            'syntax = "proto3";\n'
            'option go_package = "github.com/mkhomutov/persatrix/internal/generated/logpb";\n'
        )
        assert parse_go_package_dir(text) == "logpb"

    def test_extracts_basename_from_short_path(self) -> None:
        text = 'option go_package = "taskpb";\n'
        assert parse_go_package_dir(text) == "taskpb"

    def test_returns_none_when_no_go_package_directive(self) -> None:
        text = 'syntax = "proto3";\npackage persatrix.v1;\n'
        assert parse_go_package_dir(text) is None

    def test_strips_alias_suffix(self) -> None:
        # `option go_package = "path;alias"` is valid proto syntax.
        # The directory is `path`'s basename; `alias` is the import
        # name and must be discarded.
        text = 'option go_package = "github.com/x/y/foopb;foo";\n'
        assert parse_go_package_dir(text) == "foopb"

    def test_tolerates_extra_whitespace(self) -> None:
        text = '   option   go_package   =   "x/y/barpb"  ;  \n'
        assert parse_go_package_dir(text) == "barpb"
