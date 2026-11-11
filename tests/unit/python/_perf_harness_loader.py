"""One loader for the personal-tier latency harness, shared by its test files.

``tests/perf/personal_tier_latency.py`` is a script CI runs by path, not a
module a test can import, so the tests that pin it load it by path. Three
test files each carried their own copy of the loader. The oldest copy never
had the step that registers the module: the step arrived with the harness's
dataclasses, in the two newer copies only. So that file passed only when a
sibling file had loaded the harness first (ISSUE-0233). They all use this
one now.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

HARNESS_PATH = Path(__file__).resolve().parents[3] / "tests" / "perf" / "personal_tier_latency.py"


def load_perf_harness() -> ModuleType:
    """Load ``tests/perf/personal_tier_latency.py`` as a fresh module, by path."""
    assert HARNESS_PATH.is_file(), f"perf harness missing: {HARNESS_PATH}"
    spec = importlib.util.spec_from_file_location("personal_tier_latency", HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register the module before running it. The harness defines dataclasses,
    # and ``dataclasses`` looks the class's module up in ``sys.modules`` while
    # it builds the class. With no entry there it fails with ``AttributeError:
    # 'NoneType' object has no attribute '__dict__'``.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
