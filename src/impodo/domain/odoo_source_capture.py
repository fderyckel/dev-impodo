"""Closed contracts for bounded live Odoo-source reads.

The request is built only from protected selection and schema evidence.  Pages
are columnar and page-bounded; this module deliberately defines no raw Odoo
domain, method, arbitrary context, row JSON, or per-row digest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import re
from typing import Callable
from uuid import UUID

from ..workspace_contracts import OdooSchemaCatalog, SchemaField
from .odoo_capture import (
    ODOO_CAPTURE_FIELD_TYPES,
    OdooCaptureConsistency,
    OdooCaptureFilterClause,
    OdooCaptureFilterOperator,
    OdooCaptureFilterPolicy,
    OdooCaptureSelection,
)
from .odoo_provenance import OdooOriginBatch
from .odoo_source_policy import (
    CURRENT_ODOO_SOURCE_POLICY,
    ODOO_SOURCE_POLICY_HASH,
    TargetInstanceAssurance,
)
from .serialization import canonical_json


CaptureScalar = bool | int | str | date | datetime | None
CancellationProbe = Callable[[], bool]

_FIELD_NAME = re.compile(r"[a-z_][a-z0-9_]{0,127}")
_MODEL_NAME = re.compile(r"[a-z_][a-z0-9_.]{0,127}")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")


class OdooSourceCaptureError(RuntimeError):
    """Base error safe to show without response bodies or business values."""


class OdooSourceCaptureConfigurationError(OdooSourceCaptureError):
    """The governed request or adapter configuration is invalid."""


class OdooSourceCaptureLimitError(OdooSourceCaptureError):
    """A fixed request, value, row, page, or snapshot limit was exceeded."""


class OdooSourceCaptureConsistencyError(OdooSourceCaptureError):
    """A response or end probe no longer matches the bound capture contract."""


class OdooSourceCaptureCancelled(OdooSourceCaptureError):
    """Capture stopped at a bounded cancellation checkpoint."""


@dataclass(frozen=True, slots=True)
class OdooCaptureFieldProjection:
    """One typed direct business-value column in an exact projection."""

    name: str
    field_type: str

    def __post_init__(self) -> None:
        if (
            _FIELD_NAME.fullmatch(self.name) is None
            or self.name in {"id", "write_date"}
            or self.field_type not in ODOO_CAPTURE_FIELD_TYPES
        ):
            raise OdooSourceCaptureConfigurationError(
                "Odoo capture projection is invalid"
            )


@dataclass(frozen=True, slots=True)
class OdooSourceCaptureRequest:
    """Service-generated, immutable request accepted by the capture port."""

    data_version_id: str
    selection_id: str
    selection_version: int
    selection_hash: str
    policy_hash: str
    model: str
    projection: tuple[OdooCaptureFieldProjection, ...]
    filter_clauses: tuple[OdooCaptureFilterClause, ...]
    filter_policy: OdooCaptureFilterPolicy
    schema_model_names: tuple[str, ...]
    maximum_rows: int
    page_size: int
    max_sample_rows: int
    max_request_bytes: int
    max_response_bytes: int
    max_value_bytes: int
    max_row_bytes: int
    max_snapshot_bytes: int
    expected_connection_target_hash: str
    expected_schema_scope_hash: str
    expected_read_principal_hash: str
    expected_read_permission_hash: str
    expected_context_hash: str
    consistency: OdooCaptureConsistency
    target_instance_assurance: TargetInstanceAssurance

    def __post_init__(self) -> None:
        policy = CURRENT_ODOO_SOURCE_POLICY
        try:
            UUID(self.data_version_id)
            UUID(self.selection_id)
        except (AttributeError, ValueError) as error:
            raise OdooSourceCaptureConfigurationError(
                "Odoo capture request identity is invalid"
            ) from error
        if (
            isinstance(self.selection_version, bool)
            or not isinstance(self.selection_version, int)
            or self.selection_version < 1
            or not isinstance(self.model, str)
            or _MODEL_NAME.fullmatch(self.model) is None
            or any(
                not isinstance(value, str) or _HASH.fullmatch(value) is None
                for value in (
                    self.selection_hash,
                    self.policy_hash,
                    self.expected_connection_target_hash,
                    self.expected_schema_scope_hash,
                    self.expected_read_principal_hash,
                    self.expected_read_permission_hash,
                    self.expected_context_hash,
                )
            )
        ):
            raise OdooSourceCaptureConfigurationError(
                "Odoo capture request binding is invalid"
            )
        integer_limits = (
            self.maximum_rows,
            self.page_size,
            self.max_sample_rows,
            self.max_request_bytes,
            self.max_response_bytes,
            self.max_value_bytes,
            self.max_row_bytes,
            self.max_snapshot_bytes,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in integer_limits
        ):
            raise OdooSourceCaptureConfigurationError(
                "Odoo capture request limits are invalid"
            )
        if (
            self.policy_hash != ODOO_SOURCE_POLICY_HASH
            or not isinstance(self.filter_policy, OdooCaptureFilterPolicy)
            or self.consistency
            is not OdooCaptureConsistency.KEYSET_HIGH_WATER_INTERVAL
            or self.page_size != policy.page_size
            or self.maximum_rows < 1
            or self.maximum_rows > policy.max_rows
            or not 1 <= self.max_sample_rows <= policy.max_sample_rows
            or not 1 <= self.max_request_bytes <= policy.max_request_bytes
            or not 1 <= self.max_response_bytes <= policy.max_response_bytes
            or not 1 <= self.max_value_bytes <= policy.max_value_bytes
            or not 1 <= self.max_row_bytes <= policy.max_row_bytes
            or not 1 <= self.max_snapshot_bytes <= policy.max_snapshot_bytes
            or self.target_instance_assurance is not policy.target_instance_assurance
        ):
            raise OdooSourceCaptureConfigurationError(
                "Odoo capture request does not match the current policy"
            )
        projection = tuple(self.projection)
        if (
            not projection
            or len(projection) > policy.max_fields
            or any(
                not isinstance(item, OdooCaptureFieldProjection)
                for item in projection
            )
            or tuple(item.name for item in projection)
            != tuple(sorted({item.name for item in projection}))
        ):
            raise OdooSourceCaptureConfigurationError(
                "Odoo capture projection must be sorted and unique"
            )
        clauses = tuple(self.filter_clauses)
        if (
            len(clauses) > policy.max_filter_clauses
            or any(
                not isinstance(item, OdooCaptureFilterClause) for item in clauses
            )
            or tuple(sorted(clauses, key=lambda item: item.field_name)) != clauses
            or len({item.field_name for item in clauses}) != len(clauses)
            or len(
                canonical_json([item.to_dict() for item in clauses]).encode("utf-8")
            )
            > policy.max_filter_bytes
        ):
            raise OdooSourceCaptureConfigurationError(
                "Odoo capture request filters are invalid"
            )
        schema_model_names = tuple(self.schema_model_names)
        if (
            not schema_model_names
            or any(
                not isinstance(item, str) or _MODEL_NAME.fullmatch(item) is None
                for item in schema_model_names
            )
            or schema_model_names != tuple(sorted(set(schema_model_names)))
            or self.model not in schema_model_names
        ):
            raise OdooSourceCaptureConfigurationError(
                "Odoo capture schema scope is invalid"
            )
        object.__setattr__(self, "projection", projection)
        object.__setattr__(self, "filter_clauses", clauses)
        object.__setattr__(self, "schema_model_names", schema_model_names)

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.projection)

@dataclass(frozen=True, slots=True)
class OdooCaptureValueColumn:
    """One typed, page-sized source-value column."""

    field_name: str
    field_type: str
    values: tuple[CaptureScalar, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.field_name, str)
            or _FIELD_NAME.fullmatch(self.field_name) is None
            or self.field_type not in ODOO_CAPTURE_FIELD_TYPES
            or any(
                not _capture_value_matches_type(self.field_type, value)
                for value in self.values
            )
        ):
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture value column is invalid"
            )
        object.__setattr__(self, "values", tuple(self.values))


@dataclass(frozen=True, slots=True)
class OdooCapturePage:
    """Validated page with protected origins separated from value columns."""

    first_row_ordinal: int
    odoo_ids: tuple[int, ...]
    write_dates: tuple[datetime | None, ...]
    columns: tuple[OdooCaptureValueColumn, ...]
    response_bytes: int
    normalized_bytes: int

    def __post_init__(self) -> None:
        row_count = len(self.odoo_ids)
        if not row_count or row_count > CURRENT_ODOO_SOURCE_POLICY.page_size:
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture page is empty or exceeds the fixed page size"
            )
        if len(self.write_dates) != row_count or any(
            len(column.values) != row_count for column in self.columns
        ):
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture page columns have different lengths"
            )
        column_names = tuple(column.field_name for column in self.columns)
        if not column_names or column_names != tuple(sorted(set(column_names))):
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture page value columns are invalid"
            )
        # Reuse the protected provenance contract for the exact origin checks.
        OdooOriginBatch(
            first_row_ordinal=self.first_row_ordinal,
            odoo_ids=self.odoo_ids,
            write_dates=self.write_dates,
        )
        if self.response_bytes < 1 or self.normalized_bytes < 1:
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture page accounting is invalid"
            )

    @property
    def row_count(self) -> int:
        return len(self.odoo_ids)

    @property
    def origin_batch(self) -> OdooOriginBatch:
        return OdooOriginBatch(
            first_row_ordinal=self.first_row_ordinal,
            odoo_ids=self.odoo_ids,
            write_dates=self.write_dates,
        )


@dataclass(frozen=True, slots=True)
class OdooCaptureAccounting:
    """Honest interval/high-water accounting after a complete page stream."""

    high_water_id: int
    row_count: int
    page_count: int
    record_request_count: int
    response_bytes: int
    normalized_bytes: int
    capture_started_at: datetime
    capture_finished_at: datetime
    consistency: OdooCaptureConsistency
    target_instance_assurance: TargetInstanceAssurance
    consistency_limitation: str

    def __post_init__(self) -> None:
        if (
            self.high_water_id < 0
            or self.row_count < 0
            or self.row_count > CURRENT_ODOO_SOURCE_POLICY.max_rows
            or self.page_count < 0
            or self.page_count > self.row_count
            or self.record_request_count != self.page_count + 1
            or self.response_bytes < 1
            or self.normalized_bytes < 0
            or self.capture_started_at.tzinfo is None
            or self.capture_finished_at.tzinfo is None
            or self.capture_finished_at < self.capture_started_at
            or self.consistency
            is not OdooCaptureConsistency.KEYSET_HIGH_WATER_INTERVAL
            or self.target_instance_assurance
            is not CURRENT_ODOO_SOURCE_POLICY.target_instance_assurance
            or not self.consistency_limitation.strip()
        ):
            raise OdooSourceCaptureConsistencyError(
                "Odoo capture accounting is invalid"
            )


@dataclass(frozen=True, slots=True)
class OdooCaptureSample:
    """A bounded display preview that is never authoritative membership."""

    page: OdooCapturePage | None
    non_authoritative: bool = True


def plan_odoo_source_capture(
    selection: OdooCaptureSelection,
    schema: OdooSchemaCatalog,
) -> OdooSourceCaptureRequest:
    """Build the only request shape accepted by the live capture adapter."""

    if (
        selection.policy_hash != schema.policy_hash
        or selection.schema_scope_hash != schema.content_hash
        or selection.connection_target_hash != schema.connection_target_hash
        or selection.read_principal_hash != schema.read_principal_hash
        or selection.read_permission_hash != schema.read_permission_hash
        or selection.context_hash != schema.read_context_hash
    ):
        raise OdooSourceCaptureConfigurationError(
            "Odoo capture selection no longer matches the current schema evidence"
        )
    schema_model = next(
        (item for item in schema.models if item.name == selection.model),
        None,
    )
    if schema_model is None:
        raise OdooSourceCaptureConfigurationError(
            "Odoo capture model is outside the approved schema scope"
        )
    fields = {item.name: item for item in schema_model.fields}
    if not _eligible_write_date(fields.get("write_date")):
        raise OdooSourceCaptureConfigurationError(
            "Odoo capture requires eligible write_date metadata"
        )
    projection: list[OdooCaptureFieldProjection] = []
    for name in selection.field_names:
        field = fields.get(name)
        if not _eligible_projection(field):
            raise OdooSourceCaptureConfigurationError(
                f"Odoo field {name} is not eligible for Tier-1 capture"
            )
        assert field is not None
        projection.append(OdooCaptureFieldProjection(name, field.type))
    if selection.filter_policy is not OdooCaptureFilterPolicy.ALL_MATCHING_RECORDS:
        active = fields.get("active")
        if not _eligible_filter_field(active) or active.type != "boolean":
            raise OdooSourceCaptureConfigurationError(
                "Active/archive capture requires an eligible active field"
            )
    for clause in selection.filter_clauses:
        field = fields.get(clause.field_name)
        if not _eligible_filter_field(field):
            raise OdooSourceCaptureConfigurationError(
                f"Odoo filter field {clause.field_name} is not eligible"
            )
        assert field is not None
        _validate_filter_values(clause, field)
    policy = CURRENT_ODOO_SOURCE_POLICY
    return OdooSourceCaptureRequest(
        data_version_id=selection.data_version_id,
        selection_id=selection.selection_id,
        selection_version=selection.version,
        selection_hash=selection.content_hash,
        policy_hash=selection.policy_hash,
        model=selection.model,
        projection=tuple(projection),
        filter_clauses=selection.filter_clauses,
        filter_policy=selection.filter_policy,
        schema_model_names=tuple(sorted(item.name for item in schema.models)),
        maximum_rows=selection.max_rows,
        page_size=selection.page_size,
        max_sample_rows=policy.max_sample_rows,
        max_request_bytes=policy.max_request_bytes,
        max_response_bytes=policy.max_response_bytes,
        max_value_bytes=policy.max_value_bytes,
        max_row_bytes=policy.max_row_bytes,
        max_snapshot_bytes=policy.max_snapshot_bytes,
        expected_connection_target_hash=selection.connection_target_hash,
        expected_schema_scope_hash=selection.schema_scope_hash,
        expected_read_principal_hash=selection.read_principal_hash,
        expected_read_permission_hash=selection.read_permission_hash,
        expected_context_hash=selection.context_hash,
        consistency=selection.consistency,
        target_instance_assurance=policy.target_instance_assurance,
    )


def require_not_cancelled(probe: CancellationProbe | None) -> None:
    if probe is not None and probe():
        raise OdooSourceCaptureCancelled("Odoo source capture was cancelled")


def _eligible_projection(field: SchemaField | None) -> bool:
    return bool(
        field is not None
        and field.type in ODOO_CAPTURE_FIELD_TYPES
        and field.relation is None
        and field.stored is True
        and field.computed is False
        and field.related is False
        and field.translated is not None
        and field.company_dependent is False
        and field.exportable is True
    )


def _eligible_filter_field(field: SchemaField | None) -> bool:
    return bool(_eligible_projection(field) and field.searchable is True)


def _eligible_write_date(field: SchemaField | None) -> bool:
    return bool(
        field is not None
        and field.type == "datetime"
        and field.relation is None
        and field.stored is True
        and field.computed is False
        and field.related is False
        and field.company_dependent is False
    )


def _validate_filter_values(
    clause: OdooCaptureFilterClause,
    field: SchemaField,
) -> None:
    range_operators = {
        OdooCaptureFilterOperator.ON_OR_AFTER,
        OdooCaptureFilterOperator.AFTER,
        OdooCaptureFilterOperator.ON_OR_BEFORE,
        OdooCaptureFilterOperator.BEFORE,
    }
    if clause.operator in range_operators and field.type not in {"date", "datetime"}:
        raise OdooSourceCaptureConfigurationError(
            "Odoo range filters require a date or datetime field"
        )
    for value in clause.values:
        if field.type == "boolean":
            valid = isinstance(value, bool)
        elif field.type == "integer":
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif field.type in {"char", "text", "selection"}:
            valid = isinstance(value, str)
        elif field.type == "date":
            valid = isinstance(value, str) and _is_date(value)
        elif field.type == "datetime":
            valid = isinstance(value, str) and _is_datetime(value)
        else:
            valid = False
        if not valid:
            raise OdooSourceCaptureConfigurationError(
                f"Odoo filter value does not match field {field.name}"
            )
        if field.type == "selection" and value not in {
            item[0] for item in field.selection
        }:
            raise OdooSourceCaptureConfigurationError(
                f"Odoo selection filter value is invalid for field {field.name}"
            )


def _is_date(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _is_datetime(value: str) -> bool:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return parsed.tzinfo is None


def normalize_odoo_datetime(value: str) -> datetime:
    """Decode the exact Odoo UTC wire format without locale conversion."""

    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError) as error:
        raise OdooSourceCaptureConsistencyError(
            "Odoo returned an invalid datetime value"
        ) from error
    return parsed.replace(tzinfo=timezone.utc)


def _capture_value_matches_type(field_type: str, value: CaptureScalar) -> bool:
    if value is None:
        return True
    if field_type == "boolean":
        return isinstance(value, bool)
    if field_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if field_type in {"char", "text", "selection"}:
        return isinstance(value, str)
    if field_type == "date":
        return isinstance(value, date) and not isinstance(value, datetime)
    if field_type == "datetime":
        return isinstance(value, datetime) and value.tzinfo is not None
    return False
