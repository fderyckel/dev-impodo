"""Preserve known older Project storage and expose unavailable summaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from uuid import UUID, uuid4

import duckdb

from impodo.adapters.duckdb.schema.migration_registry import (
    MIGRATION_REGISTRY_GENERATION,
)
from impodo.web.composition.development_reset import DevelopmentResetPlan, plan_development_reset
from impodo.domain.project.foundation import MigrationFoundationError


LEGACY_RECIPE_ROOT_MIGRATION_ID = "2026-08-19-recipe-clean-root-v2"
LEGACY_RECIPE_ROOT_MIGRATION_CHECKSUM = (
    "sha256:84954535ac8c1342ca4735553811a24c9347e13b53ad38ce9434224c83049e89"
)
UNAVAILABLE_PROJECT_MESSAGE = (
    "This project uses an older saved-data format that this version of Impodo "
    "cannot safely open. Its saved data is preserved. Create a new project to "
    "continue."
)

_ARCHIVE_DIRECTORY = ".impodo-development-reset"
_MANIFEST_NAME = "unavailable-projects.json"
_MANIFEST_VERSION = 1
_MAX_UNAVAILABLE_PROJECTS = 10_000
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MIGRATION_REGISTRY_GENERATION_PREFIX = "impodo-migration-registry-"
_KNOWN_PRIOR_FOUNDATION_VERSIONS = {
    "impodo-migration-registry-2026-08-m5": 1,
}
_REQUIRED_LEGACY_PROJECT_COLUMNS = frozenset(
    {"project_id", "name", "status", "revision", "updated_at"}
)
_REQUIRED_FOUNDATION_PROJECT_COLUMNS = frozenset(
    {
        "display_name",
        "optimistic_revision",
        "project_id",
        "status",
        "updated_at",
    }
)
_KNOWN_PROJECT_STATUSES = frozenset(
    {"ACTIVE", "ARCHIVED", "CLOSED", "DRAFT", "REGISTERED"}
)


@dataclass(frozen=True, slots=True)
class _KnownIncompatibleStorage:
    layout: str
    projects: tuple["UnavailableProjectSummary", ...]


@dataclass(frozen=True, slots=True)
class UnavailableProjectSummary:
    """Describe one preserved Project that the current build cannot open."""

    project_id: str
    display_name: str
    previous_status: str
    updated_at: datetime
    message: str = UNAVAILABLE_PROJECT_MESSAGE


def prepare_incompatible_project_storage(
    root: str | Path,
) -> tuple[UnavailableProjectSummary, ...]:
    """Quarantine one known older root contract and return saved Project cards.

    Only the exact, previously shipped Recipe-root registry is eligible for the
    automatic path. Unknown entries, mismatched Project identities, and every
    unrecognized registry remain untouched for the existing compatibility guard.
    """

    storage_root = Path(root).resolve()
    storage_root.mkdir(parents=True, exist_ok=True)
    incompatible = _read_known_legacy_storage(storage_root)
    if incompatible is None:
        incompatible = _read_known_foundation_storage(storage_root)
    if incompatible is not None:
        plan = plan_development_reset(storage_root)
        if _plan_matches_known_root(plan, incompatible):
            _quarantine_known_root(plan, incompatible)
    return list_unavailable_projects(storage_root)


def list_unavailable_projects(
    root: str | Path,
) -> tuple[UnavailableProjectSummary, ...]:
    """Read bounded Project summaries written beside recoverable archives."""

    storage_root = Path(root).resolve()
    archive_root = storage_root / _ARCHIVE_DIRECTORY
    if not archive_root.is_dir():
        return ()
    by_project_id: dict[str, UnavailableProjectSummary] = {}
    archive_count = 0
    for archive in sorted(archive_root.iterdir(), key=lambda item: item.name):
        if archive_count >= _MAX_UNAVAILABLE_PROJECTS:
            break
        if not _is_direct_directory(archive_root, archive):
            continue
        manifest = archive / _MANIFEST_NAME
        if not manifest.is_file() or manifest.stat().st_size > _MAX_MANIFEST_BYTES:
            continue
        archive_count += 1
        for summary in _read_manifest(manifest):
            previous = by_project_id.get(summary.project_id)
            if previous is None or summary.updated_at > previous.updated_at:
                by_project_id[summary.project_id] = summary
            if len(by_project_id) >= _MAX_UNAVAILABLE_PROJECTS:
                break
    return tuple(
        sorted(
            by_project_id.values(),
            key=lambda item: (item.updated_at, item.project_id),
            reverse=True,
        )
    )


def _read_known_legacy_storage(
    root: Path,
) -> _KnownIncompatibleStorage | None:
    registry = root / "registry.duckdb"
    if not registry.is_file() or (root / "projects").exists():
        return None
    try:
        with duckdb.connect(str(registry), read_only=True) as connection:
            tables = {
                str(row[0])
                for row in connection.execute("SHOW TABLES").fetchall()
            }
            if not {"project_registry", "registry_schema_migration"}.issubset(
                tables
            ):
                return None
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info('project_registry')"
                ).fetchall()
            }
            if not _REQUIRED_LEGACY_PROJECT_COLUMNS.issubset(columns):
                return None
            marker = connection.execute(
                """
                SELECT checksum
                  FROM registry_schema_migration
                 WHERE migration_id = ?
                """,
                [LEGACY_RECIPE_ROOT_MIGRATION_ID],
            ).fetchone()
            if (
                marker is None
                or str(marker[0]) != LEGACY_RECIPE_ROOT_MIGRATION_CHECKSUM
            ):
                return None
            rows = connection.execute(
                """
                SELECT project_id, name, status, revision, updated_at
                  FROM project_registry
                 ORDER BY project_id
                 LIMIT ?
                """,
                [_MAX_UNAVAILABLE_PROJECTS + 1],
            ).fetchall()
    except (duckdb.Error, OSError):
        return None
    if len(rows) > _MAX_UNAVAILABLE_PROJECTS:
        return None
    summaries: list[UnavailableProjectSummary] = []
    for row in rows:
        summary = _summary_from_values(*row)
        if summary is None:
            return None
        summaries.append(summary)
    if len({item.project_id for item in summaries}) != len(summaries):
        return None
    return _KnownIncompatibleStorage(
        layout="LEGACY_RECIPE_ROOT",
        projects=tuple(summaries),
    )


def _read_known_foundation_storage(
    root: Path,
) -> _KnownIncompatibleStorage | None:
    registry = root / "registry.duckdb"
    projects_root = root / "projects"
    if not registry.is_file() or not projects_root.is_dir():
        return None
    try:
        with duckdb.connect(str(registry), read_only=True) as connection:
            tables = {
                str(row[0])
                for row in connection.execute("SHOW TABLES").fetchall()
            }
            if not {"migration_project", "schema_version"}.issubset(tables):
                return None
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info('migration_project')"
                ).fetchall()
            }
            if not _REQUIRED_FOUNDATION_PROJECT_COLUMNS.issubset(columns):
                return None
            version_row = connection.execute(
                """
                SELECT generation, version
                  FROM schema_version
                 WHERE singleton_id = 1
                """
            ).fetchone()
            if version_row is None:
                return None
            generation = str(version_row[0])
            version = int(version_row[1])
            if (
                not generation.startswith(_MIGRATION_REGISTRY_GENERATION_PREFIX)
                or generation == MIGRATION_REGISTRY_GENERATION
                or _KNOWN_PRIOR_FOUNDATION_VERSIONS.get(generation) != version
            ):
                return None
            rows = connection.execute(
                """
                SELECT project_id, display_name, status,
                       optimistic_revision, updated_at
                  FROM migration_project
                 ORDER BY project_id
                 LIMIT ?
                """,
                [_MAX_UNAVAILABLE_PROJECTS + 1],
            ).fetchall()
    except (duckdb.Error, OSError, TypeError, ValueError):
        return None
    if len(rows) > _MAX_UNAVAILABLE_PROJECTS:
        return None
    summaries: list[UnavailableProjectSummary] = []
    for row in rows:
        summary = _summary_from_values(*row)
        if summary is None:
            return None
        summaries.append(summary)
    if len({item.project_id for item in summaries}) != len(summaries):
        return None
    return _KnownIncompatibleStorage(
        layout="MIGRATION_FOUNDATION",
        projects=tuple(summaries),
    )


def _plan_matches_known_root(
    plan: DevelopmentResetPlan,
    incompatible: _KnownIncompatibleStorage,
) -> bool:
    if not plan.can_execute:
        return False
    target_names = {item.name for item in plan.targets}
    if "registry.duckdb" not in target_names:
        return False
    project_ids = {item.project_id for item in incompatible.projects}
    if incompatible.layout == "MIGRATION_FOUNDATION":
        if "projects" not in target_names:
            return False
        projects_root = plan.storage_root / "projects"
        directory_ids: set[str] = set()
        for entry in projects_root.iterdir():
            if not _is_direct_directory(projects_root, entry):
                return False
            canonical = _canonical_uuid(entry.name)
            if canonical is None:
                return False
            directory_ids.add(canonical)
        return directory_ids == project_ids
    if incompatible.layout != "LEGACY_RECIPE_ROOT" or "projects" in target_names:
        return False
    directory_ids: set[str] = set()
    for target in plan.targets:
        if not target.is_dir():
            continue
        canonical = _canonical_uuid(target.name)
        if canonical is None:
            continue
        if target.parent != plan.storage_root:
            return False
        database = target / "project.duckdb"
        if not database.is_file() or database.resolve().parent != target:
            return False
        directory_ids.add(canonical)
    return directory_ids == project_ids


def _quarantine_known_root(
    reviewed_plan: DevelopmentResetPlan,
    incompatible: _KnownIncompatibleStorage,
) -> Path:
    current = plan_development_reset(reviewed_plan.storage_root)
    if current.fingerprint != reviewed_plan.fingerprint:
        raise MigrationFoundationError(
            "Project storage changed while Impodo was preserving older projects"
        )
    if not _plan_matches_known_root(current, incompatible):
        raise MigrationFoundationError(
            "Older Project storage no longer matches the reviewed safe plan"
        )
    archive_id = str(uuid4())
    quarantine = current.storage_root / _ARCHIVE_DIRECTORY / archive_id
    quarantine.mkdir(parents=True, exist_ok=False)
    manifest = quarantine / _MANIFEST_NAME
    moved: list[tuple[Path, Path]] = []
    try:
        manifest.write_text(
            _manifest_text(archive_id, incompatible.projects),
            encoding="utf-8",
        )
        for source in current.targets:
            if source.parent != current.storage_root:
                raise MigrationFoundationError(
                    "Older Project storage target escaped its root"
                )
            destination = quarantine / source.name
            shutil.move(str(source), str(destination))
            moved.append((source, destination))
    except Exception:
        for source, destination in reversed(moved):
            if destination.exists() and not source.exists():
                shutil.move(str(destination), str(source))
        if manifest.exists():
            manifest.unlink()
        if quarantine.exists() and not any(quarantine.iterdir()):
            quarantine.rmdir()
        raise
    return quarantine


def _manifest_text(
    archive_id: str,
    projects: tuple[UnavailableProjectSummary, ...],
) -> str:
    payload = {
        "archive_id": archive_id,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "format_version": _MANIFEST_VERSION,
        "reason": "OLDER_PROJECT_DATA_CONTRACT",
        "projects": [
            {
                "display_name": item.display_name,
                "previous_status": item.previous_status,
                "project_id": item.project_id,
                "updated_at": item.updated_at.isoformat(),
            }
            for item in projects
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _read_manifest(path: Path) -> tuple[UnavailableProjectSummary, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ()
    if (
        not isinstance(payload, dict)
        or payload.get("format_version") != _MANIFEST_VERSION
    ):
        return ()
    raw_projects = payload.get("projects")
    if (
        not isinstance(raw_projects, list)
        or len(raw_projects) > _MAX_UNAVAILABLE_PROJECTS
    ):
        return ()
    summaries: list[UnavailableProjectSummary] = []
    for raw in raw_projects:
        if not isinstance(raw, dict):
            return ()
        summary = _summary_from_values(
            raw.get("project_id"),
            raw.get("display_name"),
            raw.get("previous_status"),
            1,
            raw.get("updated_at"),
        )
        if summary is None:
            return ()
        summaries.append(summary)
    return tuple(summaries)


def _summary_from_values(
    project_id: object,
    display_name: object,
    previous_status: object,
    revision: object,
    updated_at: object,
) -> UnavailableProjectSummary | None:
    canonical = _canonical_uuid(project_id)
    name = str(display_name).strip() if display_name is not None else ""
    status = str(previous_status).strip() if previous_status is not None else ""
    try:
        parsed_revision = int(revision)
        parsed_updated_at = datetime.fromisoformat(str(updated_at))
    except (TypeError, ValueError):
        return None
    if (
        canonical is None
        or not name
        or len(name) > 300
        or status not in _KNOWN_PROJECT_STATUSES
        or parsed_revision < 1
        or parsed_updated_at.tzinfo is None
    ):
        return None
    return UnavailableProjectSummary(
        project_id=canonical,
        display_name=name,
        previous_status=status,
        updated_at=parsed_updated_at,
    )


def _canonical_uuid(value: object) -> str | None:
    text = str(value)
    try:
        canonical = str(UUID(text))
    except (AttributeError, TypeError, ValueError):
        return None
    return canonical if canonical == text else None


def _is_direct_directory(parent: Path, candidate: Path) -> bool:
    return (
        candidate.is_dir()
        and candidate.resolve() == candidate
        and candidate.parent == parent
    )
