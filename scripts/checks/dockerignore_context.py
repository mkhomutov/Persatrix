#!/usr/bin/env python3
"""Docker build-context hygiene — prove `.dockerignore` excludes nested node_modules.

`Dockerfile.orchestrator` runs a clean `npm ci` inside the image and then does
`COPY web/ ./`. If `.dockerignore` fails to exclude the host's `web/node_modules`,
that stale host tree leaks into the build context and clobbers the clean install
— which on Apple Silicon shipped a darwin-only rollup that broke `vite build`
(ISSUE-0104). The fix was `.dockerignore`: `node_modules/` -> `**/node_modules/`.

Why this needs real Docker (not a string/gitignore check): Docker's
`.dockerignore` matcher anchors a slash-bearing pattern like `node_modules/` to
the **context root**, so it does NOT match the nested `web/node_modules`. Git's
ignore semantics match that same pattern in any directory. A pure-Python
gitignore check would therefore call the buggy pattern "safe" and miss the
regression entirely. The only faithful test is to run a real `docker build` and
observe what reaches the context.

Why this needs a seeded sentinel: a clean CI checkout has no `web/node_modules`,
so `COPY web/ ./` copies nothing and the probe passes vacuously even with the
buggy pattern — this is exactly why the original break was invisible to CI. We
therefore plant a sentinel under `web/node_modules/` before probing and assert
it does NOT reach the image.

Usage::

    python scripts/checks/dockerignore_context.py [--strict]

Without ``--strict`` the check skips (exit 0) with a warning if Docker is
unavailable, so local runs without a daemon are not blocked. CI passes
``--strict`` so a missing daemon fails loudly rather than silently skipping the
guard.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.checks import ensure_utf8_stdout  # noqa: E402

SENTINEL_NAME = ".dockerignore-leak-sentinel"
PROBE_IMAGE_TAG = "persatrix-dockerignore-probe:check"

# A throwaway probe stage that mirrors the orchestrator's `COPY web/ ./` step and
# fails the build if our seeded sentinel survived the build context.
PROBE_DOCKERFILE = f"""\
FROM alpine:3.20
WORKDIR /w
COPY web/ ./
RUN if [ -e node_modules/{SENTINEL_NAME} ]; then \\
        echo "LEAK: web/node_modules reached the build context"; \\
        exit 1; \\
    else \\
        echo "clean: web/node_modules excluded from the build context"; \\
    fi
"""


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "info"],
            cwd=REPO_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


def run_probe() -> int:
    """Return 0 if the build context is clean, 1 if web/node_modules leaked."""
    web_node_modules = REPO_ROOT / "web" / "node_modules"
    sentinel = web_node_modules / SENTINEL_NAME
    created_dir = not web_node_modules.exists()

    try:
        web_node_modules.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("ISSUE-0104 leak sentinel; safe to delete.\n", encoding="utf-8")

        proc = subprocess.run(
            ["docker", "build", "--no-cache", "-f", "-", "-t", PROBE_IMAGE_TAG, "."],
            cwd=REPO_ROOT,
            input=PROBE_DOCKERFILE,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if proc.returncode != 0:
            print(proc.stdout, end="")
            print(
                "\nFAIL: nested web/node_modules leaked into the Docker build context.\n"
                "      `.dockerignore` must list `**/node_modules/` (a root-anchored\n"
                "      `node_modules/` does not match the nested web/node_modules). See\n"
                "      docs/issues/ISSUE-0104-arm64-orchestrator-docker-ui-build-broken.md.",
            )
            return 1

        print("OK: .dockerignore excludes nested web/node_modules from the build context.")
        return 0
    finally:
        sentinel.unlink(missing_ok=True)
        if created_dir:
            shutil.rmtree(web_node_modules, ignore_errors=True)
        subprocess.run(
            ["docker", "image", "rm", "-f", PROBE_IMAGE_TAG],
            cwd=REPO_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def main() -> int:
    ensure_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail (exit 1) if Docker is unavailable instead of skipping the check.",
    )
    args = parser.parse_args()

    if not _docker_available():
        msg = "Docker is not available"
        if args.strict:
            print(f"FAIL: {msg}; cannot run the build-context hygiene check (--strict).")
            return 1
        print(f"SKIP: {msg}; skipping build-context hygiene check (pass --strict to require it).")
        return 0

    return run_probe()


if __name__ == "__main__":
    raise SystemExit(main())
