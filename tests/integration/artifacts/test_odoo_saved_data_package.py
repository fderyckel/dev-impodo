"""Artifact integration tests for protected portable saved Odoo packages."""

from __future__ import annotations

import hashlib
import json
import stat
import tempfile
import unittest
import warnings
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from impodo.adapters import odoo_saved_data_package as packages
from impodo.adapters.odoo_saved_data_package import (
    SavedOdooPackageError,
    read_saved_odoo_package,
    write_saved_odoo_package,
)

PASSWORD = "fictional-package-password"


class SavedOdooPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_round_trip_reads_streamed_artifacts_without_original_files(self) -> None:
        source = self.root / "original.parquet"
        source.write_bytes(b"fictional account rows\0" * 50_000)
        expected = source.read_bytes()
        package = self.root / "saved.impodo"
        manifest = write_saved_odoo_package(
            package,
            {
                "payload/account.parquet": source,
                "metadata/source.json": b'{"database":"fictional-dev"}',
            },
            password=PASSWORD,
        )
        source.unlink()
        encrypted = package.read_bytes()
        self.assertNotIn(b"fictional account rows", encrypted)
        self.assertNotIn(b"fictional-dev", encrypted)
        self.assertNotIn(b"metadata/source.json", encrypted)
        self.assertNotIn(PASSWORD.encode(), encrypted)
        output = self.root / "new-installation"
        imported = read_saved_odoo_package(package, output, password=PASSWORD)
        self.assertEqual(imported.manifest, manifest)
        self.assertEqual(
            imported.path("payload/account.parquet").read_bytes(), expected
        )
        self.assertEqual(
            imported.path("metadata/source.json").read_bytes(),
            b'{"database":"fictional-dev"}',
        )
        self.assertEqual(len(list(output.rglob("*.*"))), 2)
        with self.assertRaises(SavedOdooPackageError):
            imported.path("payload/absent.parquet")

    def test_version_metadata_is_preserved_for_both_supported_odoo_majors(self) -> None:
        for major in (19, 20):
            with self.subTest(major=major):
                metadata = json.dumps({"odoo_major_version": major}).encode()
                package = self.root / f"odoo-{major}.impodo"
                write_saved_odoo_package(
                    package, {"metadata/schema.json": metadata}, password=PASSWORD
                )
                imported = read_saved_odoo_package(
                    package, self.root / f"import-{major}", password=PASSWORD
                )
                self.assertEqual(
                    imported.path("metadata/schema.json").read_bytes(), metadata
                )

    def test_wrong_password_and_changed_ciphertext_publish_nothing(self) -> None:
        package = self.root / "saved.impodo"
        write_saved_odoo_package(
            package, {"payload/data.parquet": b"clean rows"}, password=PASSWORD
        )
        output = self.root / "wrong-password"
        with self.assertRaisesRegex(SavedOdooPackageError, "password.*incorrect"):
            read_saved_odoo_package(package, output, password="different-password")
        self.assertFalse(output.exists())
        original = package.read_bytes()
        for offset in (15, 45, len(original) - 1):
            with self.subTest(offset=offset):
                changed = bytearray(original)
                changed[offset] ^= 0x40
                package.write_bytes(changed)
                output = self.root / f"tampered-{offset}"
                with self.assertRaises(SavedOdooPackageError):
                    read_saved_odoo_package(package, output, password=PASSWORD)
                self.assertFalse(output.exists())
        self.assertFalse(any(self.root.glob(".impodo-saved-import-*")))

    def test_existing_destinations_are_preserved(self) -> None:
        package = self.root / "existing.impodo"
        package.write_bytes(b"existing package")
        with self.assertRaisesRegex(
            SavedOdooPackageError, "new saved data destination"
        ):
            write_saved_odoo_package(
                package, {"payload/data": b"replacement"}, password=PASSWORD
            )
        self.assertEqual(package.read_bytes(), b"existing package")
        output = self.root / "existing-import"
        output.mkdir()
        sentinel = output / "keep.txt"
        sentinel.write_text("keep")
        with self.assertRaises(SavedOdooPackageError):
            read_saved_odoo_package(package, output, password=PASSWORD)
        self.assertEqual(sentinel.read_text(), "keep")

    def test_export_rejects_nonportable_paths_and_case_collisions(self) -> None:
        bad_names = (
            "../outside",
            "/payload/data",
            "payload/../outside",
            "payload\\data",
            "payload/CON.txt",
            "payload/file.",
            "payload/file:stream",
            "payload//data",
            "metadata/impodo-package.json",
        )
        for number, name in enumerate(bad_names):
            with self.subTest(name=name), self.assertRaises(SavedOdooPackageError):
                write_saved_odoo_package(
                    self.root / f"bad-{number}.impodo",
                    {name: b"data"},
                    password=PASSWORD,
                )
        with self.assertRaises(SavedOdooPackageError):
            write_saved_odoo_package(
                self.root / "collision.impodo",
                {"payload/Data": b"one", "payload/data": b"two"},
                password=PASSWORD,
            )
        self.assertFalse(list(self.root.glob("*.impodo")))

    def test_limits_reject_complete_export_and_import_before_publication(self) -> None:
        package = self.root / "too-large.impodo"
        with (
            patch.object(packages, "SAVED_ODOO_PACKAGE_MAX_MEMBER_BYTES", 4),
            self.assertRaises(SavedOdooPackageError),
        ):
            write_saved_odoo_package(
                package, {"payload/data": b"12345"}, password=PASSWORD
            )
        self.assertFalse(package.exists())
        with (
            patch.object(packages, "SAVED_ODOO_PACKAGE_MAX_TOTAL_BYTES", 5),
            self.assertRaises(SavedOdooPackageError),
        ):
            write_saved_odoo_package(
                package,
                {"payload/first": b"123", "payload/second": b"456"},
                password=PASSWORD,
            )
        self.assertFalse(package.exists())
        write_saved_odoo_package(
            package, {"payload/data": b"123456"}, password=PASSWORD
        )
        output = self.root / "oversize-import"
        with (
            patch.object(packages, "SAVED_ODOO_PACKAGE_MAX_TOTAL_BYTES", 5),
            self.assertRaises(SavedOdooPackageError),
        ):
            read_saved_odoo_package(package, output, password=PASSWORD)
        self.assertFalse(output.exists())
        self.assertFalse(any(self.root.glob(".impodo-saved-*")))

    def test_authenticated_invalid_archives_are_not_admitted(self) -> None:
        actual = b"actual-data"
        valid = self._manifest("payload/data", actual)
        forged_hash = self._manifest("payload/data", b"forged-data")
        duplicate_property = valid.replace(
            b'"contract_version": 1',
            b'"contract_version": 1, "contract_version": 1',
            1,
        )
        cases: tuple[tuple[str, list[tuple[str | ZipInfo, bytes]], int], ...] = (
            (
                "hash",
                [
                    ("payload/data", actual),
                    ("metadata/impodo-package.json", forged_hash),
                ],
                ZIP_STORED,
            ),
            (
                "duplicate-property",
                [
                    ("payload/data", actual),
                    ("metadata/impodo-package.json", duplicate_property),
                ],
                ZIP_STORED,
            ),
            (
                "extra",
                [
                    ("payload/data", actual),
                    ("payload/extra", b"extra"),
                    ("metadata/impodo-package.json", valid),
                ],
                ZIP_STORED,
            ),
            (
                "traversal",
                [
                    ("payload/../outside", actual),
                    ("metadata/impodo-package.json", valid),
                ],
                ZIP_STORED,
            ),
            (
                "duplicate",
                [
                    ("payload/data", actual),
                    ("payload/data", actual),
                    ("metadata/impodo-package.json", valid),
                ],
                ZIP_STORED,
            ),
            (
                "compressed",
                [("payload/data", actual), ("metadata/impodo-package.json", valid)],
                ZIP_DEFLATED,
            ),
            (
                "symlink",
                [
                    (self._symlink("payload/data"), actual),
                    ("metadata/impodo-package.json", valid),
                ],
                ZIP_STORED,
            ),
        )
        for name, members, compression in cases:
            with self.subTest(name=name):
                package = self._authenticated_archive(name, members, compression)
                output = self.root / f"rejected-{name}"
                with self.assertRaises(SavedOdooPackageError):
                    read_saved_odoo_package(package, output, password=PASSWORD)
                self.assertFalse(output.exists())
        self.assertFalse((self.root / "outside").exists())

    def test_short_password_and_truncated_package_fail_without_output(self) -> None:
        package = self.root / "truncated.impodo"
        with self.assertRaises(SavedOdooPackageError):
            write_saved_odoo_package(
                package, {"payload/data": b"data"}, password="short"
            )
        package.write_bytes(b"IMPSAVE1\x01")
        output = self.root / "truncated-import"
        with self.assertRaises(SavedOdooPackageError):
            read_saved_odoo_package(package, output, password=PASSWORD)
        self.assertFalse(output.exists())

    @staticmethod
    def _manifest(name: str, value: bytes) -> bytes:
        return json.dumps(
            {
                "contract_version": 1,
                "package_id": str(uuid4()),
                "created_at": datetime.now(UTC).isoformat(),
                "members": [
                    {
                        "name": name,
                        "size_bytes": len(value),
                        "sha256": "sha256:" + hashlib.sha256(value).hexdigest(),
                    }
                ],
            }
        ).encode()

    @staticmethod
    def _symlink(name: str) -> ZipInfo:
        info = ZipInfo(name)
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        return info

    def _authenticated_archive(
        self, name: str, members: list[tuple[str | ZipInfo, bytes]], compression: int
    ) -> Path:
        plaintext = self.root / f"{name}.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with ZipFile(plaintext, "w", compression=compression) as archive:
                for member_name, value in members:
                    archive.writestr(member_name, value)
        package = self.root / f"{name}.impodo"
        packages._encrypt(plaintext, package, PASSWORD.encode())
        return package
