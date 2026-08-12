"""Contracts for bounded Odoo-source selection before live record capture.

These contracts contain no credentials and no numeric Odoo record, user,
company, or group identifiers. ``OdooCaptureSelection`` is protected project
evidence rather than a portable export contract; later capture slices bind its
hash to row data and a separately authorized protected origin sidecar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import json
import re
from uuid import UUID

from .serialization import canonical_json, content_hash
from .source_binding import OdooSourceBinding, SourceOriginKind


ODOO_CAPTURE_CONTRACT_VERSION = 1
MAX_ODOO_CAPTURE_FIELDS = 50
MAX_ODOO_CAPTURE_ROWS = 10_000
ODOO_CAPTURE_PAGE_SIZE = 500
ODOO_CAPTURE_FIELD_TYPES = frozenset(
    {"boolean", "char", "date", "datetime", "integer", "selection", "text"}
)

_DATASET_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")
_TECHNICAL_NAME = re.compile(r"[a-z_][a-z0-9_.]{0,127}")
_FIELD_NAME = re.compile(r"[a-z_][a-z0-9_]{0,127}")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")


class OdooCaptureContractError(ValueError):
    """Raised when Odoo-source selection evidence is malformed or widened."""


class OdooCaptureFilterPolicy(StrEnum):
    """Current closed filter choices; arbitrary caller-supplied domains are absent."""

    ACTIVE_RECORDS = "ACTIVE_RECORDS"
    ACTIVE_AND_ARCHIVED_RECORDS = "ACTIVE_AND_ARCHIVED_RECORDS"


class OdooCaptureConsistency(StrEnum):
    """Honest native-API consistency level implemented by the next reader slice."""

    KEYSET_HIGH_WATER_INTERVAL = "KEYSET_HIGH_WATER_INTERVAL"


@dataclass(frozen=True, slots=True)
class OdooCaptureSelection:
    """One immutable, bounded Odoo-source capture plan.

    The current contract intentionally exposes no arbitrary Odoo domain. Its
    only filter decision is whether archived rows join the active rows; later
    filter variants require a new contract version and explicit validation.
    """

    selection_id: str
    version: int
    project_id: str
    dataset_name: str
    model: str
    field_names: tuple[str, ...]
    filter_policy: OdooCaptureFilterPolicy
    max_rows: int
    page_size: int
    consistency: OdooCaptureConsistency
    connection_target_hash: str
    schema_scope_hash: str
    read_principal_hash: str
    read_permission_hash: str
    context_hash: str
    created_at: datetime
    created_by: str
    content_hash: str
    contract_version: int = ODOO_CAPTURE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != ODOO_CAPTURE_CONTRACT_VERSION:
            raise OdooCaptureContractError(
                "Unsupported Odoo capture selection contract version"
            )
        for value, label in (
            (self.selection_id, "selection ID"),
            (self.project_id, "project ID"),
        ):
            try:
                UUID(value)
            except (AttributeError, ValueError) as error:
                raise OdooCaptureContractError(
                    f"Odoo capture {label} is invalid"
                ) from error
        if self.version < 1:
            raise OdooCaptureContractError(
                "Odoo capture selection version must be positive"
            )
        if _DATASET_NAME.fullmatch(self.dataset_name) is None:
            raise OdooCaptureContractError("Odoo capture dataset name is invalid")
        if _TECHNICAL_NAME.fullmatch(self.model) is None:
            raise OdooCaptureContractError("Odoo capture model is invalid")
        if (
            not self.field_names
            or len(self.field_names) > MAX_ODOO_CAPTURE_FIELDS
            or self.field_names != tuple(sorted(set(self.field_names)))
            or any(_FIELD_NAME.fullmatch(item) is None for item in self.field_names)
            or "id" in self.field_names
            or "write_date" in self.field_names
        ):
            raise OdooCaptureContractError(
                "Odoo capture fields must be sorted, unique, bounded technical "
                "names without protected identity fields"
            )
        if not 1 <= self.max_rows <= MAX_ODOO_CAPTURE_ROWS:
            raise OdooCaptureContractError(
                f"Odoo capture row limit must be between 1 and "
                f"{MAX_ODOO_CAPTURE_ROWS}"
            )
        if self.page_size != ODOO_CAPTURE_PAGE_SIZE:
            raise OdooCaptureContractError(
                "Odoo capture page size is not the fixed bounded value"
            )
        for value, label in (
            (self.connection_target_hash, "connection target hash"),
            (self.schema_scope_hash, "schema scope hash"),
            (self.read_principal_hash, "read principal hash"),
            (self.read_permission_hash, "read permission hash"),
            (self.context_hash, "context hash"),
            (self.content_hash, "content hash"),
        ):
            _require_hash(value, label)
        if self.created_at.tzinfo is None:
            raise OdooCaptureContractError(
                "Odoo capture creation time must be timezone-aware"
            )
        if not self.created_by.strip() or len(self.created_by) > 256:
            raise OdooCaptureContractError("Odoo capture actor is invalid")
        if self.content_hash != self.expected_content_hash:
            raise OdooCaptureContractError(
                "Odoo capture selection content hash is invalid"
            )

    @classmethod
    def create(
        cls,
        *,
        selection_id: str,
        version: int,
        project_id: str,
        dataset_name: str,
        model: str,
        field_names: tuple[str, ...],
        filter_policy: OdooCaptureFilterPolicy,
        max_rows: int,
        connection_target_hash: str,
        schema_scope_hash: str,
        read_principal_hash: str,
        read_permission_hash: str,
        context_hash: str,
        created_at: datetime,
        created_by: str,
    ) -> OdooCaptureSelection:
        values: dict[str, object] = {
            "contract_version": ODOO_CAPTURE_CONTRACT_VERSION,
            "connection_target_hash": connection_target_hash,
            "consistency": OdooCaptureConsistency.KEYSET_HIGH_WATER_INTERVAL,
            "context_hash": context_hash,
            "dataset_name": dataset_name,
            "field_names": field_names,
            "filter_policy": filter_policy,
            "max_rows": max_rows,
            "model": model,
            "page_size": ODOO_CAPTURE_PAGE_SIZE,
            "project_id": project_id,
            "read_permission_hash": read_permission_hash,
            "read_principal_hash": read_principal_hash,
            "schema_scope_hash": schema_scope_hash,
            "selection_id": selection_id,
            "version": version,
        }
        return cls(
            selection_id=selection_id,
            version=version,
            project_id=project_id,
            dataset_name=dataset_name,
            model=model,
            field_names=field_names,
            filter_policy=filter_policy,
            max_rows=max_rows,
            page_size=ODOO_CAPTURE_PAGE_SIZE,
            consistency=OdooCaptureConsistency.KEYSET_HIGH_WATER_INTERVAL,
            connection_target_hash=connection_target_hash,
            schema_scope_hash=schema_scope_hash,
            read_principal_hash=read_principal_hash,
            read_permission_hash=read_permission_hash,
            context_hash=context_hash,
            created_at=created_at,
            created_by=created_by,
            content_hash=content_hash(values),
        )

    @property
    def expected_content_hash(self) -> str:
        return content_hash(self._semantic_dict())

    @property
    def source_binding(self) -> OdooSourceBinding:
        return OdooSourceBinding(
            capture_selection_hash=self.content_hash,
            model=self.model,
            connection_target_hash=self.connection_target_hash,
            schema_scope_hash=self.schema_scope_hash,
            read_principal_hash=self.read_principal_hash,
            read_permission_hash=self.read_permission_hash,
            context_hash=self.context_hash,
        )

    @property
    def dataset_id(self) -> str:
        """Return the stable project/model slot identity used by later mapping."""

        return odoo_dataset_id(self.project_id, self.model)

    @property
    def column_stable_keys(self) -> tuple[str, ...]:
        """Return stable technical column identities in selected-field order."""

        return tuple(
            odoo_column_stable_key(self.model, field_name)
            for field_name in self.field_names
        )

    def _semantic_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "connection_target_hash": self.connection_target_hash,
            "consistency": self.consistency.value,
            "context_hash": self.context_hash,
            "dataset_name": self.dataset_name,
            "field_names": list(self.field_names),
            "filter_policy": self.filter_policy.value,
            "max_rows": self.max_rows,
            "model": self.model,
            "page_size": self.page_size,
            "project_id": self.project_id,
            "read_permission_hash": self.read_permission_hash,
            "read_principal_hash": self.read_principal_hash,
            "schema_scope_hash": self.schema_scope_hash,
            "selection_id": self.selection_id,
            "version": self.version,
        }

    def to_json(self) -> str:
        return canonical_json(
            {
                **self._semantic_dict(),
                "content_hash": self.content_hash,
                "created_at": self.created_at.isoformat(),
                "created_by": self.created_by,
            }
        )

    @classmethod
    def from_json(cls, value: str) -> OdooCaptureSelection:
        try:
            payload = json.loads(value)
            _require_exact_keys(
                payload,
                {
                    "selection_id",
                    "version",
                    "project_id",
                    "dataset_name",
                    "model",
                    "field_names",
                    "filter_policy",
                    "max_rows",
                    "page_size",
                    "consistency",
                    "connection_target_hash",
                    "schema_scope_hash",
                    "read_principal_hash",
                    "read_permission_hash",
                    "context_hash",
                    "created_at",
                    "created_by",
                    "content_hash",
                    "contract_version",
                },
            )
            selection = cls(
                selection_id=str(payload["selection_id"]),
                version=int(payload["version"]),
                project_id=str(payload["project_id"]),
                dataset_name=str(payload["dataset_name"]),
                model=str(payload["model"]),
                field_names=tuple(str(item) for item in payload["field_names"]),
                filter_policy=OdooCaptureFilterPolicy(payload["filter_policy"]),
                max_rows=int(payload["max_rows"]),
                page_size=int(payload["page_size"]),
                consistency=OdooCaptureConsistency(payload["consistency"]),
                connection_target_hash=str(payload["connection_target_hash"]),
                schema_scope_hash=str(payload["schema_scope_hash"]),
                read_principal_hash=str(payload["read_principal_hash"]),
                read_permission_hash=str(payload["read_permission_hash"]),
                context_hash=str(payload["context_hash"]),
                created_at=datetime.fromisoformat(str(payload["created_at"])),
                created_by=str(payload["created_by"]),
                content_hash=str(payload["content_hash"]),
                contract_version=int(payload["contract_version"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            if isinstance(error, OdooCaptureContractError):
                raise
            raise OdooCaptureContractError(
                "Odoo capture selection is invalid"
            ) from error
        return selection


def odoo_dataset_id(project_id: str, model: str, *, slot: str = "primary") -> str:
    """Derive a stable dataset identity without exposing a numeric Odoo ID."""

    try:
        UUID(project_id)
    except (AttributeError, ValueError) as error:
        raise OdooCaptureContractError("Odoo dataset project ID is invalid") from error
    if _TECHNICAL_NAME.fullmatch(model) is None or not slot:
        raise OdooCaptureContractError("Odoo dataset binding is invalid")
    digest = content_hash(
        {
            "kind": SourceOriginKind.ODOO.value,
            "model": model,
            "project_id": project_id,
            "slot": slot,
        }
    ).removeprefix("sha256:")
    return f"dataset:{digest[:24]}"


def odoo_column_stable_key(model: str, field_name: str) -> str:
    """Derive a stable Odoo column key from technical names, never ordinals."""

    if (
        _TECHNICAL_NAME.fullmatch(model) is None
        or _FIELD_NAME.fullmatch(field_name) is None
    ):
        raise OdooCaptureContractError("Odoo column binding is invalid")
    digest = content_hash(
        {"field": field_name, "kind": SourceOriginKind.ODOO.value, "model": model}
    ).removeprefix("sha256:")
    return f"odoo-column:{digest[:24]}"


def _require_hash(value: str, label: str, *, allow_bare: bool = False) -> None:
    candidate = value if value.startswith("sha256:") else f"sha256:{value}"
    if _HASH.fullmatch(candidate) is None or (not allow_bare and candidate != value):
        raise OdooCaptureContractError(f"Odoo capture {label} is invalid")


def _require_exact_keys(value: object, expected: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise OdooCaptureContractError(
            "Odoo capture selection fields do not match the current contract"
        )
