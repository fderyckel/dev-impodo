"""Exercise the PowerShell release installer on its owning host."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.internal_release import _release_manifest
from tests.support.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT
RELEASE_FILES = (
    "requirements.windows-py312.lock",
    "tests.txt",
    "secret-scan.json",
    "dependency-audit.json",
    "sbom.cdx.json",
    "impodo-0.0.0-py3-none-any.whl",
)


def _powershell_environment() -> dict[str, str]:
    """Drop pwsh's module path before starting Windows PowerShell."""
    return {
        key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"
    }


def _prepare_bundle(temporary: str) -> Path:
    bundle = Path(temporary) / "bundle"
    bundle.mkdir()
    shutil.copy2(
        ROOT / "scripts" / "install-internal-release.ps1",
        bundle / "install-internal-release.ps1",
    )
    for name in RELEASE_FILES:
        (bundle / name).write_text("release evidence\n", encoding="utf-8")
    return bundle


def _write_manifest(bundle: Path, *, release_id: str) -> None:
    with patch("scripts.internal_release.sys.version_info") as version_info:
        version_info.major = 3
        version_info.minor = 12
        version_info.micro = 10
        manifest = _release_manifest(
            release_id=release_id,
            version="0.0.0",
            revision="123456789abcdef",
            artifacts=tuple(bundle.iterdir()),
        )
    (bundle / "release-manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )


class WindowsInternalReleaseInstallerTests(unittest.TestCase):
    def test_accepts_an_explicit_python_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = _prepare_bundle(temporary)
            release_id = "impodo-0.0.0-123456789abc"
            _write_manifest(bundle, release_id=release_id)
            install_root = Path(temporary) / "install"
            (install_root / release_id).mkdir(parents=True)
            python_executable = Path(temporary) / "python312.cmd"
            python_executable.write_text("@echo 3.12 64\n", encoding="utf-8")

            completed = subprocess.run(
                (
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(bundle / "install-internal-release.ps1"),
                    "-BundleDirectory",
                    str(bundle),
                    "-InstallRoot",
                    str(install_root),
                    "-PythonExecutable",
                    str(python_executable),
                ),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_powershell_environment(),
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("The versioned installation already exists", completed.stderr)

    def test_rejects_an_unlisted_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = _prepare_bundle(temporary)
            _write_manifest(bundle, release_id="impodo-0.0.0-123456789abc")
            (bundle / "unexpected.txt").write_text(
                "not governed\n",
                encoding="utf-8",
            )
            install_root = Path(temporary) / "install"

            completed = subprocess.run(
                (
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(bundle / "install-internal-release.ps1"),
                    "-BundleDirectory",
                    str(bundle),
                    "-InstallRoot",
                    str(install_root),
                ),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_powershell_environment(),
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "The bundle contains an unlisted artifact: unexpected.txt",
                completed.stderr,
            )
            self.assertFalse(install_root.exists())


if __name__ == "__main__":
    unittest.main()
