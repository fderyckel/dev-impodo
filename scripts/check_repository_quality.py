"""Run Impodo's complete, fail-closed repository quality gate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CHECKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Python quality",
        (sys.executable, "scripts/check_python_quality.py"),
    ),
    (
        "Architecture inventory report",
        (
            sys.executable,
            "scripts/architecture_inventory.py",
            "--json",
        ),
    ),
    (
        "Architecture tests",
        (
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests/architecture",
            "-t",
            ".",
            "-p",
            "test_*.py",
            "-v",
        ),
    ),
    (
        "Documentation quality",
        (sys.executable, "scripts/documentation_quality.py", "--check"),
    ),
    (
        "Code documentation inventory",
        (sys.executable, "scripts/code_documentation_inventory.py", "--check"),
    ),
)


def _run_check(name: str, command: tuple[str, ...]) -> int:
    """Run one repository guardrail and return its process status."""

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


def main() -> int:
    """Stop delivery at the first failed repository guardrail."""

    for name, command in CHECKS:
        return_code = _run_check(name, command)
        if return_code:
            print(
                f"\n{name} failed with exit code {return_code}.",
                file=sys.stderr,
            )
            return return_code
    print("\nAll repository quality checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
