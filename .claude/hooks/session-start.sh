#!/bin/bash
# Readies a Claude Code cloud session: the repo's .venv with the agents'
# dependencies at CI's pins, and the orchestrator binary the EXP-001 harness
# starts. Safe to run again; each step reuses what is already there.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"

# Build output goes to stderr: what a SessionStart hook prints on stdout is
# added to the session's context.
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv >&2
  .venv/bin/pip install --quiet --upgrade pip >&2
fi
make build-agents PIP="$PWD/.venv/bin/pip" >&2
make build-orchestrator >&2

# The pre-commit hook regenerates FILEMAP.md and docs/merged-prs.md and runs
# the commit gates; it runs under $PYTHON, so point it at the .venv. A cloud
# clone is shallow, and merged-prs.md read from it would keep only the last
# few dozen PRs, so fetch the whole history first.
if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
  git fetch --unshallow --quiet origin >&2
fi
.venv/bin/python scripts/install_hooks.py --force >&2
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PYTHON=\"$PWD/.venv/bin/python\"" >> "$CLAUDE_ENV_FILE"
fi

echo "Session ready: .venv (agents installed), bin/persatrix-server built, pre-commit hook installed."
