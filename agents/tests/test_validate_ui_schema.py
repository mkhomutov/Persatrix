"""RFC 0048 Phase 1 PR 2 — config/ui.yaml schema validation.

Pins the feature-toggle contract enforced by ``schemas/ui.schema.json``:

* The committed ``config/ui.yaml`` passes ``make validate``.
* The schema is wired into ``validate.py``'s ``_SCHEMA_MAP`` so the file is
  actually checked (a missing entry would silently skip it).
* The runtime-derived ``available`` flag MUST NOT be authored — a ``ui.yaml``
  carrying an ``available:`` key fails validation (per-panel
  ``additionalProperties:false``). This is the single enforcement of the
  "ships dark → flip on when the subsystem is wired" contract (RFC 0048 §C).
"""

from __future__ import annotations

from pathlib import Path

from agents.validate import _SCHEMA_MAP, validate_config_dir

# Repo root is three levels up from agents/tests/.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCHEMAS_DIR = str(_REPO_ROOT / "schemas")


def test_ui_yaml_is_wired_into_schema_map() -> None:
    """validate.py must know to check ui.yaml, or the schema is dead weight."""
    assert _SCHEMA_MAP.get("ui.yaml") == "ui.schema.json"


def test_committed_ui_yaml_validates() -> None:
    """The shipped config/ui.yaml passes its schema."""
    config_dir = str(_REPO_ROOT / "config")
    success, errors, files_checked = validate_config_dir(config_dir, _SCHEMAS_DIR)
    ui_errors = [e for e in errors if e.file.endswith("ui.yaml")]
    assert ui_errors == [], f"config/ui.yaml must validate cleanly: {[str(e) for e in ui_errors]}"
    assert files_checked >= 1
    assert success or not ui_errors


def test_authored_available_key_is_rejected(tmp_path: Path) -> None:
    """An `available:` key in ui.yaml is runtime-derived, so the schema rejects it."""
    (tmp_path / "ui.yaml").write_text(
        "panels:\n  chat:\n    enabled: true\n    available: true\n",
        encoding="utf-8",
    )

    success, errors, _ = validate_config_dir(str(tmp_path), _SCHEMAS_DIR)
    assert not success, "an authored `available:` key must fail validation"
    assert any(e.file.endswith("ui.yaml") for e in errors)


def test_unknown_per_panel_key_is_rejected(tmp_path: Path) -> None:
    """A typo'd per-panel key surfaces at validate time, not silently as a no-op."""
    (tmp_path / "ui.yaml").write_text(
        "panels:\n  chat:\n    enbaled: true\n",  # typo: enbaled
        encoding="utf-8",
    )

    success, errors, _ = validate_config_dir(str(tmp_path), _SCHEMAS_DIR)
    assert not success
    assert any(e.file.endswith("ui.yaml") for e in errors)
