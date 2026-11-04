"""
``shell_exec`` under RFC 0009 Phase 3's limits, and the environment its
commands get (the Phases 3–4 plan's PR 4: design points G-3 and G-14).

A CPU limit (``resource_limits.shell_cpu_seconds``) is set in the child
process before the command starts, so the kernel stops a command that spins
however its wall-time limit is set. It holds for each process the command
starts, not for all of them together. With none set, a command runs as
before. A limit an operator set that stops a command, CPU or wall time,
writes a ``tool.resource_limited`` audit line.

A command never sees the fleet enrollment secret: every ``SECURITY_*``
variable is taken out of its environment, so a prompt-injected ``env`` or
``printenv`` cannot read the secret. The agent token is held in memory only
(PR 7), and nothing puts it in the environment either.
"""

from __future__ import annotations

import importlib
import json
import logging
import shlex
import signal
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.orchestrator_session import ENROLLMENT_SECRET_ENV
from agents.server_reregister import AgentTokenKeeper
from agents.tools import builtin
from agents.tools.permissions import PermissionGate
from agents.tools.registry import clear_registry
from agents.tools.sandbox import (
    SCRUBBED_ENV_PREFIX,
    PathValidator,
    ResourceLimiter,
    cpu_rlimit,
    tool_process_env,
)

# ISSUE-0129: allowlist the interpreter running the tests by its absolute path.
_PY = sys.executable
_PY_CMD = shlex.quote(_PY)
# Spends CPU time until something stops it.
_SPIN = f'{_PY_CMD} -c "while True: pass"'
# Spends about 1.5 s of CPU time, then exits 0.
_BURN = f'{_PY_CMD} -c "import time; [0 for _ in iter(lambda: time.process_time() < 1.5, False)]"'
# Prints the command's whole environment as JSON.
_ENV = f'{_PY_CMD} -c "import os, json; print(json.dumps(dict(os.environ)))"'
# Prints the soft limit on the size of a core file the command may write.
_CORE = f'{_PY_CMD} -c "import resource; print(resource.getrlimit(resource.RLIMIT_CORE)[0])"'
_SLEEP = f'{_PY_CMD} -c "import time; time.sleep(30)"'

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="RLIMIT_CPU is POSIX-only (RFC 0009 Security Considerations 4)",
)


@pytest.fixture(autouse=True)
def _clean_state():
    """Reload the built-in tools so each test starts with no limit and no gate."""
    clear_registry()
    importlib.reload(builtin)
    yield
    builtin.permission_gate = None
    builtin.path_validator = None
    builtin.workspace_root = None
    builtin.resource_limiter = None
    clear_registry()


def _setup(
    tmp_path: Path, *, cpu_seconds: int | None = None, allow: list[str] | None = None,
    max_execution_seconds: int | None = None,
) -> None:
    shell: dict[str, object] = {"allowed_commands": allow or [_PY]}
    if max_execution_seconds is not None:
        shell["max_execution_seconds"] = max_execution_seconds
    builtin.permission_gate = PermissionGate({"shell": shell})
    builtin.path_validator = PathValidator()
    builtin.workspace_root = tmp_path
    builtin.resource_limiter = ResourceLimiter(cpu_seconds)


def _audit_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    [record] = [r for r in caplog.records if r.name == "agents.tools.audit"]
    return record


# ─── The CPU limit ────────────────────────────────────────────


