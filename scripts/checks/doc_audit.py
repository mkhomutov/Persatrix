#!/usr/bin/env python3
"""Unified documentation audit runner.

Single entry point that runs all doc quality checks (links, status markers)
and produces a consolidated report.

Usage::

    python scripts/checks/doc_audit.py [--format=text|json|markdown] [--verbose]

Exit code: 0 if all checks pass, 1 if any fail.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.checks import ensure_utf8_stdout  # noqa: E402
from scripts.checks.doc_links import check_doc_links  # noqa: E402
from scripts.checks.doc_status_markers import check_status_markers  # noqa: E402
from scripts.checks.file_size import get_warnings as get_file_size_warnings  # noqa: E402


@dataclass
class AuditViolation:
    file: str
    detail: str
    reason: str


@dataclass
class CheckResult:
    name: str
    passed: bool
    violation_count: int
    warning_count: int = 0
    violations: list[AuditViolation] = field(default_factory=list)
    warnings: list[AuditViolation] = field(default_factory=list)
    elapsed_secs: float = 0.0


@dataclass
class AuditReport:
    all_passed: bool
    total_violations: int
    total_warnings: int
    checks: list[CheckResult] = field(default_factory=list)
    elapsed_secs: float = 0.0


def _safe_run(fn: Callable[..., CheckResult], name: str, verbose: bool = False) -> CheckResult:
    try:
        return fn(verbose=verbose)
    except Exception as exc:
        return CheckResult(
            name=name,
            passed=False,
            violation_count=1,
            violations=[
                AuditViolation(
                    file="<runner>", detail=str(exc),
                    reason="Check raised an unexpected exception",
                ),
            ],
        )


def _run_doc_links(verbose: bool = False) -> CheckResult:
    buf = io.StringIO()
    t0 = time.monotonic()
    with redirect_stdout(buf):
        failures = check_doc_links(REPO_ROOT, verbose=verbose)
    elapsed = time.monotonic() - t0

    violations = [
        AuditViolation(file=f.file, detail=f.link, reason=f.reason)
        for f in failures
    ]
    return CheckResult(
        name="doc-links",
        passed=len(failures) == 0,
        violation_count=len(failures),
        violations=violations,
        elapsed_secs=round(elapsed, 2),
    )


def _run_doc_status_markers(verbose: bool = False) -> CheckResult:
    buf = io.StringIO()
    t0 = time.monotonic()
    with redirect_stdout(buf):
        failures, warns = check_status_markers(REPO_ROOT, verbose=verbose)
    elapsed = time.monotonic() - t0

    violations = [
        AuditViolation(file=f.file, detail=f.marker, reason=f.reason)
        for f in failures
    ]
    warnings = [
        AuditViolation(file=w.file, detail=w.marker, reason=w.reason)
        for w in warns
    ]
    return CheckResult(
        name="doc-status-markers",
        passed=len(failures) == 0,
        violation_count=len(failures),
        warning_count=len(warns),
        violations=violations,
        warnings=warnings,
        elapsed_secs=round(elapsed, 2),
    )


def _run_file_size(verbose: bool = False) -> CheckResult:
    t0 = time.monotonic()
    warnings = get_file_size_warnings(REPO_ROOT)
    elapsed = time.monotonic() - t0

    warn_violations = [
        AuditViolation(
            file=w.file,
            detail=f"{w.measured} {w.unit} (limit: {w.limit})",
            reason=f"{w.kind} file exceeds size limit",
        )
        for w in warnings
    ]
    return CheckResult(
        name="file-size",
        passed=True,  # file size is advisory, not blocking
        violation_count=0,
        warning_count=len(warnings),
        warnings=warn_violations,
        elapsed_secs=round(elapsed, 2),
    )


def run_audit(
    output_format: str = "text",
    verbose: bool = False,
) -> AuditReport:
    """Run all doc checks and return a consolidated report."""
    t0 = time.monotonic()

    checks = [
        _safe_run(_run_doc_links, "doc-links", verbose=verbose),
        _safe_run(_run_doc_status_markers, "doc-status-markers", verbose=verbose),
        _safe_run(_run_file_size, "file-size", verbose=verbose),
    ]

    total_violations = sum(c.violation_count for c in checks)
    total_warnings = sum(c.warning_count for c in checks)
    all_passed = all(c.passed for c in checks)
    elapsed = time.monotonic() - t0

    return AuditReport(
        all_passed=all_passed,
        total_violations=total_violations,
        total_warnings=total_warnings,
        checks=checks,
        elapsed_secs=round(elapsed, 2),
    )


def _print_text_report(report: AuditReport) -> None:
    print("=" * 60)
    print("Documentation Audit Report")
    print("=" * 60)

    for check in report.checks:
        status = "\u2705" if check.passed else "\u274c"
        warn_str = f" ({check.warning_count} warnings)" if check.warning_count else ""
        print(
            f"\n{status} {check.name}: {check.violation_count} violations{warn_str}"
            f" [{check.elapsed_secs}s]"
        )

        for v in check.violations:
            print(f"    [FAIL] {v.file}: {v.detail}")
            print(f"           {v.reason}")

        for w in check.warnings:
            print(f"    [WARN] {w.file}: {w.detail}")
            print(f"           {w.reason}")

    print("\n" + "=" * 60)
    status = "\u2705 ALL PASSED" if report.all_passed else "\u274c FAILURES DETECTED"
    print(
        f"{status} | {report.total_violations} violations"
        f" | {report.total_warnings} warnings | {report.elapsed_secs}s"
    )
    print("=" * 60)


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()

    parser = argparse.ArgumentParser(description="Run documentation audit checks.")
    parser.add_argument("--format", choices=["text", "json", "markdown"], default="text")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    report = run_audit(output_format=args.format, verbose=args.verbose)

    if args.format == "json":
        print(json.dumps(asdict(report), indent=2))
    else:
        _print_text_report(report)

    return 0 if report.all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
