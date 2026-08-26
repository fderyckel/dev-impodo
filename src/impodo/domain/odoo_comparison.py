"""Protected contracts for pinned Odoo baseline/proposed/current evidence.

These objects are deliberately excluded from portable reports.  They contain
target-local numeric identifiers and business values and must therefore be
application-encrypted before persistence.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
import json
from typing import Any, Mapping
from uuid import UUID

from impodo.domain.shared.models import canonical_json_text, portable_value, restore_portable_value
from .serialization import content_hash


ODOO_COMPARISON_CONTRACT_VERSION = 2


class OdooComparisonError(ValueError):
    """Raised when protected Odoo comparison evidence is invalid."""


class OdooComparisonOutcome(StrEnum):
    """One fail-closed row result for a pinned Odoo record."""

    UNCHANGED = "UNCHANGED"
    UPDATE = "UPDATE"
    RECORD_REMOVED_OR_INACCESSIBLE = "RECORD_REMOVED_OR_INACCESSIBLE"
    CONCURRENT_FIELD_CHANGE = "CONCURRENT_FIELD_CHANGE"
    BASELINE_NOT_CAPTURED = "BASELINE_NOT_CAPTURED"
    TARGET_SCHEMA_CHANGED = "TARGET_SCHEMA_CHANGED"


class OdooFieldComparisonOutcome(StrEnum):
    """How one approved write field contributed to its row result."""

    UNCHANGED = "UNCHANGED"
    UPDATE = "UPDATE"
    CONCURRENT_CHANGE = "CONCURRENT_CHANGE"
    EXTERNAL_CHANGE_NOT_WRITTEN = "EXTERNAL_CHANGE_NOT_WRITTEN"
    BASELINE_MISSING = "BASELINE_MISSING"
    SCHEMA_CHANGED = "SCHEMA_CHANGED"


@dataclass(frozen=True, slots=True)
class OdooFieldComparison:
    """Protected values and result for one approved scalar field."""

    field: str
    field_type: str
    baseline: object
    proposed: object
    current: object
    outcome: OdooFieldComparisonOutcome

    def __post_init__(self) -> None:
        if not self.field or len(self.field) > 200:
            raise OdooComparisonError("Odoo comparison field is invalid")
        if not self.field_type or len(self.field_type) > 50:
            raise OdooComparisonError("Odoo comparison field type is invalid")
        object.__setattr__(self, "outcome", OdooFieldComparisonOutcome(self.outcome))

    def to_dict(self) -> dict[str, object]:
        return {
            "baseline": portable_value(self.baseline),
            "current": portable_value(self.current),
            "field": self.field,
            "field_type": self.field_type,
            "outcome": self.outcome.value,
            "proposed": portable_value(self.proposed),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OdooFieldComparison":
        _require_keys(
            payload,
            {"baseline", "current", "field", "field_type", "outcome", "proposed"},
        )
        return cls(
            field=str(payload["field"]),
            field_type=str(payload["field_type"]),
            baseline=restore_portable_value(payload["baseline"]),
            proposed=restore_portable_value(payload["proposed"]),
            current=restore_portable_value(payload["current"]),
            outcome=OdooFieldComparisonOutcome(str(payload["outcome"])),
        )


@dataclass(frozen=True, slots=True)
class OdooComparisonRow:
    """Protected exact-ID comparison for one prepared execution row."""

    source_row_ordinal: int
    source_trace_id: str
    odoo_id: int
    captured_write_date: datetime | None
    current_write_date: datetime | None
    outcome: OdooComparisonOutcome
    fields: tuple[OdooFieldComparison, ...]
    unrelated_current_change: bool = False

    def __post_init__(self) -> None:
        if self.source_row_ordinal < 1:
            raise OdooComparisonError("Odoo comparison source row is invalid")
        if not self.source_trace_id.startswith("sha256:"):
            raise OdooComparisonError("Odoo comparison row hash is invalid")
        if self.odoo_id < 1:
            raise OdooComparisonError("Odoo comparison record ID is invalid")
        for value in (self.captured_write_date, self.current_write_date):
            if value is not None and value.tzinfo is None:
                raise OdooComparisonError(
                    "Odoo comparison write timestamps must be timezone-aware"
                )
        object.__setattr__(self, "outcome", OdooComparisonOutcome(self.outcome))
        if tuple(sorted(self.fields, key=lambda item: item.field)) != self.fields:
            raise OdooComparisonError("Odoo comparison fields are not canonical")

    def to_dict(self) -> dict[str, object]:
        return {
            "captured_write_date": _datetime_text(self.captured_write_date),
            "current_write_date": _datetime_text(self.current_write_date),
            "fields": [item.to_dict() for item in self.fields],
            "odoo_id": self.odoo_id,
            "outcome": self.outcome.value,
            "source_row_ordinal": self.source_row_ordinal,
            "source_trace_id": self.source_trace_id,
            "unrelated_current_change": self.unrelated_current_change,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OdooComparisonRow":
        _require_keys(
            payload,
            {
                "captured_write_date", "current_write_date", "fields", "odoo_id",
                "outcome", "source_row_ordinal", "source_trace_id",
                "unrelated_current_change",
            },
        )
        raw_fields = payload["fields"]
        if not isinstance(raw_fields, list):
            raise OdooComparisonError("Odoo comparison fields are invalid")
        return cls(
            source_row_ordinal=int(payload["source_row_ordinal"]),
            source_trace_id=str(payload["source_trace_id"]),
            odoo_id=int(payload["odoo_id"]),
            captured_write_date=_parse_datetime(payload["captured_write_date"]),
            current_write_date=_parse_datetime(payload["current_write_date"]),
            outcome=OdooComparisonOutcome(str(payload["outcome"])),
            fields=tuple(OdooFieldComparison.from_dict(item) for item in raw_fields),
            unrelated_current_change=bool(payload["unrelated_current_change"]),
        )


@dataclass(frozen=True, slots=True)
class OdooComparisonArtifact:
    """Hash-bound protected evidence for one read-only comparison run."""

    run_id: str
    workspace_id: str
    capture_manifest_hash: str
    frozen_input_hash: str
    model: str
    connection_target_hash: str
    schema_scope_hash: str
    read_principal_hash: str
    context_hash: str
    checked_at: datetime
    rows: tuple[OdooComparisonRow, ...]
    content_hash: str
    contract_version: int = ODOO_COMPARISON_CONTRACT_VERSION
    _calculate_content_hash: InitVar[bool] = False

    def __post_init__(self, _calculate_content_hash: bool) -> None:
        if self.contract_version != ODOO_COMPARISON_CONTRACT_VERSION:
            raise OdooComparisonError("Unsupported Odoo comparison contract")
        for value, label in ((self.run_id, "run"), (self.workspace_id, "project")):
            try:
                UUID(value)
            except (ValueError, AttributeError) as error:
                raise OdooComparisonError(
                    f"Odoo comparison {label} ID is invalid"
                ) from error
        for value in (
            self.capture_manifest_hash,
            self.frozen_input_hash,
            self.connection_target_hash,
            self.schema_scope_hash,
            self.read_principal_hash,
            self.context_hash,
        ):
            if not value.startswith("sha256:") or len(value) != 71:
                raise OdooComparisonError("Odoo comparison binding hash is invalid")
        if not self.model or self.checked_at.tzinfo is None:
            raise OdooComparisonError("Odoo comparison header is invalid")
        if tuple(sorted(self.rows, key=lambda item: item.source_row_ordinal)) != self.rows:
            raise OdooComparisonError("Odoo comparison rows are not canonical")
        if len({item.source_trace_id for item in self.rows}) != len(self.rows):
            raise OdooComparisonError("Odoo comparison rows are duplicated")
        expected = content_hash(self._semantic_dict())
        if _calculate_content_hash:
            if self.content_hash:
                raise OdooComparisonError("New Odoo comparison already has a hash")
            object.__setattr__(self, "content_hash", expected)
        elif self.content_hash != expected:
            raise OdooComparisonError("Odoo comparison content hash is invalid")

    @classmethod
    def create(cls, **values: Any) -> "OdooComparisonArtifact":
        return cls(content_hash="", _calculate_content_hash=True, **values)

    @property
    def counts(self) -> dict[str, int]:
        return {
            outcome.value: sum(item.outcome is outcome for item in self.rows)
            for outcome in OdooComparisonOutcome
        }

    def _semantic_dict(self) -> dict[str, object]:
        return {
            "capture_manifest_hash": self.capture_manifest_hash,
            "checked_at": _datetime_text(self.checked_at),
            "connection_target_hash": self.connection_target_hash,
            "context_hash": self.context_hash,
            "contract_version": self.contract_version,
            "frozen_input_hash": self.frozen_input_hash,
            "model": self.model,
            "workspace_id": self.workspace_id,
            "read_principal_hash": self.read_principal_hash,
            "rows": [item.to_dict() for item in self.rows],
            "run_id": self.run_id,
            "schema_scope_hash": self.schema_scope_hash,
        }

    def to_json(self) -> str:
        return canonical_json_text(
            {**self._semantic_dict(), "content_hash": self.content_hash}
        )

    @classmethod
    def from_json(cls, value: str) -> "OdooComparisonArtifact":
        try:
            payload = json.loads(value)
            _require_keys(
                payload,
                {
                    "capture_manifest_hash", "checked_at", "connection_target_hash",
                    "content_hash", "context_hash", "contract_version",
                    "frozen_input_hash", "model", "workspace_id",
                    "read_principal_hash", "rows", "run_id", "schema_scope_hash",
                },
            )
            raw_rows = payload["rows"]
            if not isinstance(raw_rows, list):
                raise OdooComparisonError("Odoo comparison rows are invalid")
            return cls(
                run_id=str(payload["run_id"]),
                workspace_id=str(payload["workspace_id"]),
                capture_manifest_hash=str(payload["capture_manifest_hash"]),
                frozen_input_hash=str(payload["frozen_input_hash"]),
                model=str(payload["model"]),
                connection_target_hash=str(payload["connection_target_hash"]),
                schema_scope_hash=str(payload["schema_scope_hash"]),
                read_principal_hash=str(payload["read_principal_hash"]),
                context_hash=str(payload["context_hash"]),
                checked_at=datetime.fromisoformat(str(payload["checked_at"])),
                rows=tuple(OdooComparisonRow.from_dict(item) for item in raw_rows),
                content_hash=str(payload["content_hash"]),
                contract_version=int(payload["contract_version"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            if isinstance(error, OdooComparisonError):
                raise
            raise OdooComparisonError("Odoo comparison artifact is invalid") from error


def canonical_odoo_scalar(field_type: str, value: object) -> object:
    """Normalize captured, proposed, and live Tier-1 values identically."""

    if field_type == "boolean":
        if not isinstance(value, bool):
            raise OdooComparisonError("Odoo returned an invalid boolean value")
        return value
    if value is False or value is None:
        return None
    if field_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise OdooComparisonError("Odoo returned an invalid integer value")
        return value
    if field_type in {"char", "text", "selection"}:
        if not isinstance(value, str):
            raise OdooComparisonError("Odoo returned an invalid text value")
        return value
    if field_type == "date":
        if isinstance(value, datetime):
            raise OdooComparisonError("Odoo returned an invalid date value")
        if isinstance(value, date):
            return value
        if not isinstance(value, str):
            raise OdooComparisonError("Odoo returned an invalid date value")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise OdooComparisonError("Odoo returned an invalid date value") from error
        if parsed.isoformat() != value:
            raise OdooComparisonError("Odoo returned an invalid date value")
        return parsed
    if field_type == "datetime":
        if isinstance(value, datetime):
            normalized = value
            if normalized.tzinfo is None:
                normalized = normalized.replace(tzinfo=timezone.utc)
            return normalized.astimezone(timezone.utc)
        if not isinstance(value, str):
            raise OdooComparisonError("Odoo returned an invalid datetime value")
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as error:
            raise OdooComparisonError("Odoo returned an invalid datetime value") from error
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    raise OdooComparisonError("Odoo comparison field type is unsupported")


def canonical_write_date(value: object) -> datetime | None:
    """Normalize Odoo's nullable UTC write timestamp."""

    if value is False or value is None:
        return None
    normalized = canonical_odoo_scalar("datetime", value)
    assert isinstance(normalized, datetime)
    return normalized


def _datetime_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return canonical_write_date(value)


def _require_keys(payload: object, expected: set[str]) -> None:
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise OdooComparisonError("Odoo comparison contract shape is invalid")
