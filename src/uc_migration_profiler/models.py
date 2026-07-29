"""Environment-independent domain values and deterministic serialization.

These dataclasses form the boundaries between source preparation, reference
resolution, comparison, and reporting. Portable source-side objects use
business keys instead of numeric Odoo IDs. `TargetRecord` is the deliberate
exception: it represents an environment-specific snapshot and must be
converted before entering the portable manifest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping


ScalarValue = str | int | Decimal | bool | date | datetime | None


class Classification(StrEnum):
    """Possible read-only conclusions for one import candidate."""

    CREATE = "CREATE"
    UPDATE = "UPDATE"
    UNCHANGED = "UNCHANGED"
    AMBIGUOUS = "AMBIGUOUS"
    BLOCKED = "BLOCKED"


class Severity(StrEnum):
    """Whether an issue permits comparison to continue."""

    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Issue:
    """Structured validation or resolution evidence.

    `affected_count` permits repeated row-level causes to be grouped for the
    report while preserving the business impact.
    """

    code: str
    message: str
    severity: Severity = Severity.ERROR
    dataset: str | None = None
    row: int | None = None
    field: str | None = None
    affected_count: int = 1

    @property
    def blocking(self) -> bool:
        """Return whether this issue prevents a safe candidate conclusion."""

        return self.severity == Severity.ERROR


@dataclass(frozen=True, slots=True)
class LogicalReference:
    """A portable reference intent awaiting resolution."""

    origin: str
    key: tuple[ScalarValue, ...]
    dataset: str | None = None
    model: str | None = None
    target_fields: tuple[str, ...] = ()
    scope: tuple[ScalarValue, ...] = ()


@dataclass(frozen=True, slots=True)
class BusinessReference:
    """A portable resolved relationship expressed through a governed key."""

    model: str
    key: tuple[ScalarValue, ...]
    scope: tuple[ScalarValue, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedRecord:
    """Canonical source candidate before/after symbolic resolution.

    `source.py` creates the record with `LogicalReference` values. `engine.py`
    returns a replaced copy containing `BusinessReference` values wherever
    resolution succeeded. Numeric target IDs never belong in this contract.
    """

    dataset: str
    source_row: int
    target_model: str
    source_identity: tuple[ScalarValue, ...]
    target_identity: tuple[ScalarValue | LogicalReference | BusinessReference, ...]
    target_scope: tuple[ScalarValue | LogicalReference | BusinessReference, ...]
    scalar_values: Mapping[str, ScalarValue]
    references: Mapping[
        str,
        LogicalReference
        | BusinessReference
        | tuple[LogicalReference | BusinessReference, ...]
        | None,
    ]
    issues: tuple[Issue, ...] = ()

    @property
    def blocked(self) -> bool:
        """Return whether any attached issue has error severity."""

        return any(issue.blocking for issue in self.issues)


@dataclass(frozen=True, slots=True)
class TargetRecord:
    """Environment-specific record captured from an Odoo snapshot."""

    model: str
    odoo_id: int
    values: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class EnvironmentFingerprint:
    """Evidence identifying the exact target environment capture."""

    environment: str
    database: str
    odoo_version: str
    snapshot_timestamp: str
    module_versions: Mapping[str, str] = field(default_factory=dict)

    def portable_dict(self) -> dict[str, Any]:
        """Return a deterministically ordered JSON-compatible representation."""

        return {
            "environment": self.environment,
            "database": self.database,
            "odoo_version": self.odoo_version,
            "snapshot_timestamp": self.snapshot_timestamp,
            "module_versions": dict(sorted(self.module_versions.items())),
        }


@dataclass(frozen=True, slots=True)
class FieldMetadata:
    """Captured Odoo field shape needed to validate a mapping."""

    name: str
    type: str
    required: bool = False
    readonly: bool = False
    relation: str | None = None
    relation_field: str | None = None
    selection: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ModelMetadata:
    """Captured metadata for one permitted Odoo model."""

    model: str
    description: str | None
    fields: Mapping[str, FieldMetadata]


@dataclass(frozen=True, slots=True)
class FieldDifference:
    """One material existing-versus-proposed field comparison."""

    dataset: str
    business_identity: tuple[Any, ...]
    business_scope: tuple[Any, ...]
    field: str
    existing: Any
    proposed: Any
    comparison_rule: str
    material: bool = True


@dataclass(frozen=True, slots=True)
class Decision:
    """Final preflight conclusion and evidence for one prepared source row."""

    dataset: str
    source_row: int
    business_identity: tuple[Any, ...]
    business_scope: tuple[Any, ...]
    classification: Classification
    target_match_count: int
    differences: tuple[FieldDifference, ...] = ()
    issues: tuple[Issue, ...] = ()


@dataclass(frozen=True, slots=True)
class ReferenceResolution:
    """Grouped-ready evidence for one attempted symbolic relationship."""

    dataset: str
    field: str
    reference: LogicalReference
    status: str
    match_count: int
    affected_count: int = 1


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """Canonical result from which JSON and workbook reports are generated."""

    profile_id: str
    source_hashes: Mapping[str, str]
    fingerprint: EnvironmentFingerprint
    metadata_snapshot_hash: str | None
    record_snapshot_hash: str | None
    decisions: tuple[Decision, ...]
    reference_resolutions: tuple[ReferenceResolution, ...]
    issues: tuple[Issue, ...]
    metadata_coverage: tuple[Mapping[str, Any], ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        """Count decisions by every classification, including zero counts."""

        return {
            classification.value: sum(
                1
                for decision in self.decisions
                if decision.classification == classification
            )
            for classification in Classification
        }

    @property
    def semantic_hash(self) -> str:
        """Hash the complete portable result payload excluding the hash itself."""

        payload = self.to_portable_dict(include_hash=False)
        return "sha256:" + sha256(canonical_json_bytes(payload)).hexdigest()

    def to_portable_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        """Serialize the result deterministically for manifests and hashing.

        Typed decimals/dates, business references, ordered evidence, and sorted
        mappings are preserved. A final recursive guard rejects numeric Odoo
        identifiers from the portable artifact.
        """

        payload: dict[str, Any] = {
            "engine": {"name": "uc-profiler"},
            "profile": {"id": self.profile_id},
            "source_hashes": dict(sorted(self.source_hashes.items())),
            "snapshot_hashes": {
                "metadata": self.metadata_snapshot_hash,
                "records": self.record_snapshot_hash,
            },
            "target_environment": self.fingerprint.portable_dict(),
            "summary": self.counts,
            "decisions": [
                {
                    "dataset": decision.dataset,
                    "source_row": decision.source_row,
                    "business_identity": portable_value(decision.business_identity),
                    "business_scope": portable_value(decision.business_scope),
                    "classification": decision.classification.value,
                    "target_match_count": decision.target_match_count,
                    "differences": [
                        {
                            "dataset": difference.dataset,
                            "business_identity": portable_value(
                                difference.business_identity
                            ),
                            "business_scope": portable_value(
                                difference.business_scope
                            ),
                            "field": difference.field,
                            "existing": portable_value(difference.existing),
                            "proposed": portable_value(difference.proposed),
                            "comparison_rule": difference.comparison_rule,
                            "material": difference.material,
                        }
                        for difference in decision.differences
                    ],
                    "issues": [portable_issue(issue) for issue in decision.issues],
                }
                for decision in self.decisions
            ],
            "reference_resolutions": [
                {
                    "dataset": resolution.dataset,
                    "field": resolution.field,
                    "reference": portable_value(resolution.reference),
                    "status": resolution.status,
                    "match_count": resolution.match_count,
                    "affected_count": resolution.affected_count,
                }
                for resolution in self.reference_resolutions
            ],
            "source_issues": [portable_issue(issue) for issue in self.issues],
            "metadata_coverage": [dict(item) for item in self.metadata_coverage],
        }
        if include_hash:
            payload["semantic_hash"] = self.semantic_hash
        assert_no_numeric_odoo_ids(payload)
        return payload


def portable_issue(issue: Issue) -> dict[str, Any]:
    """Convert structured issue evidence to JSON-compatible primitives."""

    return {
        "code": issue.code,
        "message": issue.message,
        "severity": issue.severity.value,
        "dataset": issue.dataset,
        "row": issue.row,
        "field": issue.field,
        "affected_count": issue.affected_count,
    }


def portable_value(value: Any) -> Any:
    """Recursively serialize typed and portable domain values.

    Decimals never pass through binary floating point, datetimes become UTC,
    sets receive canonical ordering, and business references retain keys and
    scope instead of target IDs.
    """

    if isinstance(value, Decimal):
        return {"type": "decimal", "value": format(value, "f")}
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is None:
            normalized = normalized.replace(tzinfo=timezone.utc)
        normalized = normalized.astimezone(timezone.utc)
        return {
            "type": "datetime",
            "value": normalized.isoformat().replace("+00:00", "Z"),
        }
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, BusinessReference):
        return {
            "model": value.model,
            "key": portable_value(value.key),
            "scope": portable_value(value.scope),
        }
    if isinstance(value, LogicalReference):
        result: dict[str, Any] = {
            "origin": value.origin,
            "key": portable_value(value.key),
            "scope": portable_value(value.scope),
        }
        if value.dataset is not None:
            result["dataset"] = value.dataset
        if value.model is not None:
            result["model"] = value.model
        if value.target_fields:
            result["target_fields"] = list(value.target_fields)
        return result
    if isinstance(value, tuple):
        return [portable_value(item) for item in value]
    if isinstance(value, list):
        return [portable_value(item) for item in value]
    if isinstance(value, set | frozenset):
        rendered = [portable_value(item) for item in value]
        return sorted(rendered, key=canonical_json_text)
    if isinstance(value, Mapping):
        return {
            str(key): portable_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    return value


def canonical_json_text(value: Any) -> str:
    """Return stable compact JSON text used for ordering and hashing."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Return UTF-8 bytes of `canonical_json_text`."""

    return canonical_json_text(value).encode("utf-8")


def assert_no_numeric_odoo_ids(value: Any, path: str = "$") -> None:
    """Reject environment-specific identifiers from portable artifacts."""

    forbidden = {"odoo_id", "odoo_ids", "record_id", "record_ids"}
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = str(key).casefold()
            if normalized_key in forbidden:
                raise ValueError(f"numeric Odoo identifier forbidden at {path}.{key}")
            assert_no_numeric_odoo_ids(item, f"{path}.{key}")
    elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            assert_no_numeric_odoo_ids(item, f"{path}[{index}]")
