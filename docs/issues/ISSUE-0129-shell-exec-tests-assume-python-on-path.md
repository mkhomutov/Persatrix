---
id: ISSUE-0129
summary: "The five `TestShellExec` tests that actually spawn a process hard-code the bare `python` binary, which macOS has not shipped since it dropped Python 2 — only `python3` exists. They fail locally with `Command not found: python` while CI stays green, because `actions/setup-python` puts a `python` shim on PATH. The failure is permanent, unrelated to whatever a developer is working on, and has already been waved through in PR descriptions as an authoring-sandbox artifact — a mischaracterisation that is itself the danger, since the five tests are the whole non-allowlist half of `shell_exec` coverage (success path, exit code, timeout kill, output truncation)."
status: open
severity: low
area: tests
created: 2026-08-15
refs:
  - tests/unit/python/test_builtin_tools_filesystem_shell.py
  - agents/tools/builtin.py
  - agents/tools/permissions.py
  - .github/workflows/ci.yml
---

## Summary

Five tests assume a `python` executable on PATH. Half the developers
running the suite do not have one, and CI does — so the suite is red
locally, green in CI, and nobody is wrong.

## Context

`TestShellExec` (`tests/unit/python/test_builtin_tools_filesystem_shell.py`)
drives `builtin.shell_exec` with commands like:

```python
result = await builtin.shell_exec('python -c "print(\'hello\')"')
```

`shell_exec` resolves the binary through `asyncio.create_subprocess_exec`,
which does no shell PATH fallback and no `python`→`python3` aliasing. On
macOS — where `/usr/bin/python` was removed with the Python 2 sunset,
leaving only `/usr/bin/python3` — every one of those calls returns:

```
ToolResult(success=False, data=None,
           error='Command not found: python',
           error_type='FileNotFoundError')
```

Five of the eleven tests in the class fail: `test_allowed_command`,
`test_nonzero_exit_code`, `test_timeout_kills_process`,
`test_large_stdout_truncated`, `test_timeout_clamped_to_bounds`. The
other six never reach an exec — they assert permission denials, allowlist
rejections, and syntax errors — so they pass everywhere.

CI does not see it. Every Python job runs `actions/setup-python@v5`
(`.github/workflows/ci.yml`), which installs a `python` shim alongside
`python3`. The allowlist is not the cause either: `_setup_tools` grants
`allowed_commands: ["echo", "python", "cat"]`, and the failure is a
`FileNotFoundError` raised *after* `is_command_allowed` returns true.

Found during the [PR #829](https://github.com/mkhomutov/Persatrix/pull/829)
review, where the five failures were reproduced on clean `main` to
confirm they predate the branch.

## Impact

Low in production terms — no shipped code is wrong. The cost is entirely
in what a permanently-red suite does to a review.

The failures have already been characterised in PR descriptions as "a
sandbox artifact of the authoring environment (shell exec restricted)".
That is not what is happening: shell exec is not restricted, the binary
is absent. The gap between the two is the whole problem — an accepted
red block that nobody re-diagnoses is indistinguishable from a real
regression in the same file, and these five tests are the *only*
coverage of `shell_exec`'s execution behaviour. Everything they assert —
that an allowlisted command actually runs, that a non-zero exit is
reported with its code, that the timeout kills the process rather than
leaking it, that stdout past `MAX_OUTPUT_BYTES` is truncated — is
unverified on any machine without a `python` shim, and CI's green tick
says otherwise. `shell_exec` runs LLM-suggested commands; it is not the
surface to have quietly untested on half the development machines.

## Proposed fix / investigation path

Use the interpreter already running the tests rather than a name that
may not resolve:

```python
import sys
_PY = sys.executable

async def test_allowed_command(self, tmp_path):
    _setup_tools(tmp_path)
    result = await builtin.shell_exec(f'{shlex.quote(_PY)} -c "print(\'hello\')"')
```

**The trap:** `PermissionGate.is_command_allowed`
(`agents/tools/permissions.py`) matches by exact token prefix —
`args[: len(pattern_parts)] == pattern_parts` — so an absolute
`sys.executable` path does **not** match the allowlist entry `"python"`,
and the tests would flip from `FileNotFoundError` to
`PermissionError: Command not in allowlist`. `_setup_tools` has to grant
`sys.executable` alongside (or instead of) `"python"`. Fixing only the
call sites reddens the suite a second way.

Worth deciding at the same time whether a portability guard belongs in
CI, since CI structurally cannot see this class of bug: a job step that
removes the `python` shim before running the unit suite would have
caught it, at the cost of a job matrix entry. Alternatively a plain
`shutil.which("python") is None` assertion is not the answer — it pins
the environment rather than the behaviour.

## Notes

> 2026-08-15 — captured during PR #829 review. The failures were
> reproduced on clean `main` with the branch stashed, so they predate
> RFC 0040 Phase 1. Root cause corrected here: earlier PR descriptions
> attributed them to a restricted shell in the authoring sandbox; the
> actual error is `Command not found: python`, i.e. a missing binary,
> and the allowlist grants the command fine.
