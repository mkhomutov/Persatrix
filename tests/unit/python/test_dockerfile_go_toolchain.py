"""The orchestrator image's Go stage must satisfy go.mod's `go` directive.

Found live at the v0.3.16 release-prep arc: a Dependabot bump moved go.mod to
`go 1.26.0` (#877) while `Dockerfile.orchestrator` still built on
`golang:1.25-alpine`, whose toolchain is pinned local — so `go mod download`
refused with "go.mod requires go >= 1.26.0" and every `make demo-*` stack was
unbootable at the RC tip. CI never saw it: the workflows use
`go-version-file: go.mod`, and no job builds the image. This test is the
gate CI lacked — the image's base tag against the directive, read from both
files rather than pinned to either.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

_DIRECTIVE = re.compile(r"^go\s+(\d+)\.(\d+)", re.M)
_BASE = re.compile(r"^FROM\s+golang:(\d+)\.(\d+)(?:\.\d+)?(?:-[\w.]+)?\s+AS\s+builder", re.M)


def _go_mod_minor() -> tuple[int, int]:
    m = _DIRECTIVE.search((REPO_ROOT / "go.mod").read_text(encoding="utf-8"))
    assert m, "go.mod has no `go` directive"
    return int(m.group(1)), int(m.group(2))


def test_orchestrator_image_builds_on_a_toolchain_go_mod_accepts() -> None:
    text = (REPO_ROOT / "Dockerfile.orchestrator").read_text(encoding="utf-8")
    m = _BASE.search(text)
    assert m, "Dockerfile.orchestrator has no `FROM golang:<major>.<minor>… AS builder` stage"
    image = (int(m.group(1)), int(m.group(2)))
    required = _go_mod_minor()
    assert image >= required, (
        f"Dockerfile.orchestrator builds on golang:{image[0]}.{image[1]} but go.mod "
        f"requires go >= {required[0]}.{required[1]} — with GOTOOLCHAIN=local in the "
        f"image, `go mod download` refuses and no compose stack can boot"
    )
