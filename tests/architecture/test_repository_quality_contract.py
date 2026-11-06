"""Keep the complete repository gate mandatory for CI."""

from __future__ import annotations

import sys
import unittest

import yaml

from scripts.check_repository_quality import CHECKS
from tests.support.paths import REPOSITORY_ROOT


class RepositoryQualityContractTests(unittest.TestCase):
    def test_hosted_workflows_run_only_on_manual_dispatch(self) -> None:
        directory = REPOSITORY_ROOT / ".github" / "workflows"
        workflows = sorted((*directory.glob("*.yml"), *directory.glob("*.yaml")))
        self.assertTrue(workflows)
        for path in workflows:
            with self.subTest(workflow=path.name):
                # BaseLoader keeps the GitHub event key "on" as a string.
                workflow = yaml.load(
                    path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
                )
                self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})

    def test_complete_gate_owns_every_repository_guardrail_family(self) -> None:
        commands = dict(CHECKS)

        self.assertEqual(
            commands["Python quality"],
            (sys.executable, "scripts/check_python_quality.py"),
        )
        self.assertEqual(
            commands["Architecture inventory report"],
            (
                sys.executable,
                "scripts/architecture_inventory.py",
                "--json",
            ),
        )
        self.assertEqual(commands["Architecture tests"][3], "discover")
        self.assertIn("tests/architecture", commands["Architecture tests"])
        self.assertIn("test_*.py", commands["Architecture tests"])
        self.assertIn(
            "scripts/documentation_quality.py",
            commands["Documentation quality"],
        )
        self.assertIn(
            "scripts/code_documentation_inventory.py",
            commands["Code documentation inventory"],
        )

    def test_ci_invokes_the_complete_gate(self) -> None:
        command = "python scripts/check_repository_quality.py"
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "python-quality.yml"
        ).read_text(encoding="utf-8")

        self.assertIn(command, workflow)
        self.assertIn("name: Python quality", workflow)
        self.assertIn("\n  quality:\n", workflow)
        self.assertIn("\n  portable:\n", workflow)
        self.assertIn("needs: [portable, behavior]", workflow)
        self.assertIn("--ignore=tests/platform --ignore=tests/performance", workflow)
        self.assertIn("pytest -q tests/performance", workflow)
        self.assertIn("shard: [1, 2, 3, 4]", workflow)
        self.assertIn("python -m pytest -q tests", workflow)
        self.assertIn("-p scripts.pytest_sharding --shard-count 4 --shard-index ${{ matrix.shard }}", workflow)
        self.assertIn("if: runner.os == 'Linux' && matrix.shard == 1", workflow)
        for runner in ("ubuntu-latest", "macos-latest", "windows-latest"):
            self.assertIn(f"          - {runner}", workflow)
        self.assertNotIn(
            "run: uv run --frozen --group quality python "
            "scripts/check_python_quality.py",
            workflow,
        )

    def test_linux_server_integration_installs_the_test_extra(self) -> None:
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "linux-server-quality.yml"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "uv run --frozen --group quality --extra test python -m unittest",
            workflow,
        )


if __name__ == "__main__":
    unittest.main()
