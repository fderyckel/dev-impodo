from __future__ import annotations

import os
from pathlib import Path
import unittest

from impodo.application.shared.artifacts import ArtifactPathTooLongError
from impodo.adapters.artifacts.local_store import WINDOWS_PORTABLE_PATH_LIMIT, _require_portable_windows_path


@unittest.skipUnless(os.name == "nt", "Windows path-budget policy")
class WindowsArtifactPathTests(unittest.TestCase):
    def test_portable_limit_is_enforced_before_filesystem_access(self) -> None:
        supported = Path("C:\\" + "a" * (WINDOWS_PORTABLE_PATH_LIMIT - 3))
        unsupported = Path(str(supported) + "b")

        _require_portable_windows_path(supported)
        with self.assertRaisesRegex(
            ArtifactPathTooLongError,
            r"ARTIFACT_PATH_TOO_LONG path_units=260 portable_limit=259",
        ):
            _require_portable_windows_path(unsupported)


if __name__ == "__main__":
    unittest.main()
