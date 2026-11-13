"""Prove requested preparation limits against fresh native runtime processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from impodo.application.shared.preparation_runtime import (
    POLARS_THREADS_ENV,
    PREPARATION_THREADS_ENV,
    PreparationRuntimeConfigurationError,
    configure_preparation_runtime,
)


class PreparationThreadRuntimeTests(unittest.TestCase):
    def run_child(self, script: str, environment: dict[str, str]) -> dict[str, object]:
        child_environment = dict(os.environ)
        child_environment.pop(POLARS_THREADS_ENV, None)
        child_environment.pop(PREPARATION_THREADS_ENV, None)
        child_environment.update(environment)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            env=child_environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_import_does_not_configure_or_import_polars(self) -> None:
        result = self.run_child(
            "import json, os, sys; "
            "from impodo.application.shared import preparation_runtime; "
            "print(json.dumps({'polars_imported': 'polars' in sys.modules, "
            "'polars_setting': os.environ.get('POLARS_MAX_THREADS')}))",
            {PREPARATION_THREADS_ENV: "4"},
        )
        self.assertEqual(result, {"polars_imported": False, "polars_setting": None})

    def test_default_and_existing_polars_override_are_observed(self) -> None:
        for environment, expected, source in (
            ({}, 1, "default"),
            ({POLARS_THREADS_ENV: "8"}, 8, "inherited_polars"),
        ):
            with self.subTest(environment=environment):
                result = self.run_child(
                    "import json; "
                    "from impodo.application.shared.preparation_runtime import "
                    "configure_preparation_runtime; "
                    "print(json.dumps(configure_preparation_runtime().diagnostic_payload()))",
                    environment,
                )
                self.assertEqual(result["requested_polars_threads"], expected)
                self.assertEqual(result["effective_polars_threads"], expected)
                self.assertEqual(result["thread_configuration_source"], source)

    def test_opt_in_native_pool_counts_after_production_manager_import(self) -> None:
        for threads in (1, 2, 4):
            with self.subTest(threads=threads):
                result = self.run_child(
                    "import json, sys; "
                    "from impodo.web.composition import preparation_job_manager; "
                    "from impodo.application.shared.preparation_runtime import "
                    "configure_preparation_runtime; "
                    "loaded = 'polars' in sys.modules; "
                    "payload = configure_preparation_runtime().diagnostic_payload(); "
                    "payload['polars_loaded_before_configuration'] = loaded; "
                    "print(json.dumps(payload))",
                    {PREPARATION_THREADS_ENV: str(threads), POLARS_THREADS_ENV: "1"},
                )
                self.assertEqual(result["requested_polars_threads"], threads)
                self.assertEqual(result["effective_polars_threads"], threads)
                self.assertEqual(
                    result["thread_configuration_source"], "preparation_override"
                )
                self.assertFalse(result["polars_loaded_before_configuration"])

    def test_native_initialization_mismatch_stops_before_preparation(self) -> None:
        result = self.run_child(
            "import json, polars; "
            "polars.thread_pool_size(); "
            "from impodo.application.shared.preparation_runtime import "
            "configure_preparation_runtime, PreparationRuntimeConfigurationError; "
            "\ntry:\n configure_preparation_runtime()"
            "\nexcept PreparationRuntimeConfigurationError as error:"
            "\n print(json.dumps({'code': error.failure_code}))",
            {PREPARATION_THREADS_ENV: "4", POLARS_THREADS_ENV: "1"},
        )
        self.assertEqual(result["code"], "PREPARATION_THREAD_CONFIGURATION_MISMATCH")

    def test_launcher_import_allows_child_setting_before_native_initialization(
        self,
    ) -> None:
        result = self.run_child(
            "import json; "
            "from impodo.web.launcher import main; "
            "from impodo.application.shared.preparation_runtime import "
            "configure_preparation_runtime; "
            "print(json.dumps(configure_preparation_runtime().diagnostic_payload()))",
            {PREPARATION_THREADS_ENV: "4", POLARS_THREADS_ENV: "1"},
        )
        self.assertEqual(result["effective_polars_threads"], 4)

    def test_invalid_explicit_values_are_rejected_without_native_import(self) -> None:
        for value in ("", "0", "3", "8", "-1", "text", "2.0", " 2 "):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {PREPARATION_THREADS_ENV: value}, clear=True),
            ):
                with self.assertRaises(PreparationRuntimeConfigurationError) as raised:
                    configure_preparation_runtime()
                self.assertEqual(
                    raised.exception.failure_code,
                    "PREPARATION_THREAD_CONFIGURATION_INVALID",
                )

    def test_invalid_existing_polars_values_have_clear_failure(self) -> None:
        for value in ("", "0", "-2", "text"):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {POLARS_THREADS_ENV: value}, clear=True),
                self.assertRaises(PreparationRuntimeConfigurationError),
            ):
                configure_preparation_runtime()

    def test_spawned_child_does_not_change_parent_environment_or_pool(self) -> None:
        script = """\
import json
import multiprocessing
import os
from impodo.web.launcher import main
from impodo.web.composition import preparation_job_manager

def child(queue):
    from impodo.application.shared.preparation_runtime import configure_preparation_runtime
    queue.put(configure_preparation_runtime().diagnostic_payload())

if __name__ == '__main__':
    os.environ['POLARS_MAX_THREADS'] = '1'
    import polars
    before = polars.thread_pool_size()
    context = multiprocessing.get_context('spawn')
    queue = context.Queue()
    process = context.Process(target=child, args=(queue,))
    process.start()
    evidence = queue.get(timeout=20)
    process.join(timeout=20)
    print(json.dumps({'child': evidence, 'parent_before': before,
                     'parent_after': polars.thread_pool_size(),
                     'parent_environment': os.environ['POLARS_MAX_THREADS'],
                     'child_exit': process.exitcode}))
"""
        child_environment = dict(os.environ)
        child_environment[PREPARATION_THREADS_ENV] = "4"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "spawn_preparation.py"
            path.write_text(script, encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(path)],
                env=child_environment,
                capture_output=True,
                text=True,
                timeout=45,
                check=True,
            )
        result = json.loads(completed.stdout)
        self.assertEqual(result["child"]["effective_polars_threads"], 4)
        self.assertEqual(result["parent_before"], 1)
        self.assertEqual(result["parent_after"], 1)
        self.assertEqual(result["parent_environment"], "1")
        self.assertEqual(result["child_exit"], 0)


if __name__ == "__main__":
    unittest.main()
