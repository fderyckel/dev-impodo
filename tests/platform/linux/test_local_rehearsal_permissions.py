"""Verify Linux permission bits created by the local server rehearsal helper."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from deploy.local_rehearsal import prepare_local_secrets


class LocalRehearsalPermissionTests(unittest.TestCase):
    def test_secret_files_have_linux_private_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            secret_directory = Path(temporary_directory) / "secrets"
            created = prepare_local_secrets(secret_directory)

            self.assertEqual(secret_directory.stat().st_mode & 0o777, 0o700)
            for path in created:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
