"""Contracts for bounded Odoo-source selection before live record capture.

These contracts contain no credentials and no numeric Odoo record, user,
company, or group identifiers. ``OdooCaptureSelection`` is protected project
evidence rather than a portable export contract; later capture slices bind its
hash to row data and a separately authorized protected origin sidecar.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import datetime
from enum import StrEnum
import json
import re
from uuid import UUID

from .odoo_source_policy import (
    CURRENT_ODOO_SOURCE_POLICY,
    ODOO_SOURCE_POLICY_HASH,
)
from .serialization import canonical_json, content_hash
from .source_binding import OdooSourceBinding, SourceOriginKind


ODOO_CAPTURE_CONTRACT_VERSION = 4
MAX_ODOO_CAPTURE_FIELDS = CURRENT_ODOO_SOURCE_POLICY.max_fields
MAX_ODOO_CAPTURE_ROWS = CURRENT_ODOO_SOURCE_POLICY.max_rows
ODOO_CAPTURE_PAGE_SIZE = CURRENT_ODOO_SOURCE_POLICY.page_size
ODOO_CAPTURE_PAGE_SIZES = (10, 100, ODOO_CAPTURE_PAGE_SIZE)
ODOO_CAPTURE_FIELD_TYPES = frozenset(
    CURRENT_ODOO_SOURCE_POLICY.capture_field_types
)

_DATASET_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")
_TECHNICAL_NAME = re.compile(r"[a-z_][a-z0-9_.]{0,127}")
_FIELD_NAME = re.compile(r"[a-z_][a-z0-9_]{0,127}")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")


class OdooCaptureContractError(ValueError):
    """Raised when Odoo-source selection evidence is malformed or widened."""


class OdooCaptureFilterPolicy(StrEnum):
    """Current closed filter choices; arbitrary caller-supplied domains are absent."""

    ALL_MATCHING_RECORDS = "ALL_MATCHING_RECORDS"
    ACTIVE_RECORDS = "ACTIVE_RECORDS"
    ACTIVE_AND_ARCHIVED_RECORDS = "ACTIVE_AND_ARCHIVED_RECORDS"


class OdooCaptureConsistency(StrEnum):
    """Honest native-API consistency level implemented by the next reader slice."""

    KEYSET_HIGH_WATER_INTERVAL = "KEYSET_HIGH_WATER_INTERVAL"


class OdooCaptureFilterOperator(StrEnum):
    """Closed semantic operators translated to Odoo domains by the adapter."""

    EQUALS = "EQUALS"
    IN_SET = "IN_SET"
    ON_OR_AFTER = "ON_OR_AFTER"
    AFTER = "AFTER"
    ON_OR_BEFORE = "ON_OR_BEFORE"
    BEFORE = "BEFORE"


@dataclass(frozen=True, slots=True)
class OdooCaptureFilterClause:
    """One bounded direct-field predicate; no raw domain syntax is accepted."""

    field_name: str
    operator: OdooCaptureFilterOperator
    values: tuple[bool | int | str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "operator", OdooCaptureFilterOperator(self.operator))
        values = tuple(self.values)
        if _FIELD_NAME.fullmatch(self.field_name) is None:
            raise OdooCaptureContractError("Odoo capture filter field is invalid")
        if not values or any(
            not isinstance(value, (bool, int, str)) for value in values
        ):
            raise OdooCaptureContractError("Odoo capture filter value is invalid")
        if self.operator is OdooCaptureFilterOperator.IN_SET:
            if len(values) > CURRENT_ODOO_SOURCE_POLICY.max_filter_set_members:
                raise OdooCaptureContractError(
                    "Odoo capture filter set exceeds the current limit"
                )
        elif len(values) != 1:
            raise OdooCaptureContractError(
                "Odoo capture filter operator requires exactly one value"
            )
        object.__setattr__(self, "values", values)

    def to_dict(self) -> dict[str, object]:
        return {
            "field_name": self.field_name,
            "operator": self.operator.value,
            "values": list(self.values),
        }

    @classmethod
    def from_dict(cls, value: object) -> OdooCaptureFilterClause:
        if not isinstance(value, dict) or set(value) != {
            "field_name",
            "operator",
            "values",
        }:
            raise OdooCaptureContractError(
                "Odoo capture filter clause shape is invalid"
            )
        raw_values = value["values"]
        if not isinstance(raw_values, list):
            raise OdooCaptureContractError("Odoo capture filter values are invalid")
        return cls(
            field_name=str(value["field_name"]),
            operator=OdooCaptureFilterOperator(value["operator"]),
            values=tuple(raw_values),
        )


@dataclass(frozen=True, slots=True)
class OdooCaptureSelection:
    """One immutable, bounded Odoo-source capture plan.

    The current contract intentionally exposes no arbitrary Odoo domain. Its
    only filter decision is whether archived rows join the active rows; later
    filter variants require a new contract version and explicit validation.
    """

    selection_id: str
    version: int
    data_version_id: str
    dataset_name: str
    model: str
    field_names: tuple[str, ...]
    filter_clauses: tuple[OdooCaptureFilterClause, ...]
    filter_policy: OdooCaptureFilterPolicy
    max_rows: int
    page_size: int
    consistency: OdooCaptureConsistency
    policy_hash: str
    connection_target_hash: str
    schema_scope_hash: str
    read_principal_hash: str
    read_permission_hash: str
    context_hash: str
    created_at: datetime
    created_by: str
    content_hash: str
    contract_version: int = ODOO_CAPTURE_CONTRACT_VERSION
    _calculate_content_hash: InitVar[bool] = False

    def __post_init__(self, _calculate_content_hash: bool) -> None:
        if self.contract_version != ODOO_CAPTURE_CONTRACT_VERSION:
            raise OdooCaptureContractError(
                "Unsupported Odoo capture selection contract version"
            )
        for value, label in (
            (self.selection_id, "selection ID"),
            (self.data_version_id, "DataVersion ID"),
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
        clauses = tuple(self.filter_clauses)
        if (
            len(clauses) > CURRENT_ODOO_SOURCE_POLICY.max_filter_clauses
            or tuple(sorted(clauses, key=lambda item: item.field_name)) != clauses
            or len({item.field_name for item in clauses}) != len(clauses)
            or any(item.field_name in {"id", "write_date"} for item in clauses)
            or len(
                canonical_json([item.to_dict() for item in clauses]).encode("utf-8")
            )
            > CURRENT_ODOO_SOURCE_POLICY.max_filter_bytes
        ):
            raise OdooCaptureContractError(
                "Odoo capture filters must be sorted, unique, direct, and bounded"
            )
        object.__setattr__(self, "filter_clauses", clauses)
        if not 1 <= self.max_rows <= MAX_ODOO_CAPTURE_ROWS:
            raise OdooCaptureContractError(
                f"Odoo capture row limit must be between 1 and "
                f"{MAX_ODOO_CAPTURE_ROWS}"
            )
        if self.page_size not in ODOO_CAPTURE_PAGE_SIZES:
            raise OdooCaptureContractError(
                "Odoo capture batch size must be 10, 100, or 500 records"
            )
        for value, label in (
            (self.policy_hash, "policy hash"),
            (self.connection_target_hash, "connection target hash"),
            (self.schema_scope_hash, "schema scope hash"),
            (self.read_principal_hash, "read principal hash"),
            (self.read_permission_hash, "read permission hash"),
            (self.context_hash, "context hash"),
        ):
            _require_hash(value, label)
        if self.policy_hash != ODOO_SOURCE_POLICY_HASH:
            raise OdooCaptureContractError(
                "Odoo capture selection does not use the current source policy"
            )
        if self.created_at.tzinfo is None:
            raise OdooCaptureContractError(
                "Odoo capture creation time must be timezone-aware"
            )
        if not self.created_by.strip() or len(self.created_by) > 256:
            raise OdooCaptureContractError("Odoo capture actor is invalid")
        expected_content_hash = content_hash(self._semantic_dict())
        if _calculate_content_hash:
            if self.content_hash:
                raise OdooCaptureContractError(
                    "New Odoo capture selection already has a content hash"
                )
            object.__setattr__(self, "content_hash", expected_content_hash)
        else:
            _require_hash(self.content_hash, "content hash")
        if self.content_hash != expected_content_hash:
            raise OdooCaptureContractError(
                "Odoo capture selection content hash is invalid"
            )

    @classmethod
    def create(
        cls,
        *,
        selection_id: str,
        version: int,
        data_version_id: str,
        dataset_name: str,
        model: str,
        field_names: tuple[str, ...],
        filter_clauses: tuple[OdooCaptureFilterClause, ...] = (),
        filter_policy: OdooCaptureFilterPolicy,
        max_rows: int,
        page_size: int = ODOO_CAPTURE_PAGE_SIZE,
        connection_target_hash: str,
        schema_scope_hash: str,
        read_principal_hash: str,
        read_permission_hash: str,
        context_hash: str,
        created_at: datetime,
        created_by: str,
    ) -> OdooCaptureSelection:
        return cls(
            selection_id=selection_id,
            version=version,
            data_version_id=data_version_id,
            dataset_name=dataset_name,
            model=model,
            field_names=field_names,
            filter_clauses=filter_clauses,
            filter_policy=filter_policy,
            max_rows=max_rows,
            page_size=page_size,
            consistency=OdooCaptureConsistency.KEYSET_HIGH_WATER_INTERVAL,
            policy_hash=ODOO_SOURCE_POLICY_HASH,
            connection_target_hash=connection_target_hash,
            schema_scope_hash=schema_scope_hash,
            read_principal_hash=read_principal_hash,
            read_permission_hash=read_permission_hash,
            context_hash=context_hash,
            created_at=created_at,
            created_by=created_by,
            content_hash="",
            _calculate_content_hash=True,
        )

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
            policy_hash=self.policy_hash,
        )

    @property
    def dataset_id(self) -> str:
        """Return the stable project/model slot identity used by later mapping."""

        return odoo_dataset_id(self.data_version_id, self.model)

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
            "filter_clauses": [item.to_dict() for item in self.filter_clauses],
            "filter_policy": self.filter_policy.value,
            "max_rows": self.max_rows,
            "model": self.model,
            "page_size": self.page_size,
            "policy_hash": self.policy_hash,
            "data_version_id": self.data_version_id,
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
                    "data_version_id",
                    "dataset_name",
                    "model",
                    "field_names",
                    "filter_clauses",
                    "filter_policy",
                    "max_rows",
                    "page_size",
                    "consistency",
                    "policy_hash",
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
                data_version_id=str(payload["data_version_id"]),
                dataset_name=str(payload["dataset_name"]),
                model=str(payload["model"]),
                field_names=tuple(str(item) for item in payload["field_names"]),
                filter_clauses=tuple(
                    OdooCaptureFilterClause.from_dict(item)
                    for item in payload["filter_clauses"]
                ),
                filter_policy=OdooCaptureFilterPolicy(payload["filter_policy"]),
                max_rows=int(payload["max_rows"]),
                page_size=int(payload["page_size"]),
                consistency=OdooCaptureConsistency(payload["consistency"]),
                policy_hash=str(payload["policy_hash"]),
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


def odoo_dataset_id(data_version_id: str, model: str, *, slot: str = "primary") -> str:
    """Derive a stable dataset identity without exposing a numeric Odoo ID."""

    try:
        UUID(data_version_id)
    except (AttributeError, ValueError) as error:
        raise OdooCaptureContractError(
            "Odoo dataset DataVersion ID is invalid"
        ) from error
    if _TECHNICAL_NAME.fullmatch(model) is None or not slot:
        raise OdooCaptureContractError("Odoo dataset binding is invalid")
    digest = content_hash(
        {
            "kind": SourceOriginKind.ODOO.value,
            "model": model,
            "data_version_id": data_version_id,
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


def _require_hash(value: str, label: str) -> None:
    if _HASH.fullmatch(value) is None:
        raise OdooCaptureContractError(f"Odoo capture {label} is invalid")


def _require_exact_keys(value: object, expected: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise OdooCaptureContractError(
            "Odoo capture selection fields do not match the current contract"
        )
