from __future__ import annotations

import ctypes
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from impodo.adapters.protected_evidence.project_security import (
    ProjectRootSecurityError,
    _apply_private_windows_acl,
    _validate_private_windows_sddl,
    development_mode_enabled,
    prepare_project_root,
)
from impodo.web.launcher import default_project_root


class ProjectRootPolicyTests(unittest.TestCase):
    def test_development_mode_requires_an_explicit_one(self) -> None:
        self.assertTrue(development_mode_enabled({"IMPODO_DEVELOPMENT_MODE": "1"}))
        for value in ("", "0", "true", "yes"):
            with self.subTest(value=value):
                self.assertFalse(
                    development_mode_enabled({"IMPODO_DEVELOPMENT_MODE": value})
                )

    def test_development_mode_keeps_disposable_root_agile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "projects"
            status = prepare_project_root(candidate, development_mode=True)
            self.assertEqual(status.root, candidate)
            self.assertTrue(status.development_mode)
            self.assertEqual(
                status.access_control,
                "development-mode-not-enforced",
            )
            self.assertTrue(candidate.is_dir())

    def test_macos_defaults_to_the_private_application_support_root(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"IMPODO_PROJECT_ROOT": "", "LOCALAPPDATA": ""},
                clear=False,
            ),
            patch("impodo.web.launcher.sys.platform", "darwin"),
            patch(
                "impodo.web.launcher.Path.home",
                return_value=Path("/Users/example"),
            ),
        ):
            root = default_project_root(development_mode=False)

        self.assertEqual(
            root,
            Path("/Users/example/Library/Application Support/Impodo/projects"),
        )


class WindowsProjectRootAclTests(unittest.TestCase):
    def test_private_sddl_accepts_only_expected_inheritable_full_control(self) -> None:
        sid = "S-1-5-21-100-200-300-400"
        descriptor = (
            f"O:{sid}G:SYD:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
        )
        _validate_private_windows_sddl(descriptor, sid)

        for owner in ("BA", "S-1-5-21-100-200-300-401"):
            with self.subTest(owner=owner):
                with self.assertRaisesRegex(ProjectRootSecurityError, "not owned"):
                    _validate_private_windows_sddl(
                        descriptor.replace(f"O:{sid}", f"O:{owner}"), sid
                    )

        with self.assertRaisesRegex(ProjectRootSecurityError, "outside"):
            _validate_private_windows_sddl(
                descriptor + "(A;OICI;FA;;;WD)",
                sid,
            )

    def test_private_acl_sets_the_current_user_as_owner(self) -> None:
        sid = "S-1-5-21-100-200-300-400"
        advapi = Mock()
        kernel = Mock()

        def convert(_text, _revision, descriptor, _size):
            ctypes.cast(descriptor, ctypes.POINTER(ctypes.c_void_p))[0] = 100
            return 1

        def read_owner(_descriptor, owner, _defaulted):
            ctypes.cast(owner, ctypes.POINTER(ctypes.c_void_p))[0] = 200
            return 1

        def read_dacl(_descriptor, present, dacl, _defaulted):
            ctypes.cast(present, ctypes.POINTER(ctypes.c_int))[0] = 1
            ctypes.cast(dacl, ctypes.POINTER(ctypes.c_void_p))[0] = 300
            return 1

        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.side_effect = (
            convert
        )
        advapi.GetSecurityDescriptorOwner.side_effect = read_owner
        advapi.GetSecurityDescriptorDacl.side_effect = read_dacl
        advapi.SetNamedSecurityInfoW.return_value = 0
        with (
            patch(
                "impodo.adapters.protected_evidence.project_security._current_windows_user_sid",
                return_value=sid,
            ),
            patch.object(ctypes, "WinDLL", side_effect=[advapi, kernel], create=True),
        ):
            _apply_private_windows_acl(Path(tempfile.gettempdir()) / "protected-root")

        descriptor_text = (
            advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.call_args.args[
                0
            ]
        )
        self.assertTrue(descriptor_text.startswith(f"O:{sid}D:P"))
        args = advapi.SetNamedSecurityInfoW.call_args.args
        self.assertEqual(args[2], 0x00000001 | 0x00000004 | 0x80000000)
        self.assertEqual(args[3].value, 200)
        self.assertEqual(args[5].value, 300)
        kernel.LocalFree.assert_called_once()

    def test_unreadable_or_missing_owner_blocks_acl_publication(self) -> None:
        for result in (0, 1):
            with self.subTest(owner_read_result=result):
                advapi = Mock()
                kernel = Mock()
                advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.return_value = 1
                advapi.GetSecurityDescriptorOwner.return_value = result
                with (
                    patch(
                        "impodo.adapters.protected_evidence.project_security._current_windows_user_sid",
                        return_value="S-1-5-21-100-200-300-400",
                    ),
                    patch.object(
                        ctypes, "WinDLL", side_effect=[advapi, kernel], create=True
                    ),
                    patch(
                        "impodo.adapters.protected_evidence.project_security._windows_security_error",
                        return_value=ProjectRootSecurityError("owner unavailable"),
                    ),
                ):
                    with self.assertRaisesRegex(
                        ProjectRootSecurityError, "owner unavailable"
                    ):
                        _apply_private_windows_acl(
                            Path(tempfile.gettempdir()) / "protected-root"
                        )
                advapi.SetNamedSecurityInfoW.assert_not_called()
                kernel.LocalFree.assert_called_once()
