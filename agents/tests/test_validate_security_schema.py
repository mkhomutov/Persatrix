"""RFC 0039 PR 3 — config/security.yaml schema validation.

Pins the auth-config contract enforced by ``schemas/security.schema.json``:

* The committed ``config/security.yaml`` passes ``make validate``.
* The schema is wired into ``validate.py``'s ``_SCHEMA_MAP`` so the file is
  actually checked (a missing entry would silently skip it).
* ``auth.mode`` is a closed enum — the loud-fail posture starts at authoring
  time: ``mode: enbaled`` (the typo the posture exists for) fails ``make
  validate`` before it can fail orchestrator startup.
* Unknown keys are rejected (``additionalProperties: false``) so a typo'd
  knob never silently no-ops.

The Go loader (``internal/server/auth_config.go``) remains the semantic
authority — cross-field checks live there; this schema is the authoring gate.
"""

from __future__ import annotations

from pathlib import Path

from agents.validate import _SCHEMA_MAP, validate_config_dir

# Repo root is three levels up from agents/tests/.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCHEMAS_DIR = str(_REPO_ROOT / "schemas")


def _validate_content(tmp_path: Path, content: str) -> list[str]:
    """Validate a synthetic security.yaml, returning its error strings."""
    (tmp_path / "security.yaml").write_text(content, encoding="utf-8")
    _success, errors, _checked = validate_config_dir(str(tmp_path), _SCHEMAS_DIR)
    return [str(e) for e in errors if e.file.endswith("security.yaml")]


def test_security_yaml_is_wired_into_schema_map() -> None:
    """validate.py must know to check security.yaml, or the schema is dead weight."""
    assert _SCHEMA_MAP.get("security.yaml") == "security.schema.json"


def test_committed_security_yaml_validates() -> None:
    """The shipped config/security.yaml passes its schema."""
    config_dir = str(_REPO_ROOT / "config")
    _success, errors, files_checked = validate_config_dir(config_dir, _SCHEMAS_DIR)
    sec_errors = [str(e) for e in errors if e.file.endswith("security.yaml")]
    assert sec_errors == [], f"config/security.yaml must validate cleanly: {sec_errors}"
    assert files_checked >= 1


def test_mode_is_a_closed_enum(tmp_path: Path) -> None:
    """The RFC 0039 §H rollout switch rejects anything but disabled|enabled."""
    assert _validate_content(tmp_path, "auth:\n  mode: enbaled\n")
    assert not _validate_content(tmp_path, "auth:\n  mode: enabled\n")


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    """additionalProperties: false — a typo'd knob fails, never no-ops."""
    assert _validate_content(tmp_path, "auth:\n  session_ttk: 24h\n")
    typo_limiter = "auth:\n  login_throttle:\n    per_src:\n      calls_per_window: 5\n"
    assert _validate_content(tmp_path, typo_limiter)


def test_argon_memory_floor_matches_loader(tmp_path: Path) -> None:
    """The schema's 8 MiB Argon2id floor — mirrored by the Go loader's
    ``minArgonMemoryKiB`` so the authoring gate and the semantic
    authority can never disagree on it (review follow-up)."""
    assert _validate_content(
        tmp_path, "auth:\n  password:\n    argon2_memory_kib: 4096\n"
    )
    assert not _validate_content(
        tmp_path, "auth:\n  password:\n    argon2_memory_kib: 8192\n"
    )


def test_limiter_and_ttl_bounds(tmp_path: Path) -> None:
    """Limiter integers have a floor of 1; TTLs must be Go durations."""
    assert _validate_content(
        tmp_path,
        "auth:\n  login_throttle:\n    per_source:\n      calls_per_window: 0\n",
    )
    assert _validate_content(tmp_path, "auth:\n  session_ttl: soon\n")
    assert not _validate_content(tmp_path, "auth:\n  session_ttl: 1h30m\n")
