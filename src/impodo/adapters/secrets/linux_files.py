"""Read root-owned Linux secret files through bounded file descriptors."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import PurePosixPath
from typing import Protocol

MAX_SECRET_FILE_BYTES = 8 * 1024


class LinuxSecretFileError(RuntimeError):
    """Report a bounded secret-file rejection without exposing its content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Linux secret file rejected: {code}")


class LinuxFileMetadata(Protocol):
    """Expose only metadata required by the Linux secret-file policy."""

    @property
    def st_mode(self) -> int: ...

    @property
    def st_uid(self) -> int: ...

    @property
    def st_gid(self) -> int: ...

    @property
    def st_nlink(self) -> int: ...

    @property
    def st_size(self) -> int: ...


class LinuxFileDescriptorOperations(Protocol):
    """Abstract Linux descriptor operations for deterministic policy tests."""

    def open_directory(self, path: PurePosixPath) -> int: ...

    def open_child(self, directory_fd: int, name: str) -> int: ...

    def metadata(self, descriptor: int) -> LinuxFileMetadata: ...

    def read(self, descriptor: int, maximum_bytes: int) -> bytes: ...

    def close(self, descriptor: int) -> None: ...


def _required_open_flag(name: str) -> int:
    value = getattr(os, name, None)
    if not isinstance(value, int):
        raise LinuxSecretFileError("unsupported_platform")
    return value


class OsLinuxFileDescriptorOperations:
    """Use Linux no-follow and descriptor-relative file operations."""

    def open_directory(self, path: PurePosixPath) -> int:
        flags = (
            os.O_RDONLY
            | _required_open_flag("O_CLOEXEC")
            | _required_open_flag("O_DIRECTORY")
            | _required_open_flag("O_NOFOLLOW")
        )
        return os.open(os.fspath(path), flags)

    def open_child(self, directory_fd: int, name: str) -> int:
        flags = (
            os.O_RDONLY
            | _required_open_flag("O_CLOEXEC")
            | _required_open_flag("O_NOFOLLOW")
        )
        return os.open(name, flags, dir_fd=directory_fd)

    def metadata(self, descriptor: int) -> os.stat_result:
        return os.fstat(descriptor)

    def read(self, descriptor: int, maximum_bytes: int) -> bytes:
        chunks: list[bytes] = []
        remaining = maximum_bytes
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def close(self, descriptor: int) -> None:
        os.close(descriptor)


def _validate_root(
    metadata: LinuxFileMetadata,
    *,
    expected_gid: int,
) -> None:
    mode = stat.S_IMODE(metadata.st_mode)
    if not stat.S_ISDIR(metadata.st_mode):
        raise LinuxSecretFileError("root_not_directory")
    if metadata.st_uid != 0:
        raise LinuxSecretFileError("root_not_owned_by_root")
    if metadata.st_gid != expected_gid:
        raise LinuxSecretFileError("root_wrong_group")
    if not mode & stat.S_IXGRP:
        raise LinuxSecretFileError("root_group_cannot_traverse")
    if mode & (stat.S_IWGRP | stat.S_IRWXO):
        raise LinuxSecretFileError("root_permissions_too_broad")


def _validate_secret(
    metadata: LinuxFileMetadata,
    *,
    expected_gid: int,
) -> None:
    mode = stat.S_IMODE(metadata.st_mode)
    if not stat.S_ISREG(metadata.st_mode):
        raise LinuxSecretFileError("not_regular_file")
    if metadata.st_nlink != 1:
        raise LinuxSecretFileError("unexpected_link_count")
    if metadata.st_uid != 0:
        raise LinuxSecretFileError("not_owned_by_root")
    if metadata.st_gid != expected_gid:
        raise LinuxSecretFileError("wrong_group")
    if not mode & stat.S_IRUSR or not mode & stat.S_IRGRP:
        raise LinuxSecretFileError("not_readable_by_service_group")
    if mode & (stat.S_IXUSR | stat.S_IWGRP | stat.S_IXGRP | stat.S_IRWXO):
        raise LinuxSecretFileError("permissions_too_broad")
    if not 0 < metadata.st_size <= MAX_SECRET_FILE_BYTES:
        raise LinuxSecretFileError("invalid_size")


class LinuxSecretFileReader:
    """Read one direct child of a protected root without following symlinks."""

    def __init__(
        self,
        *,
        expected_gid: int,
        operations: LinuxFileDescriptorOperations | None = None,
    ) -> None:
        self._expected_gid = expected_gid
        self._operations = operations or OsLinuxFileDescriptorOperations()

    def read_text(
        self,
        root: PurePosixPath,
        reference: PurePosixPath,
    ) -> str:
        """Return one bounded UTF-8 line from a direct secret-root child."""

        if reference.parent != root or reference.name in {"", ".", ".."}:
            raise LinuxSecretFileError("outside_secret_root")

        root_descriptor: int | None = None
        secret_descriptor: int | None = None
        try:
            root_descriptor = self._operations.open_directory(root)
            _validate_root(
                self._operations.metadata(root_descriptor),
                expected_gid=self._expected_gid,
            )
            secret_descriptor = self._operations.open_child(
                root_descriptor,
                reference.name,
            )
            metadata = self._operations.metadata(secret_descriptor)
            _validate_secret(metadata, expected_gid=self._expected_gid)
            payload = self._operations.read(
                secret_descriptor,
                MAX_SECRET_FILE_BYTES + 1,
            )
            if len(payload) != metadata.st_size:
                raise LinuxSecretFileError("size_changed_while_reading")
            value = payload.decode("utf-8").rstrip("\r\n")
            if (
                not value
                or value != value.strip()
                or "\x00" in value
                or "\r" in value
                or "\n" in value
            ):
                raise LinuxSecretFileError("invalid_text")
            return value
        except LinuxSecretFileError:
            raise
        except (OSError, UnicodeError) as error:
            raise LinuxSecretFileError("unavailable") from error
        finally:
            active_error = sys.exception()
            close_error: OSError | None = None
            if secret_descriptor is not None:
                try:
                    self._operations.close(secret_descriptor)
                except OSError as error:
                    close_error = error
            if root_descriptor is not None:
                try:
                    self._operations.close(root_descriptor)
                except OSError as error:
                    close_error = close_error or error
            if close_error is not None and active_error is None:
                raise LinuxSecretFileError("unavailable") from close_error


__all__ = [
    "MAX_SECRET_FILE_BYTES",
    "LinuxFileDescriptorOperations",
    "LinuxFileMetadata",
    "LinuxSecretFileError",
    "LinuxSecretFileReader",
    "OsLinuxFileDescriptorOperations",
]
