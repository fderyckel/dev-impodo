"""Plan and execute an explicit recoverable development-storage reset."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
from uuid import uuid4

from .domain.serialization import content_hash
from .migration_foundation import MigrationFoundationError


_UUID_DIRECTORY = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_KNOWN_FILES = frozenset({"registry.duckdb", "registry.duckdb.wal"})
_KNOWN_DIRECTORIES = frozenset(
    {
        ".project-evidence-protected",
        ".recipes-protected",
        "artifacts",
        "projects",
    }
)
_QUARANTINE_DIRECTORY = ".impodo-development-reset"


@dataclass(frozen=True, slots=True)
class DevelopmentResetPlan:
    """Enumerate the exact local storage that a developer may quarantine."""

    storage_root: Path
    targets: tuple[Path, ...]
    unknown_entries: tuple[Path, ...]
    fingerprint: str
    confirmation_token: str

    @property
    def can_execute(self) -> bool:
        return bool(self.targets) and not self.unknown_entries


def plan_development_reset(root: str | Path) -> DevelopmentResetPlan:
    """Return a non-mutating reset plan for one explicit Impodo storage root."""

    storage_root = Path(root).resolve()
    if not storage_root.is_dir():
        raise MigrationFoundationError("Development storage root does not exist")
    targets: list[Path] = []
    unknown: list[Path] = []
    for entry in sorted(storage_root.iterdir(), key=lambda item: item.name):
        if entry.name == _QUARANTINE_DIRECTORY:
            continue
        if (
            entry.is_file()
            and entry.name in _KNOWN_FILES
            or entry.is_dir()
            and (
                entry.name in _KNOWN_DIRECTORIES
                or _UUID_DIRECTORY.fullmatch(entry.name) is not None
            )
        ):
            targets.append(entry.resolve())
        else:
            unknown.append(entry.resolve())
    fingerprint = content_hash(
        {
            "storage_root": str(storage_root),
            "targets": [item.name for item in targets],
            "unknown_entries": [item.name for item in unknown],
        }
    )
    return DevelopmentResetPlan(
        storage_root=storage_root,
        targets=tuple(targets),
        unknown_entries=tuple(unknown),
        fingerprint=fingerprint,
        confirmation_token=f"RESET-MIGRATION-STORAGE:{fingerprint}",
    )


def execute_development_reset(
    plan: DevelopmentResetPlan,
    *,
    confirmation_token: str,
    development_mode: bool,
) -> Path:
    """Move an unchanged confirmed plan to a recoverable quarantine directory."""

    if not development_mode:
        raise MigrationFoundationError(
            "Development storage reset requires IMPODO_DEVELOPMENT_MODE=1"
        )
    if confirmation_token != plan.confirmation_token:
        raise MigrationFoundationError("Development reset confirmation is invalid")
    current = plan_development_reset(plan.storage_root)
    if current.fingerprint != plan.fingerprint:
        raise MigrationFoundationError(
            "Development storage changed after review; create a new reset plan"
        )
    if current.unknown_entries:
        raise MigrationFoundationError(
            "Development storage contains unrecognized entries; nothing was moved"
        )
    if not current.targets:
        raise MigrationFoundationError("Development storage has nothing to reset")
    quarantine = (
        current.storage_root
        / _QUARANTINE_DIRECTORY
        / str(uuid4())
    )
    quarantine.mkdir(parents=True, exist_ok=False)
    moved: list[tuple[Path, Path]] = []
    try:
        for source in current.targets:
            if source.parent != current.storage_root:
                raise MigrationFoundationError("Development reset target escaped root")
            destination = quarantine / source.name
            shutil.move(str(source), str(destination))
            moved.append((source, destination))
    except Exception:
        for source, destination in reversed(moved):
            if destination.exists() and not source.exists():
                shutil.move(str(destination), str(source))
        raise
    return quarantine
