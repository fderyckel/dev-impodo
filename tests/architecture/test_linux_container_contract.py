"""Protect the single-image, fail-closed Linux deployment baseline."""

from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
import unittest

from impodo.web.composition.server_cli import (
    CONFIGURATION_ERROR_EXIT,
    main,
)
from tests.support.paths import REPOSITORY_ROOT


ROOT = REPOSITORY_ROOT
DOCKERFILE = ROOT / "deploy" / "Dockerfile"


class LinuxContainerContractTests(unittest.TestCase):
    def test_image_is_pinned_and_installed_from_the_repository_lock(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")

        self.assertRegex(
            dockerfile,
            r"ARG PYTHON_IMAGE=python:3\.12\.\d+-slim-bookworm@sha256:[0-9a-f]{64}",
        )
        self.assertEqual(dockerfile.count("FROM ${PYTHON_IMAGE}"), 2)
        self.assertIn(
            "uv sync --frozen --no-dev --no-editable --no-cache",
            dockerfile,
        )

    def test_runtime_is_non_root_with_only_named_writable_paths(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")

        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn("RUN chmod -R a-w /opt/impodo", dockerfile)
        for path in (
            "/var/lib/impodo/artifacts",
            "/var/lib/impodo/runtime",
            "/var/tmp/impodo",
        ):
            self.assertIn(path, dockerfile)

    def test_build_context_is_an_explicit_package_allowlist(self) -> None:
        entries = tuple(
            line.strip()
            for line in (ROOT / ".dockerignore")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        )

        self.assertEqual(entries[0], "**")
        self.assertEqual(
            set(entries[1:]),
            {
                "!pyproject.toml",
                "!uv.lock",
                "!README.md",
                "!LICENSE",
                "!src/",
                "!src/**",
                "!deploy/",
                "!deploy/Dockerfile",
            },
        )

    def test_one_entry_point_exposes_explicit_fail_closed_roles(self) -> None:
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")

        self.assertIn(
            'impodo-server = "impodo.web.composition.server_cli:main"',
            project,
        )
        self.assertIn('ENTRYPOINT ["impodo-server"]', dockerfile)
        self.assertIn('CMD ["web"]', dockerfile)
        self.assertNotIn("impodo.web.launcher", dockerfile)

        for command in ("web", "worker", "migrate", "admin"):
            with self.subTest(command=command):
                error = StringIO()
                with redirect_stderr(error):
                    result = main((command,))
                self.assertEqual(result, CONFIGURATION_ERROR_EXIT)
                self.assertIn(f"{command} startup refused", error.getvalue())


if __name__ == "__main__":
    unittest.main()
