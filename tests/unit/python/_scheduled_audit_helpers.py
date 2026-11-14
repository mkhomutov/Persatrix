"""Shared readers of `.github/workflows/scheduled-audit.yml` for its two test files.

`test_scheduled_audit.py` holds the workflow's steps and `test_scheduled_audit_issue.py`
the text of the issue it opens. Both read the same steps, the same `if:`
conditions and the same commands, kept here. The `_` prefix keeps pytest from
collecting this module.
"""

from __future__ import annotations

import re
from typing import Any

import yaml
from _test_infra import REPO_ROOT

AUDIT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "scheduled-audit.yml"

# Step id -> the words its command must hold. A step whose id starts with
# "audit-" is an audit; these four are the ones the issue asked for.
AUDIT_TOOLS = {
    "audit-rust": "cargo deny check",
    "audit-go": "govulncheck",
    "audit-python": "pip-audit",
    "audit-web": "npm audit",
}

# Audit -> the steps that install what it runs with. An audit whose set-up
# failed would otherwise run with the runner's own tool, or none.
SETUP_FOR = {
    "audit-rust": ("setup-rust", "setup-cargo-deny"),
    "audit-go": ("go-line", "setup-go"),
    "audit-python": ("setup-python", "setup-uv"),
    "audit-web": ("setup-node",),
}
SETUP_STEPS = sorted({step for steps in SETUP_FOR.values() for step in steps})


def workflow_steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(AUDIT_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert list(jobs) == ["dependency-audit"], (
        "the audit is one job, so one failure issue covers it"
    )
    return list(jobs["dependency-audit"]["steps"])


def audit_steps() -> dict[str, dict[str, Any]]:
    return {s["id"]: s for s in workflow_steps() if str(s.get("id", "")).startswith("audit-")}


def issue_step() -> dict[str, Any]:
    last = workflow_steps()[-1]
    assert str(last.get("uses", "")).startswith("actions/github-script@"), (
        "the last step is the one that opens the failure issue"
    )
    return last


def tool_command(step: dict[str, Any], tool: str) -> str:
    """The one line of a step's script that runs its audit tool, without shell plumbing."""
    lines = [
        ln.strip()
        for ln in str(step["run"]).splitlines()
        if tool in ln and not ln.strip().startswith("echo")
    ]
    assert len(lines) == 1, (
        f"step {step['id']!r} should run {tool!r} on exactly one line, found {lines}"
    )
    return lines[0].split(" || ")[0]


def would_run(step: dict[str, Any], outcomes: dict[str, str], cache_hit: bool) -> bool:
    """Whether a step's `if:` lets it run, given how the steps before it ended.

    Reads the few forms this workflow writes: `!cancelled()`, a step's
    `outcome`, the cache step's `cache-hit` output, `==`, `!=`, `&&`, `||`.
    Anything else raises, so a new form is not read wrongly.
    """
    text = str(step["if"]).strip()
    assert text.startswith("${{") and text.endswith("}}"), text
    expr = text[3:-2].replace("!cancelled()", "True")
    expr = re.sub(r"steps\.([\w-]+)\.outcome", r"outcome[\1]", expr)
    expr = re.sub(r"steps\.([\w-]+)\.outputs\.cache-hit", r"hit[\1]", expr)
    expr = expr.replace("&&", " and ").replace("||", " or ")
    expr = re.sub(r"\[([\w-]+)\]", r"['\1']", expr)
    assert re.fullmatch(r"[\w\s'\[\]()=!-]+", expr) and "steps." not in expr, expr
    hit = {s["id"]: ("true" if cache_hit else "") for s in workflow_steps() if "id" in s}
    return bool(eval(expr, {"__builtins__": {}}, {"outcome": outcomes, "hit": hit}))


def walk(failing: str | None, cache_hit: bool = False) -> dict[str, str]:
    """How every step with an id ends when the step named `failing` fails."""
    outcomes: dict[str, str] = {}
    for step in workflow_steps()[:-1]:
        step_id = step.get("id")
        if step_id is None:
            continue
        runs = "if" not in step or would_run(step, outcomes, cache_hit)
        outcomes[step_id] = ("failure" if step_id == failing else "success") if runs else "skipped"
    return outcomes


def outcome_variables() -> dict[str, str]:
    """Step id -> the variable the report step reads that step's outcome from."""
    variables = {}
    for name, value in issue_step().get("env", {}).items():
        step = re.fullmatch(r"\$\{\{ steps\.([\w-]+)\.outcome \}\}", str(value))
        assert step, f"{name} is not one step's outcome: {value}"
        variables[step.group(1)] = name
    return variables


def local_command(step: dict[str, Any]) -> str:
    """What an operator types to run one audit step from the repository root."""
    tool = AUDIT_TOOLS[step["id"]]
    command = tool_command(step, tool)
    lines = [ln.strip() for ln in str(step["run"]).splitlines() if ln.strip()]
    loops = [ln for ln in lines if ln.startswith("for ")]
    if loops:
        (loop,) = loops
        assert loop.endswith("; do"), loop
        return f"{loop} {command}; done"
    # A step with no loop is its command and nothing else, so nothing the
    # step runs can be missing from what the issue gives.
    assert lines == [command], lines
    where = step.get("working-directory")
    return f"cd {where} && {command}" if where else command
