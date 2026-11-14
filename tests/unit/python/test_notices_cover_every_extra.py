"""The notices file is written only from an install that has every package.

`make notices` lists whatever is installed in the Python that runs it. The
Gemini and watsonx SDKs are an optional extra that the usual install lacks,
so `THIRD_PARTY_NOTICES.md` named neither, nor anything they pull in
(ISSUE-0220). The generator now reads `agents/pyproject.toml` and refuses to
write the file from an install that is missing a package the manifest can
install, saying which install has them all.
"""

from __future__ import annotations

import tomllib
from typing import Any

import pytest
from _test_infra import REPO_ROOT

from scripts import generate_third_party_notices as notices

MANIFEST = REPO_ROOT / "agents" / "pyproject.toml"


def _project() -> dict[str, Any]:
    project: dict[str, Any] = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))["project"]
    return project


def test_every_package_the_manifest_can_install_is_declared() -> None:
    declared = notices.declared_python_packages()
    # One name from the base list and one from each extra, read off the
    # manifest as it is today.
    for name in ("aiohttp", "pytest", "google-genai", "ibm-watsonx-ai"):
        assert name in declared, f"{name} is in agents/pyproject.toml and was not read"
    # The project itself is not a third party: `providers` names it to pull
    # in the other extras.
    assert "persatrix-agents" not in declared
    count = len(_project()["dependencies"]) + sum(
        len(specs) for specs in _project()["optional-dependencies"].values()
    )
    # Every entry but the one self-reference.
    assert len(declared) == count - 1


def test_a_new_extra_is_read_without_a_change_to_the_generator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(
        MANIFEST.read_text(encoding="utf-8").replace(
            "[project.scripts]", 'another = [\n    "Some_New.SDK>=1,<2",\n]\n\n[project.scripts]'
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(notices, "AGENTS_MANIFEST", manifest)

    assert notices.declared_python_packages()["some-new-sdk"] == "the `another` extra"


def test_an_install_without_a_declared_package_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declared = set(notices.declared_python_packages())
    monkeypatch.setattr(
        notices, "installed_python_packages", lambda: declared - {"google-genai", "pandas"}
    )

    with pytest.raises(notices.MissingPythonPackagesError) as raised:
        notices.check_python_install()

    message = str(raised.value)
    # pandas is not declared (the watsonx SDK pulls it in), so only the
    # declared one can be named.
    assert "google-genai (the `gemini` extra)" in message
    assert "make build-agents-providers" in message


def test_an_install_with_every_declared_package_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declared = set(notices.declared_python_packages())
    monkeypatch.setattr(notices, "installed_python_packages", lambda: declared | {"extra-one"})

    notices.check_python_install()


def test_installed_names_are_compared_in_one_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The watsonx SDK installs as `ibm_watsonx_ai` and is declared as `ibm-watsonx-ai`."""

    class _Dist:
        def __init__(self, name: str) -> None:
            self.metadata = {"Name": name}

    monkeypatch.setattr(
        notices.importlib.metadata,
        "distributions",
        lambda: [_Dist("ibm_watsonx_ai"), _Dist("PyYAML"), _Dist("zope.interface")],
    )

    assert notices.installed_python_packages() == {"ibm-watsonx-ai", "pyyaml", "zope-interface"}


def test_the_generator_stops_before_it_writes_when_a_package_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "NOTICES.md"
    monkeypatch.setattr(notices, "installed_python_packages", lambda: set())
    monkeypatch.setattr(notices, "collect_go", lambda: [])
    monkeypatch.setattr(notices, "collect_rust", lambda: [])
    monkeypatch.setattr("sys.argv", ["generate_third_party_notices.py", "-o", str(out)])

    assert notices.main() == 2

    assert not out.exists()
    assert "make build-agents-providers" in capsys.readouterr().err
