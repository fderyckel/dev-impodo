"""Read and write bounded, password-protected portable saved Odoo packages.

The caller selects credential-free metadata and payload artifacts. This codec
streams those explicit members through an uncompressed ZIP container and
authenticates the entire encrypted container before inspecting its contents.
Import verifies every member before atomically exposing its output directory.
It neither reads workspace stores nor decides which business data to export.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO
from uuid import UUID, uuid4
from zipfile import ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

SAVED_ODOO_PACKAGE_VERSION = 1
SAVED_ODOO_PACKAGE_MAX_MEMBERS = 512
SAVED_ODOO_PACKAGE_MAX_MEMBER_BYTES = 128 * 1024 * 1024
SAVED_ODOO_PACKAGE_MAX_TOTAL_BYTES = 256 * 1024 * 1024
_MANIFEST_BYTES = 256 * 1024
_ARCHIVE_OVERHEAD_BYTES = 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_MAGIC = b"IMPSAVE1"
_HEADER = struct.Struct(">8sB16s12s")
_TAG_BYTES = 16
_KDF_ITERATIONS = 600_000
_MANIFEST_NAME = "metadata/impodo-package.json"
_MEMBER_NAME = re.compile(
    r"(?:metadata|payload)/(?:[A-Za-z0-9][A-Za-z0-9_.-]*/)*"
    r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z"
)
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


class SavedOdooPackageError(ValueError):
    """Report an invalid, incomplete, changed, or unopenable portable package."""


@dataclass(frozen=True, slots=True)
class SavedOdooPackageMember:
    """Describe the exact plaintext bytes of one explicitly selected artifact."""

    name: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        _validate_member_name(self.name)
        if (
            type(self.size_bytes) is not int
            or not 0 <= self.size_bytes <= SAVED_ODOO_PACKAGE_MAX_MEMBER_BYTES
            or not isinstance(self.sha256, str)
            or _HASH.fullmatch(self.sha256) is None
        ):
            raise SavedOdooPackageError("Saved data member evidence is invalid")

    def to_dict(self) -> dict[str, str | int]:
        """Return the exact authenticated manifest entry."""

        return {
            "name": self.name,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class SavedOdooPackageManifest:
    """Identify a complete package and all its authenticated plaintext members."""

    package_id: str
    created_at: datetime
    members: tuple[SavedOdooPackageMember, ...]
    contract_version: int = SAVED_ODOO_PACKAGE_VERSION

    def __post_init__(self) -> None:
        try:
            UUID(self.package_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise SavedOdooPackageError(
                "Saved data package identity is invalid"
            ) from error
        if (
            type(self.contract_version) is not int
            or self.contract_version != SAVED_ODOO_PACKAGE_VERSION
            or self.created_at.tzinfo is None
            or not 1 <= len(self.members) <= SAVED_ODOO_PACKAGE_MAX_MEMBERS
            or self.members != tuple(sorted(self.members, key=lambda item: item.name))
            or len({item.name.casefold() for item in self.members}) != len(self.members)
            or sum(item.size_bytes for item in self.members)
            > SAVED_ODOO_PACKAGE_MAX_TOTAL_BYTES
        ):
            raise SavedOdooPackageError("Saved data package manifest is invalid")

    def to_dict(self) -> dict[str, object]:
        """Return versioned JSON-ready metadata without credentials or passwords."""

        return {
            "contract_version": self.contract_version,
            "package_id": self.package_id,
            "created_at": self.created_at.isoformat(),
            "members": [item.to_dict() for item in self.members],
        }


@dataclass(frozen=True, slots=True)
class SavedOdooPackageContents:
    """Return verified artifact paths after complete import publication."""

    manifest: SavedOdooPackageManifest
    member_paths: tuple[tuple[str, Path], ...]

    def path(self, name: str) -> Path:
        """Find an admitted member by its exact portable name."""

        for member_name, member_path in self.member_paths:
            if member_name == name:
                return member_path
        raise SavedOdooPackageError("The requested saved data member is unavailable")


def write_saved_odoo_package(
    destination: Path,
    members: Mapping[str, Path | bytes],
    *,
    password: str,
) -> SavedOdooPackageManifest:
    """Encrypt explicit members into a new package without replacing a file.

    Passwords contain at least eight characters and at most 1,024 UTF-8 bytes.
    All metadata and member bytes are encrypted. The caller must omit API keys
    and other credentials from the selected members. Source artifacts are read
    in bounded chunks, and failed exports leave no destination package.
    """

    password_bytes = _password_bytes(password)
    destination = Path(destination).absolute()
    _require_new_destination(destination)
    names = _member_names(members)
    try:
        with tempfile.TemporaryDirectory(
            prefix=".impodo-saved-export-", dir=destination.parent
        ) as temporary:
            temporary_root = Path(temporary)
            archive_path = temporary_root / "members.zip"
            evidence: list[SavedOdooPackageMember] = []
            total_bytes = 0
            with ZipFile(archive_path, "w", compression=ZIP_STORED) as archive:
                for name in names:
                    member = _write_member(archive, name, members[name])
                    total_bytes += member.size_bytes
                    if total_bytes > SAVED_ODOO_PACKAGE_MAX_TOTAL_BYTES:
                        raise SavedOdooPackageError(
                            "Saved data exceeds the package size limit"
                        )
                    evidence.append(member)
                manifest = SavedOdooPackageManifest(
                    package_id=str(uuid4()),
                    created_at=datetime.now(UTC),
                    members=tuple(evidence),
                )
                manifest_bytes = _manifest_bytes(manifest)
                archive.writestr(_zip_info(_MANIFEST_NAME), manifest_bytes)
            encrypted_path = temporary_root / "package.impodo"
            _encrypt(archive_path, encrypted_path, password_bytes)
            # A hard link publishes the already-complete file without a
            # check-then-replace race that could overwrite another export.
            os.link(encrypted_path, destination)
        return manifest
    except (OSError, BadZipFile, ValueError) as error:
        if isinstance(error, SavedOdooPackageError):
            raise
        raise SavedOdooPackageError(
            "The saved data package could not be created"
        ) from error


def read_saved_odoo_package(
    source: Path,
    destination_dir: Path,
    *,
    password: str,
) -> SavedOdooPackageContents:
    """Authenticate and verify every member before publishing a new directory.

    The destination must not exist. An invalid password, changed container,
    unrecognized member, size breach, or failed integrity check leaves it
    absent. No extraction starts until encrypted-container authentication has
    succeeded. The caller admits returned metadata into its domain separately.
    """

    password_bytes = _password_bytes(password)
    source = Path(source)
    destination_dir = Path(destination_dir).absolute()
    _require_new_destination(destination_dir)
    try:
        with tempfile.TemporaryDirectory(
            prefix=".impodo-saved-import-", dir=destination_dir.parent
        ) as temporary:
            temporary_root = Path(temporary)
            archive_path = temporary_root / "members.zip"
            _decrypt(source, archive_path, password_bytes)
            candidate = temporary_root / "verified"
            candidate.mkdir(mode=0o700)
            with ZipFile(archive_path, "r") as archive:
                manifest = _read_manifest(archive)
                for member in manifest.members:
                    _extract_verified_member(archive, candidate, member)
            if destination_dir.exists() or destination_dir.is_symlink():
                raise SavedOdooPackageError("Choose a new saved data import directory")
            candidate.rename(destination_dir)
        return SavedOdooPackageContents(
            manifest=manifest,
            member_paths=tuple(
                (member.name, destination_dir.joinpath(*member.name.split("/")))
                for member in manifest.members
            ),
        )
    except InvalidTag as error:
        raise SavedOdooPackageError(
            "The package password is incorrect or the package has changed"
        ) from error
    except (OSError, BadZipFile, EOFError, RuntimeError, ValueError) as error:
        if isinstance(error, SavedOdooPackageError):
            raise
        raise SavedOdooPackageError(
            "The saved data package could not be opened"
        ) from error


def _password_bytes(password: str) -> bytes:
    if not isinstance(password, str) or len(password) < 8:
        raise SavedOdooPackageError(
            "Use a package password with at least eight characters"
        )
    value = password.encode("utf-8")
    if len(value) > 1024:
        raise SavedOdooPackageError("The package password is too long")
    return value


def _require_new_destination(destination: Path) -> None:
    if not destination.parent.is_dir():
        raise SavedOdooPackageError("Choose an existing directory for saved data")
    if destination.exists() or destination.is_symlink():
        raise SavedOdooPackageError("Choose a new saved data destination")


def _validate_member_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or len(name) > 240
        or _MEMBER_NAME.fullmatch(name) is None
        or name.casefold() == _MANIFEST_NAME.casefold()
        or any(
            len(segment) > 128
            or segment.endswith(".")
            or segment.split(".", 1)[0].upper() in _WINDOWS_RESERVED
            for segment in name.split("/")
        )
    ):
        raise SavedOdooPackageError(
            "Saved data contains an invalid portable member name"
        )


def _member_names(members: Mapping[str, Path | bytes]) -> tuple[str, ...]:
    names = tuple(sorted(members))
    if not 1 <= len(names) <= SAVED_ODOO_PACKAGE_MAX_MEMBERS or len(
        {name.casefold() for name in names}
    ) != len(names):
        raise SavedOdooPackageError("Saved data contains an invalid member selection")
    for name in names:
        _validate_member_name(name)
    return names


def _zip_info(name: str) -> ZipInfo:
    result = ZipInfo(name)
    result.compress_type = ZIP_STORED
    result.create_system = 3
    result.external_attr = (stat.S_IFREG | 0o600) << 16
    return result


def _write_member(
    archive: ZipFile, name: str, value: Path | bytes
) -> SavedOdooPackageMember:
    digest = hashlib.sha256()
    size_bytes = 0
    with archive.open(_zip_info(name), "w") as target:
        if isinstance(value, bytes):
            if len(value) > SAVED_ODOO_PACKAGE_MAX_MEMBER_BYTES:
                raise SavedOdooPackageError(
                    "A saved data member exceeds its size limit"
                )
            target.write(value)
            digest.update(value)
            size_bytes = len(value)
        elif isinstance(value, Path):
            if value.is_symlink() or not value.is_file():
                raise SavedOdooPackageError("A saved data member is not a regular file")
            with value.open("rb") as source:
                while chunk := source.read(_CHUNK_BYTES):
                    size_bytes += len(chunk)
                    if size_bytes > SAVED_ODOO_PACKAGE_MAX_MEMBER_BYTES:
                        raise SavedOdooPackageError(
                            "A saved data member exceeds its size limit"
                        )
                    target.write(chunk)
                    digest.update(chunk)
        else:
            raise SavedOdooPackageError("A saved data member has an unsupported source")
    return SavedOdooPackageMember(name, size_bytes, "sha256:" + digest.hexdigest())


def _manifest_bytes(manifest: SavedOdooPackageManifest) -> bytes:
    value = json.dumps(
        manifest.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(value) > _MANIFEST_BYTES:
        raise SavedOdooPackageError("The saved data manifest exceeds its size limit")
    return value


def _key(password: bytes, salt: bytes) -> bytes:
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_KDF_ITERATIONS
    ).derive(password)


def _archive_limit() -> int:
    return SAVED_ODOO_PACKAGE_MAX_TOTAL_BYTES + _ARCHIVE_OVERHEAD_BYTES


def _encrypt(source: Path, destination: Path, password: bytes) -> None:
    if source.stat().st_size > _archive_limit():
        raise SavedOdooPackageError("The saved data container exceeds its size limit")
    salt = os.urandom(16)
    nonce = os.urandom(12)
    header = _HEADER.pack(_MAGIC, SAVED_ODOO_PACKAGE_VERSION, salt, nonce)
    encryptor = Cipher(
        algorithms.AES(_key(password, salt)), modes.GCM(nonce)
    ).encryptor()
    encryptor.authenticate_additional_data(header)
    with source.open("rb") as reader, destination.open("xb") as writer:
        writer.write(header)
        while chunk := reader.read(_CHUNK_BYTES):
            writer.write(encryptor.update(chunk))
        writer.write(encryptor.finalize())
        writer.write(encryptor.tag)
        writer.flush()
        os.fsync(writer.fileno())


def _decrypt(source: Path, destination: Path, password: bytes) -> None:
    size_bytes = source.stat().st_size
    ciphertext_bytes = size_bytes - _HEADER.size - _TAG_BYTES
    if not 1 <= ciphertext_bytes <= _archive_limit():
        raise SavedOdooPackageError("The saved data container size is invalid")
    with source.open("rb") as reader:
        header = reader.read(_HEADER.size)
        magic, version, salt, nonce = _HEADER.unpack(header)
        if magic != _MAGIC or version != SAVED_ODOO_PACKAGE_VERSION:
            raise SavedOdooPackageError("The saved data package version is unsupported")
        reader.seek(-_TAG_BYTES, os.SEEK_END)
        tag = reader.read(_TAG_BYTES)
        reader.seek(_HEADER.size)
        decryptor = Cipher(
            algorithms.AES(_key(password, salt)), modes.GCM(nonce, tag)
        ).decryptor()
        decryptor.authenticate_additional_data(header)
        with destination.open("xb") as writer:
            remaining = ciphertext_bytes
            while remaining:
                chunk = reader.read(min(_CHUNK_BYTES, remaining))
                if not chunk:
                    raise SavedOdooPackageError("The saved data package is incomplete")
                remaining -= len(chunk)
                writer.write(decryptor.update(chunk))
            writer.write(decryptor.finalize())


def _read_manifest(archive: ZipFile) -> SavedOdooPackageManifest:
    entries = archive.infolist()
    if not 2 <= len(entries) <= SAVED_ODOO_PACKAGE_MAX_MEMBERS + 1:
        raise SavedOdooPackageError("The saved data container member count is invalid")
    names = [item.filename for item in entries]
    if len({name.casefold() for name in names}) != len(names):
        raise SavedOdooPackageError("The saved data container has duplicate members")
    for entry in entries:
        _validate_zip_entry(entry)
    if names.count(_MANIFEST_NAME) != 1:
        raise SavedOdooPackageError("The saved data manifest is missing")
    metadata = archive.getinfo(_MANIFEST_NAME)
    if metadata.file_size > _MANIFEST_BYTES:
        raise SavedOdooPackageError("The saved data manifest exceeds its size limit")
    with archive.open(metadata) as reader:
        raw = reader.read(_MANIFEST_BYTES + 1)
    if len(raw) > _MANIFEST_BYTES:
        raise SavedOdooPackageError("The saved data manifest exceeds its size limit")
    manifest = _parse_manifest(raw)
    expected = {_MANIFEST_NAME, *(item.name for item in manifest.members)}
    if set(names) != expected:
        raise SavedOdooPackageError(
            "The saved data members do not match their manifest"
        )
    if (
        sum(item.file_size for item in entries)
        > SAVED_ODOO_PACKAGE_MAX_TOTAL_BYTES + _MANIFEST_BYTES
    ):
        raise SavedOdooPackageError("Saved data exceeds the package size limit")
    return manifest


def _validate_zip_entry(entry: ZipInfo) -> None:
    if entry.filename != _MANIFEST_NAME:
        _validate_member_name(entry.filename)
    file_type = stat.S_IFMT(entry.external_attr >> 16)
    if (
        entry.is_dir()
        or entry.orig_filename != entry.filename
        or entry.compress_type != ZIP_STORED
        or entry.flag_bits & 1
        or entry.file_size != entry.compress_size
        or entry.file_size < 0
        or entry.file_size > SAVED_ODOO_PACKAGE_MAX_MEMBER_BYTES
        or file_type not in {0, stat.S_IFREG}
    ):
        raise SavedOdooPackageError(
            "The saved data container has an unsupported member"
        )


def _parse_manifest(raw: bytes) -> SavedOdooPackageManifest:
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_json_object)
        if not isinstance(payload, dict) or set(payload) != {
            "contract_version",
            "package_id",
            "created_at",
            "members",
        }:
            raise SavedOdooPackageError("The saved data manifest structure is invalid")
        member_values = payload["members"]
        if not isinstance(member_values, list):
            raise SavedOdooPackageError("The saved data manifest members are invalid")
        members: list[SavedOdooPackageMember] = []
        for value in member_values:
            if not isinstance(value, dict) or set(value) != {
                "name",
                "size_bytes",
                "sha256",
            }:
                raise SavedOdooPackageError("The saved data manifest member is invalid")
            members.append(SavedOdooPackageMember(**value))
        return SavedOdooPackageManifest(
            package_id=payload["package_id"],
            created_at=datetime.fromisoformat(payload["created_at"]),
            members=tuple(members),
            contract_version=payload["contract_version"],
        )
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as error:
        if isinstance(error, SavedOdooPackageError):
            raise
        raise SavedOdooPackageError("The saved data manifest is invalid") from error


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SavedOdooPackageError(
                "The saved data manifest has duplicate properties"
            )
        result[key] = value
    return result


def _extract_verified_member(
    archive: ZipFile, candidate: Path, member: SavedOdooPackageMember
) -> None:
    entry = archive.getinfo(member.name)
    if entry.file_size != member.size_bytes:
        raise SavedOdooPackageError(
            "A saved data member size does not match its manifest"
        )
    path = candidate.joinpath(*member.name.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(entry) as reader, path.open("xb") as writer:
        count, digest = _copy_verified_bytes(reader, writer, member.size_bytes)
    if count != member.size_bytes or digest != member.sha256:
        raise SavedOdooPackageError("A saved data member failed its integrity check")


def _copy_verified_bytes(
    reader: IO[bytes], writer: IO[bytes], expected_bytes: int
) -> tuple[int, str]:
    # Hashing stays in the extraction loop so plaintext is never read a second
    # time merely to establish its manifest identity.
    hasher = hashlib.sha256()
    count = 0
    while chunk := reader.read(_CHUNK_BYTES):
        count += len(chunk)
        if count > expected_bytes:
            raise SavedOdooPackageError("A saved data member exceeds its declared size")
        writer.write(chunk)
        hasher.update(chunk)
    return count, "sha256:" + hasher.hexdigest()
