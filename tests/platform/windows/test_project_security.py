"""Exercise native Windows project-root ownership and location policy."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from impodo.adapters.protected_evidence.project_security import (
    ProjectRootSecurityError,
    _apply_private_windows_acl,
    _current_windows_user_sid,
    _validate_windows_location,
    _verify_private_windows_acl,
    prepare_project_root,
)
from impodo.web.launcher import default_project_root
from tests.support.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


@unittest.skipUnless(os.name == "nt", "Windows project-root policy")
class WindowsProjectRootTests(unittest.TestCase):
    def test_windows_project_root_override_requires_development_mode(self) -> None:
        with patch.dict(
            os.environ,
            {
                "IMPODO_PROJECT_ROOT": str(ROOT / "var" / "projects"),
                "IMPODO_DEVELOPMENT_MODE": "0",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(
                ProjectRootSecurityError,
                "available only",
            ):
                default_project_root(development_mode=False)

    def test_normal_windows_mode_rejects_a_git_checkout(self) -> None:
        with self.assertRaisesRegex(ProjectRootSecurityError, "Git checkout"):
            prepare_project_root(ROOT / "var" / "internal-projects")

    def test_normal_windows_mode_rejects_a_removable_drive(self) -> None:
        with (
            patch(
                "impodo.adapters.protected_evidence.project_security._reject_reparse_points"
            ),
            patch(
                "impodo.adapters.protected_evidence.project_security._windows_drive_type",
                return_value=2,
            ),
        ):
            with self.assertRaisesRegex(ProjectRootSecurityError, "fixed local"):
                _validate_windows_location(Path(r"X:\Impodo\projects"), {})

    def test_private_dacl_is_applied_and_verified_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "projects"
            candidate.mkdir()
            self.assertRegex(_current_windows_user_sid(), r"^S-\d(?:-\d+)+$")
            _apply_private_windows_acl(candidate)
            _verify_private_windows_acl(candidate)
