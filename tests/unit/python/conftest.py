import pytest

from agents.tools.registry import clear_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()
