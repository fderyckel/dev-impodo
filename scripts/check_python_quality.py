"""Run the pinned, non-mutating Python quality gate for Impodo."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CHECKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Ruff",
        (
            "ruff",
            "check",
            "src/impodo",
            "scripts/check_python_quality.py",
            "scripts/check_repository_quality.py",
        ),
    ),
    (
        "Basedpyright",
        ("basedpyright", "--pythonpath", sys.executable),
    ),
    (
        "Python compilation",
        (sys.executable, "-m", "compileall", "-q", "src/impodo"),
    ),
)


def _run_check(name: str, command: tuple[str, ...]) -> int:
    """Run one quality check from the repository root and return its status."""

    print(f"\n==> {name}: {' '.join(command)}", flush=True)
    try:
        completed = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            check=False,
        )
    except FileNotFoundError:
        print(
            f"Required executable is unavailable: {command[0]}",
            file=sys.stderr,
        )
        return 127
    return completed.returncode


def _git_whitespace_command() -> tuple[str, ...]:
    """Check the working diff locally and the complete change range in CI."""

    if base_ref := os.environ.get("GITHUB_BASE_REF"):
        return ("git", "diff", "--check", f"origin/{base_ref}...HEAD")
    before = os.environ.get("IMPODO_CI_BEFORE")
    if before and set(before) != {"0"}:
        return ("git", "diff", "--check", f"{before}..HEAD")
    return ("git", "diff", "--check")


def main() -> int:
    """Stop at the first failed check so its output remains actionable."""

    checks = (*CHECKS, ("Git whitespace check", _git_whitespace_command()))
    for name, command in checks:
        return_code = _run_check(name, command)
        if return_code:
            print(f"\n{name} failed with exit code {return_code}.", file=sys.stderr)
            return return_code
    print("\nAll Python quality checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
