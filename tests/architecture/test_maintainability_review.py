"""Protect change-aware coupling checks and nonblocking size review."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.maintainability_review import (
    COMPLEXITY_LIMIT,
    FUNCTION_LINE_LIMIT,
    MODULE_LINE_LIMIT,
    coupling_findings,
    growth_advisories,
    repository_review,
    review_base,
    review_sources,
)

PATH = "src/impodo/application/workspace/execution/example.py"


class MaintainabilityReviewTests(unittest.TestCase):
    def test_relative_private_imports_resolve_to_their_public_owner(self) -> None:
        findings = coupling_findings(
            PATH, "from .service import _identity_domain as predicate\n"
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule, "private-import")
        self.assertEqual(
            findings[0].target, "impodo.application.workspace.execution.service"
        )
        self.assertEqual(findings[0].detail, "_identity_domain")

    def test_public_and_conventional_dunder_imports_are_allowed(self) -> None:
        self.assertEqual(
            coupling_findings(
                PATH, "from package import PublicContract, __version__\n"
            ),
            (),
        )

    def test_direct_and_from_private_module_imports_are_rejected(self) -> None:
        for source in (
            "import package._private\n",
            "from package._private import PublicName\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    coupling_findings(PATH, source)[0].rule, "private-import"
                )

    def test_existing_import_remains_visible_but_moving_it_does_not_fail(self) -> None:
        before = "from .service import _private\n"
        after = "\nfrom .service import _private as renamed\n"
        review = review_sources({PATH: before}, {PATH: after})
        self.assertEqual(len(review.findings), 1)
        self.assertEqual(review.introduced, ())

    def test_adding_another_private_symbol_fails_even_in_an_existing_import(
        self,
    ) -> None:
        review = review_sources(
            {PATH: "from .service import _first\n"},
            {PATH: "from .service import _first, _second\n"},
        )
        self.assertEqual([item.detail for item in review.introduced], ["_second"])

    def test_new_modules_do_not_inherit_another_modules_private_import_allowance(
        self,
    ) -> None:
        other = PATH.replace("example.py", "other.py")
        source = "from .service import _private\n"
        review = review_sources({PATH: source}, {PATH: source, other: source})
        self.assertEqual([item.path for item in review.introduced], [other])

    def test_obvious_import_and_declared_method_replacements_are_rejected(self) -> None:
        sources = (
            "import package as api\napi.execute = replacement\n",
            "from package import Runner\nRunner.execute = replacement\n",
            "import package\nsetattr(package, 'execute', replacement)\n",
            "import package\ndelattr(package, 'execute')\n",
            "class Runner:\n def execute(self): pass\nRunner.execute = replacement\n",
            "class Runner:\n def execute(self): pass\n def change(self): self.execute = replacement\n",
        )
        for source in sources:
            with self.subTest(source=source):
                findings = coupling_findings(PATH, source)
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0].rule, "runtime-replacement")

    def test_ordinary_instance_state_callback_and_dataclass_replace_are_allowed(
        self,
    ) -> None:
        source = (
            "from dataclasses import replace\n"
            "class Runner:\n"
            " def __init__(self, callback):\n"
            "  self.callback = callback\n"
            "  self.count = 0\n"
            " def update(self, value):\n"
            "  self.count += 1\n"
            "  return replace(value, count=self.count)\n"
        )
        self.assertEqual(coupling_findings(PATH, source), ())

    def test_production_mock_patch_calls_are_rejected(self) -> None:
        for source in (
            "from unittest.mock import patch\npatch.object(Target, 'run', replacement)\n",
            "import unittest.mock as mock\nmock.patch('package.run', replacement)\n",
        ):
            with self.subTest(source=source):
                findings = coupling_findings(PATH, source)
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0].rule, "runtime-replacement")

    def test_changed_runtime_replacement_value_requires_review_again(self) -> None:
        review = review_sources(
            {PATH: "import package\npackage.execute = first\n"},
            {PATH: "import package\npackage.execute = second\n"},
        )
        self.assertEqual(len(review.introduced), 1)

    def test_module_growth_advice_does_not_become_a_hard_finding(self) -> None:
        before = "# line\n" * MODULE_LINE_LIMIT
        after = before + "# another line\n"
        review = review_sources({PATH: before}, {PATH: after})
        self.assertEqual(review.introduced, ())
        self.assertEqual(len(review.advisories), 1)
        self.assertEqual(review.advisories[0].current, MODULE_LINE_LIMIT + 1)
        self.assertEqual(review.advisories[0].previous, MODULE_LINE_LIMIT)

    def test_growth_in_an_existing_oversized_function_is_reported(self) -> None:
        before = "def lengthy():\n" + "    value = 0\n" * FUNCTION_LINE_LIMIT
        after = before + "    value = 1\n"
        advisories = growth_advisories(PATH, before, after)
        self.assertEqual(len(advisories), 1)
        self.assertEqual(advisories[0].symbol, "lengthy")
        self.assertEqual(advisories[0].metric, "physical lines")

    def test_nested_function_decisions_do_not_inflate_the_outer_function(self) -> None:
        source = (
            "def outer():\n"
            "    def inner(value):\n"
            + "        if value: value -= 1\n" * COMPLEXITY_LIMIT
            + "    return inner\n"
        )
        advisories = growth_advisories(PATH, "", source)
        self.assertEqual(len(advisories), 1)
        self.assertEqual(advisories[0].symbol, "outer.inner")
        self.assertEqual(advisories[0].metric, "cyclomatic complexity")
        self.assertEqual(advisories[0].current, COMPLEXITY_LIMIT + 1)

    def test_reductions_above_a_threshold_are_not_reported_as_growth(self) -> None:
        before = "# line\n" * (MODULE_LINE_LIMIT + 20)
        after = "# line\n" * (MODULE_LINE_LIMIT + 10)
        self.assertEqual(growth_advisories(PATH, before, after), ())


class MaintainabilityGitReviewTests(unittest.TestCase):
    def test_fixed_base_covers_committed_and_untracked_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._git(root, "init", "-q")
            path = root / PATH
            path.parent.mkdir(parents=True)
            path.write_text("VALUE = 1\n", encoding="utf-8")
            self._git(root, "add", PATH)
            self._git(
                root,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "initial",
            )
            with patch.dict(os.environ, {}, clear=True):
                base = review_base(root)
            path.write_text("from .service import _private\n", encoding="utf-8")
            added = path.with_name("added.py")
            added.write_text("from .service import _other\n", encoding="utf-8")
            review = repository_review(root, base)
            self.assertEqual(
                {item.detail for item in review.introduced}, {"_private", "_other"}
            )
            self._git(root, "add", ".")
            self._git(
                root,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "changed",
            )
            self.assertEqual(
                repository_review(root, base).introduced, review.introduced
            )
            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=True):
                self.assertEqual(review_base(root), base)
                self.assertEqual(
                    review_base(root, "HEAD"),
                    self._git(root, "rev-parse", "HEAD").strip(),
                )

    def test_an_unavailable_review_base_fails_instead_of_skipping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._git(root, "init", "-q")
            with self.assertRaises(ValueError):
                review_base(root, "missing-review-commit")

    @staticmethod
    def _git(root: Path, *arguments: str) -> str:
        result = subprocess.run(
            ("git", *arguments), cwd=root, text=True, capture_output=True, check=True
        )
        return result.stdout


if __name__ == "__main__":
    unittest.main()