class TestCpuLimit:
    @posix_only
    async def test_a_cpu_limit_ends_a_cpu_bound_command(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        _setup(tmp_path, cpu_seconds=1)
        with caplog.at_level(logging.INFO, logger="agents.tools.audit"):
            result = await builtin.shell_exec(_SPIN, timeout=30)
        assert result.success is False
        assert result.error_type == "ResourceLimitError"
        assert result.error == "Command stopped at its CPU limit of 1 s"
        assert result.data["exit_code"] == -signal.SIGXCPU
        record = _audit_record(caplog)
        assert record.getMessage() == "tool.resource_limited"
        assert record.tool_name == "shell_exec"  # type: ignore[attr-defined]
        assert record.limit == "shell_cpu_seconds"  # type: ignore[attr-defined]
        assert record.cpu_seconds == 1  # type: ignore[attr-defined]

    async def test_with_no_cpu_limit_the_same_kind_of_command_runs_as_today(self, tmp_path: Path):
        _setup(tmp_path, cpu_seconds=None)
        result = await builtin.shell_exec(_BURN, timeout=30)
        assert result.success is True, result.error

    @posix_only
    async def test_a_command_under_its_cpu_limit_runs(self, tmp_path: Path):
        _setup(tmp_path, cpu_seconds=5)
        result = await builtin.shell_exec(f'{_PY_CMD} -c "print(1)"')
        assert result.success is True
        assert result.data["stdout"].strip() == "1"

    @posix_only
    async def test_a_command_under_a_cpu_limit_writes_no_core_file(self, tmp_path: Path):
        """Review F-3: SIGXCPU dumps core by default, into the agent's workspace.

        The agent's own core limit is raised first, as a container's may be,
        so the command would inherit a non-zero one but for the limit.
        """
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
        if hard == 0:
            pytest.skip("this process may not raise its core limit")
        raised = 1 << 20 if hard == resource.RLIM_INFINITY else hard
        resource.setrlimit(resource.RLIMIT_CORE, (raised, hard))
        try:
            _setup(tmp_path, cpu_seconds=5)
            result = await builtin.shell_exec(_CORE)
            _setup(tmp_path, cpu_seconds=None)
            unlimited = await builtin.shell_exec(_CORE)
        finally:
            resource.setrlimit(resource.RLIMIT_CORE, (soft, hard))
        assert result.success is True, result.error
        assert result.data["stdout"].strip() == "0"
        # Without a CPU limit nothing is set in the child: it inherits as before.
        assert unlimited.data["stdout"].strip() == str(raised)

    @posix_only
    async def test_a_missing_command_under_a_cpu_limit_reads_as_today(self, tmp_path: Path):
        _setup(tmp_path, cpu_seconds=5, allow=["no-such-command-persatrix"])
        result = await builtin.shell_exec("no-such-command-persatrix")
        assert result.success is False
        assert result.error == "Command not found: no-such-command-persatrix"


class TestResourceLimiter:
    def test_no_limit_needs_nothing_in_the_child(self):
        assert ResourceLimiter().child_setup() is None
        assert ResourceLimiter(None).cpu_seconds is None

    @pytest.mark.parametrize("seconds", [0, -1])
    def test_a_limit_under_one_second_is_refused(self, seconds: int):
        with pytest.raises(ValueError):
            ResourceLimiter(seconds)

    @posix_only
    def test_a_limit_sets_something_in_the_child(self):
        assert callable(ResourceLimiter(10).child_setup())

    @posix_only
    @pytest.mark.parametrize(
        ("seconds", "current_hard", "expected"),
        [
            # SIGXCPU at the limit, and SIGKILL a second later if it is ignored.
            (10, None, (10, 11)),
            # A process may not raise its hard limit: a lower one stands, and
            # the soft limit sits a second under it, so the command still gets
            # SIGXCPU first (review F-2: Linux checks the hard limit first).
            (10, 5, (4, 5)),
            (10, 10, (9, 10)),
            (10, 11, (10, 11)),
            # No room for a second under a one-second hard limit.
            (10, 1, (1, 1)),
        ],
    )
    def test_the_child_s_cpu_rlimit(self, seconds, current_hard, expected):
        import resource

        hard = resource.RLIM_INFINITY if current_hard is None else current_hard
        assert cpu_rlimit(seconds, hard) == expected

    def test_only_sigxcpu_with_a_limit_set_reads_as_the_cpu_limit(self):
        limited = ResourceLimiter(1)
        if sys.platform != "win32":
            assert limited.stopped_by_cpu_limit(-signal.SIGXCPU) is True
            assert ResourceLimiter(None).stopped_by_cpu_limit(-signal.SIGXCPU) is False
        assert limited.stopped_by_cpu_limit(-9) is False
        assert limited.stopped_by_cpu_limit(0) is False
        assert limited.stopped_by_cpu_limit(None) is False


# ─── The wall-time limit ──────────────────────────────────────


class TestWallTimeLimit:
    async def test_a_command_stopped_at_the_agent_s_time_limit_writes_an_audit_line(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ):
        _setup(tmp_path, max_execution_seconds=1)
        with caplog.at_level(logging.INFO, logger="agents.tools.audit"):
            result = await builtin.shell_exec(_SLEEP, timeout=30)
        assert result.success is False
        assert result.error_type == "TimeoutError"
        record = _audit_record(caplog)
        assert record.getMessage() == "tool.resource_limited"
        assert record.limit == "max_execution_seconds"  # type: ignore[attr-defined]
        assert record.wall_seconds == 1  # type: ignore[attr-defined]

    @pytest.mark.parametrize("max_execution_seconds", [None, 60])
    async def test_a_timeout_the_model_chose_writes_none(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, max_execution_seconds,
    ):
        """Review F-7: a timeout under the agent's limit, or with none set,
        is the model's choice, not a limit an operator set."""
        _setup(tmp_path, max_execution_seconds=max_execution_seconds)
        with caplog.at_level(logging.INFO, logger="agents.tools.audit"):
            result = await builtin.shell_exec(_SLEEP, timeout=1)
        assert result.error_type == "TimeoutError"
        assert [r for r in caplog.records if r.name == "agents.tools.audit"] == []


# ─── The environment a command gets ───────────────────────────


class TestToolProcessEnvironment:
    def test_the_scrub_covers_the_enrollment_secret_s_name(self):
        assert ENROLLMENT_SECRET_ENV.startswith(SCRUBBED_ENV_PREFIX)

    def test_every_security_variable_goes_and_everything_else_stays(self):
        environ = {
            "PATH": "/usr/bin", "HOME": "/home/agent",
            "SECURITY_ENROLLMENT_SECRET": "s", "SECURITY_UNQUARANTINE_TOKEN": "t",
            "security_lower_case": "u",
        }
        assert tool_process_env(environ) == {"PATH": "/usr/bin", "HOME": "/home/agent"}

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX environment names")
    async def test_the_enrollment_secret_never_reaches_a_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv(ENROLLMENT_SECRET_ENV, "fleet-secret-value")
        monkeypatch.setenv("SECURITY_UNQUARANTINE_TOKEN", "operator-token-value")
        monkeypatch.setenv("PERSATRIX_PROBE", "kept")
        _setup(tmp_path)
        result = await builtin.shell_exec(_ENV)
        assert result.success is True, result.error
        env = json.loads(result.data["stdout"])
        assert ENROLLMENT_SECRET_ENV not in env
        assert "SECURITY_UNQUARANTINE_TOKEN" not in env
        assert env["PERSATRIX_PROBE"] == "kept"
        assert "fleet-secret-value" not in result.data["stdout"]

    async def test_the_agent_token_never_reaches_a_command(self, tmp_path: Path):
        keeper = AgentTokenKeeper(AsyncMock(return_value=True))
        answer = MagicMock(status=200)
        answer.json = AsyncMock(return_value={"agent_token": "agent-token-value"})
        await keeper.keep_from(answer)
        assert keeper.token == "agent-token-value"
        _setup(tmp_path)
        result = await builtin.shell_exec(_ENV)
        assert result.success is True, result.error
        assert "agent-token-value" not in result.data["stdout"]
