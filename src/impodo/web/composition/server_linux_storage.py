"""Observe Linux storage roots for the shared-server mount policy."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from impodo.web.composition.server_config import ServerSettings
from impodo.web.composition.server_storage import (
    ServerStorageObservationError,
    StorageMountKind,
    StorageRootObservation,
)

_MOUNT_ESCAPE_PATTERN = re.compile(r"\\([0-7]{3})")
_TRANSIENT_FILESYSTEMS = frozenset({"aufs", "overlay", "ramfs", "tmpfs"})


class LinuxStorageObservationError(ServerStorageObservationError):
    """Report that Linux storage facts could not be established safely."""


@dataclass(frozen=True, slots=True)
class LinuxMount:
    """Hold the mount facts needed to classify one configured root."""

    mount_id: int
    mount_point: PurePosixPath
    filesystem_type: str
    writable: bool


class StatResult(Protocol):
    """Expose only the filesystem metadata required by the mount policy."""

    @property
    def st_mode(self) -> int: ...

    @property
    def st_uid(self) -> int: ...

    @property
    def st_gid(self) -> int: ...


StatReader = Callable[[PurePosixPath], StatResult]
WritableProbe = Callable[[PurePosixPath], bool]


def _decode_mount_field(value: str) -> str:
    return _MOUNT_ESCAPE_PATTERN.sub(
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def parse_linux_mountinfo(value: str) -> tuple[LinuxMount, ...]:
    """Parse the bounded mount facts published by ``/proc/self/mountinfo``."""

    mounts: list[LinuxMount] = []
    for line_number, line in enumerate(value.splitlines(), start=1):
        if not line.strip():
            continue
        fields = line.split()
        try:
            separator = fields.index("-")
            mount_id = int(fields[0])
            mount_point_value = fields[4]
            mount_options = fields[5].split(",")
            filesystem_type = fields[separator + 1]
            super_options = fields[separator + 3].split(",")
        except (IndexError, ValueError) as error:
            raise LinuxStorageObservationError(
                f"Linux mount information is malformed at line {line_number}"
            ) from error
        if mount_id <= 0:
            raise LinuxStorageObservationError(
                f"Linux mount information has an invalid ID at line {line_number}"
            )
        mount_point = PurePosixPath(_decode_mount_field(mount_point_value))
        if not mount_point.is_absolute() or ".." in mount_point.parts:
            raise LinuxStorageObservationError(
                f"Linux mount information has an invalid path at line {line_number}"
            )
        mounts.append(
            LinuxMount(
                mount_id=mount_id,
                mount_point=mount_point,
                filesystem_type=filesystem_type,
                writable="rw" in mount_options and "rw" in super_options,
            )
        )
    if not mounts:
        raise LinuxStorageObservationError("Linux mount information is empty")
    return tuple(mounts)


def _containing_mount(
    path: PurePosixPath,
    mounts: tuple[LinuxMount, ...],
) -> LinuxMount | None:
    candidates = tuple(
        mount
        for mount in mounts
        if path == mount.mount_point or mount.mount_point in path.parents
    )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda mount: (len(mount.mount_point.parts), mount.mount_id),
    )


def _mount_kind(mount: LinuxMount | None) -> StorageMountKind:
    if mount is None:
        return StorageMountKind.UNKNOWN
    if mount.filesystem_type in _TRANSIENT_FILESYSTEMS:
        return StorageMountKind.TRANSIENT
    if mount.mount_point == PurePosixPath("/"):
        return StorageMountKind.UNKNOWN
    return StorageMountKind.PERSISTENT


def _stat_path(path: PurePosixPath) -> os.stat_result:
    return os.stat(Path(path), follow_symlinks=False)


def _probe_writable(path: PurePosixPath) -> bool:
    probe_path: Path | None = None
    descriptor: int | None = None
    writable = False
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".impodo-write-probe-",
            dir=Path(path),
        )
        probe_path = Path(raw_path)
        os.write(descriptor, b"impodo-storage-probe\n")
        os.fsync(descriptor)
        writable = True
    except OSError:
        writable = False
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                writable = False
        if probe_path is not None:
            try:
                probe_path.unlink()
            except OSError:
                writable = False
    return writable


def _observe_root(
    path: PurePosixPath,
    *,
    mounts: tuple[LinuxMount, ...],
    stat_reader: StatReader,
    writable_probe: WritableProbe,
) -> StorageRootObservation:
    mount = _containing_mount(path, mounts)
    try:
        metadata = stat_reader(path)
    except FileNotFoundError:
        return StorageRootObservation(
            path=path,
            exists=False,
            is_directory=False,
            owner_uid=None,
            owner_gid=None,
            mode=None,
            writable=False,
            mount_kind=_mount_kind(mount),
        )
    except OSError as error:
        raise LinuxStorageObservationError(
            f"Linux storage metadata is unavailable for {path}"
        ) from error
    is_directory = stat.S_ISDIR(metadata.st_mode)
    writable = (
        is_directory and mount is not None and mount.writable and writable_probe(path)
    )
    return StorageRootObservation(
        path=path,
        exists=True,
        is_directory=is_directory,
        owner_uid=metadata.st_uid,
        owner_gid=metadata.st_gid,
        mode=stat.S_IMODE(metadata.st_mode),
        writable=writable,
        mount_kind=_mount_kind(mount),
    )


def observe_linux_server_storage(
    settings: ServerSettings,
    *,
    mountinfo: str | None = None,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    stat_reader: StatReader = _stat_path,
    writable_probe: WritableProbe = _probe_writable,
) -> tuple[StorageRootObservation, ...]:
    """Observe every Impodo-owned root without inspecting PostgreSQL storage."""

    if mountinfo is None:
        try:
            mountinfo = mountinfo_path.read_text(encoding="utf-8")
        except OSError as error:
            raise LinuxStorageObservationError(
                "Linux mount information is unavailable"
            ) from error
    mounts = parse_linux_mountinfo(mountinfo)
    return tuple(
        _observe_root(
            path,
            mounts=mounts,
            stat_reader=stat_reader,
            writable_probe=writable_probe,
        )
        for path in (
            settings.artifact_root,
            settings.runtime_root,
            settings.temporary_root,
        )
    )


__all__ = [
    "LinuxMount",
    "LinuxStorageObservationError",
    "StatReader",
    "WritableProbe",
    "observe_linux_server_storage",
    "parse_linux_mountinfo",
]
