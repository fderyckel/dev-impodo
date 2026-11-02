"""Verify the real pytest plugin keeps complete, disjoint portable CI coverage."""

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import yaml

from tests.support.paths import REPOSITORY_ROOT


class PortableShardingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="impodo-ci-shards-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for number in range(16):
            (self.root / f"test_{number:02}.py").write_text(
                "import pytest\n"
                "@pytest.mark.parametrize('value', [0, 1])\n"
                "def test_example(value):\n"
                "    assert value >= 0\n",
                encoding="utf-8",
            )

    def collect(self, *arguments):
        environment = dict(os.environ)
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        environment["PYTEST_ADDOPTS"] = ""
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, (str(REPOSITORY_ROOT), environment.get("PYTHONPATH", "")))
        )
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "scripts.pytest_sharding",
                "--collect-only",
                "-q",
                *arguments,
            ],
            cwd=self.root,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    def node_ids(self, *arguments):
        result = self.collect(*arguments)
        self.assertIn(result.returncode, (0, 5), result.stdout + result.stderr)
        return [
            line for line in result.stdout.splitlines() if "::test_example[" in line
        ]

    def test_every_parameterized_case_belongs_to_one_complete_module_shard(self):
        whole = self.node_ids()
        self.assertEqual(len(whole), 32)
        shards = [
            self.node_ids("--shard-count", "4", "--shard-index", str(index))
            for index in range(1, 5)
        ]
        self.assertEqual(
            Counter(node for shard in shards for node in shard), Counter(whole)
        )
        owners = {}
        for index, shard in enumerate(shards):
            for node in shard:
                module = node.split("::", 1)[0]
                self.assertEqual(owners.setdefault(module, index), index)
        self.assertEqual(
            self.node_ids("--shard-count", "1", "--shard-index", "1"), whole
        )

    def test_invalid_allocations_fail_instead_of_silently_omitting_tests(self):
        for count, index in ((0, 1), (-1, 1), (4, 0), (4, 5)):
            with self.subTest(count=count, index=index):
                result = self.collect(
                    "--shard-count", str(count), "--shard-index", str(index)
                )
                self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
                self.assertIn("Require --shard-count", result.stderr)

    def test_workflow_entrypoint_imports_plugin_without_pythonpath(self):
        workflow = yaml.safe_load(
            (REPOSITORY_ROOT / ".github/workflows/python-quality.yml").read_text(
                encoding="utf-8"
            )
        )
        step = next(
            step
            for step in workflow["jobs"]["behavior"]["steps"]
            if step.get("name") == "Run portable behavioral contracts"
        )
        arguments = shlex.split(step["run"].replace("${{ matrix.shard }}", "1"))
        start = next(
            index
            for index, value in enumerate(arguments)
            if value in {"python", "pytest"}
        )
        command = arguments[start:]
        if command[0] == "python":
            command[0] = sys.executable
        else:
            executable = shutil.which("pytest", path=str(Path(sys.executable).parent))
            self.assertIsNotNone(executable)
            command[0] = executable
        command = [str(self.root) if value == "tests" else value for value in command]
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        environment["PYTEST_ADDOPTS"] = ""
        result = subprocess.run(
            [*command, "--collect-only", "--rootdir", str(self.root)],
            cwd=REPOSITORY_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("::test_example[", result.stdout)
