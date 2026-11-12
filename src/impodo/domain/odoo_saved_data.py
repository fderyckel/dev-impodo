"""Define immutable Odoo capture configurations and retained data references.

Saved data definitions contain reusable capture meaning. Versions reference
existing DataVersion evidence instead of copying business records into the
registry. Optimistic revisions and configuration revisions have separate jobs:
publishing data advances the former without changing capture meaning.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime

from impodo.domain.project.foundation import (
    MigrationFoundationError,
    require_aware,
    require_hash,
    require_revision,
    require_uuid,
    required_text,
)
from impodo.domain.run.setup import OdooConnectionMode, validate_odoo_base_url
from impodo.domain.serialization import canonical_json, content_hash

MAX_SAVED_ODOO_CONFIG_BYTES = 2 * 1024 * 1024
MAX_SAVED_ODOO_MODELS = 100
_MODEL = re.compile(r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*\Z")
_FORBIDDEN_CONFIG_KEYS = frozenset(
    {"api_key", "password", "secret", "credentials", "access_token", "refresh_token"}
)


def _validate_config(value: object, *, depth: int = 0) -> None:
    """Reject credentials and excessive nesting in reusable configuration."""

    if depth > 20:
        raise MigrationFoundationError("Saved Odoo configuration is nested too deeply")
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in _FORBIDDEN_CONFIG_KEYS:
                raise MigrationFoundationError(
                    "Saved Odoo configuration cannot contain credentials"
                )
            _validate_config(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _validate_config(item, depth=depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise MigrationFoundationError(
            "Saved Odoo configuration contains a non-finite number"
        )


def _reject_json_constant(value: str) -> object:
    raise MigrationFoundationError(f"Saved Odoo configuration contains {value}")


def _canonical_config(encoded: str) -> str:
    if len(encoded.encode("utf-8")) > MAX_SAVED_ODOO_CONFIG_BYTES:
        raise MigrationFoundationError(
            "Saved Odoo configuration exceeds its size limit"
        )
    try:
        value = json.loads(encoded, parse_constant=_reject_json_constant)
    except (TypeError, ValueError, RecursionError) as error:
        raise MigrationFoundationError("Saved Odoo configuration is invalid") from error
    if not isinstance(value, dict):
        raise MigrationFoundationError("Saved Odoo configuration must be an object")
    _validate_config(value)
    return canonical_json(value)


@dataclass(frozen=True, slots=True)
class SavedOdooDataDefinition:
    """Keep one source database/company configuration and its current version."""

    saved_data_id: str
    project_id: str
    label: str
    revision: int
    source_workspace_id: str
    source_target_hash: str
    source_company_hash: str
    odoo_major: int
    config_json: str
    latest_version: int | None
    created_at: datetime
    updated_at: datetime
    configuration_revision: int = 1

    def __post_init__(self) -> None:
        for name in ("saved_data_id", "project_id", "source_workspace_id"):
            require_uuid(getattr(self, name), name)
        require_revision(self.revision)
        require_revision(self.configuration_revision, "configuration_revision")
        if self.configuration_revision > self.revision:
            raise MigrationFoundationError(
                "Configuration revision exceeds saved revision"
            )
        require_hash(self.source_target_hash, "source_target_hash")
        require_hash(self.source_company_hash, "source_company_hash")
        if (
            not isinstance(self.odoo_major, int)
            or isinstance(self.odoo_major, bool)
            or self.odoo_major not in {19, 20}
        ):
            raise MigrationFoundationError(
                "Saved Odoo data requires Odoo 19 or Odoo 20"
            )
        object.__setattr__(
            self, "label", required_text(self.label, "label", maximum=300)
        )
        if self.latest_version is not None:
            require_revision(self.latest_version, "latest_version")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise MigrationFoundationError("Saved Odoo update predates its creation")
        object.__setattr__(self, "config_json", _canonical_config(self.config_json))

    @property
    def content_hash(self) -> str:
        """Bind every stored definition value to a deterministic integrity hash."""

        return content_hash(asdict(self))


@dataclass(frozen=True, slots=True)
class SavedOdooDataVersion:
    """Reference one complete immutable set of captured Odoo model artifacts."""

    saved_data_id: str
    project_id: str
    version_number: int
    configuration_revision: int
    workspace_id: str
    data_version_id: str
    source_selection_hash: str
    manifest_hashes: tuple[tuple[str, str], ...]
    row_counts: tuple[tuple[str, int], ...]
    captured_at: datetime
    operation_id: str

    def __post_init__(self) -> None:
        for name in (
            "saved_data_id",
            "project_id",
            "workspace_id",
            "data_version_id",
            "operation_id",
        ):
            require_uuid(getattr(self, name), name)
        require_revision(self.version_number, "version_number")
        require_revision(self.configuration_revision, "configuration_revision")
        require_hash(self.source_selection_hash, "source_selection_hash")
        require_aware(self.captured_at, "captured_at")
        models = tuple(model for model, _ in self.manifest_hashes)
        if (
            not models
            or len(models) > MAX_SAVED_ODOO_MODELS
            or models != tuple(sorted(set(models)))
            or any(_MODEL.fullmatch(model) is None for model in models)
            or tuple(model for model, _ in self.row_counts) != models
        ):
            raise MigrationFoundationError(
                "Saved Odoo version model coverage is invalid"
            )
        for _model, manifest_hash in self.manifest_hashes:
            require_hash(manifest_hash, "manifest_hash")
        if any(
            isinstance(count, bool) or not isinstance(count, int) or count < 0
            for _model, count in self.row_counts
        ):
            raise MigrationFoundationError("Saved Odoo version row counts are invalid")

    @property
    def content_hash(self) -> str:
        """Identify the complete retained version without storing its rows."""

        return content_hash(asdict(self))


@dataclass(frozen=True, slots=True)
class SavedOdooRestoreBinding:
    """Bind one fresh restore workspace to retained source evidence."""

    project_id: str
    workspace_id: str
    saved_data_id: str
    version_number: int
    source_workspace_id: str
    data_version_id: str
    generation_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "project_id",
            "workspace_id",
            "saved_data_id",
            "source_workspace_id",
            "data_version_id",
            "generation_id",
        ):
            require_uuid(getattr(self, name), name)
        if self.workspace_id == self.source_workspace_id:
            raise MigrationFoundationError("Restoration requires a fresh workspace")
        require_revision(self.version_number, "version_number")
        require_aware(self.created_at, "created_at")

    @property
    def content_hash(self) -> str:
        """Bind the exact immutable source and fresh restore generation."""

        return content_hash(asdict(self))


@dataclass(frozen=True, slots=True)
class SavedOdooRestoreProfile:
    """Remember portable destination choices without retaining a target approval."""

    profile_id: str
    project_id: str
    saved_data_id: str
    label: str
    revision: int
    connection_mode: str
    base_url: str
    database: str
    company_name: str
    credential_workspace_id: str | None
    settings_json: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for name in ("profile_id", "project_id", "saved_data_id"):
            require_uuid(getattr(self, name), name)
        if self.credential_workspace_id is not None:
            require_uuid(self.credential_workspace_id, "credential_workspace_id")
        require_revision(self.revision)
        mode = OdooConnectionMode(self.connection_mode)
        object.__setattr__(self, "connection_mode", mode.value)
        object.__setattr__(
            self, "base_url", validate_odoo_base_url(self.base_url, mode)
        )
        for name in ("label", "database", "company_name"):
            object.__setattr__(
                self, name, required_text(getattr(self, name), name, maximum=300)
            )
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise MigrationFoundationError(
                "Restore profile update predates its creation"
            )
        object.__setattr__(self, "settings_json", _canonical_config(self.settings_json))

    @property
    def content_hash(self) -> str:
        """Bind the exact remembered destination and portable policy settings."""

        return content_hash(asdict(self))
