from __future__ import annotations

import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.protected_evidence.project_security import (
    ProjectRootSecurityError,
    _apply_private_windows_acl,
    _current_windows_user_sid,
    _validate_windows_location,
    _validate_private_windows_sddl,
    _verify_private_windows_acl,
    development_mode_enabled,
    prepare_project_root,
)
from impodo.web.launcher import default_project_root


ROOT = Path(__file__).resolve().parents[1]


class ProjectRootPolicyTests(unittest.TestCase):
    def test_development_mode_requires_an_explicit_one(self) -> None:
        self.assertTrue(
            development_mode_enabled({"IMPODO_DEVELOPMENT_MODE": "1"})
        )
        for value in ("", "0", "true", "yes"):
            with self.subTest(value=value):
                self.assertFalse(
                    development_mode_enabled({"IMPODO_DEVELOPMENT_MODE": value})
                )

    @unittest.skipUnless(os.name == "nt", "Windows launcher policy")
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

    def test_development_mode_keeps_disposable_root_agile(self) -> None:
        candidate = ROOT / ".tmp" / f"project-security-dev-{uuid4()}"
        try:
            status = prepare_project_root(candidate, development_mode=True)
            self.assertEqual(status.root, candidate)
            self.assertTrue(status.development_mode)
            self.assertEqual(
                status.access_control,
                "development-mode-not-enforced",
            )
            self.assertTrue(candidate.is_dir())
        finally:
            if candidate.exists():
                shutil.rmtree(candidate)

    @unittest.skipUnless(os.name == "nt", "Windows project-root policy")
    def test_normal_windows_mode_rejects_a_git_checkout(self) -> None:
        with self.assertRaisesRegex(ProjectRootSecurityError, "Git checkout"):
            prepare_project_root(ROOT / "var" / "internal-projects")

    @unittest.skipUnless(os.name == "nt", "Windows project-root policy")
    def test_normal_windows_mode_rejects_a_removable_drive(self) -> None:
        with (
            patch("impodo.adapters.protected_evidence.project_security._reject_reparse_points"),
            patch("impodo.adapters.protected_evidence.project_security._windows_drive_type", return_value=2),
        ):
            with self.assertRaisesRegex(ProjectRootSecurityError, "fixed local"):
                _validate_windows_location(Path(r"X:\Impodo\projects"), {})


@unittest.skipUnless(os.name == "nt", "Windows DACL verification")
class WindowsProjectRootAclTests(unittest.TestCase):
    def test_private_sddl_accepts_only_expected_inheritable_full_control(self) -> None:
        sid = "S-1-5-21-100-200-300-400"
        descriptor = (
            f"O:{sid}G:SYD:P"
            f"(A;OICI;FA;;;{sid})"
            "(A;OICI;FA;;;SY)"
            "(A;OICI;FA;;;BA)"
        )
        _validate_private_windows_sddl(descriptor, sid)

        with self.assertRaisesRegex(ProjectRootSecurityError, "outside"):
            _validate_private_windows_sddl(
                descriptor + "(A;OICI;FA;;;WD)",
                sid,
            )

    def test_private_dacl_is_applied_and_verified_on_disk(self) -> None:
        candidate = ROOT / ".tmp" / f"project-security-acl-{uuid4()}"
        candidate.mkdir(parents=True)
        try:
            self.assertRegex(_current_windows_user_sid(), r"^S-\d(?:-\d+)+$")
            _apply_private_windows_acl(candidate)
            _verify_private_windows_acl(candidate)
        finally:
            if candidate.exists():
                shutil.rmtree(candidate)
