"""Dataset-centric mapping contracts and pure semantic validation.

The local browser authors :class:`MappingDefinition` revisions against one
frozen source selection and one governed Odoo schema bundle.  This module has
no file, database, HTTP, or Odoo access.  It validates mapping meaning only;
row-level uniqueness, required values, and reference resolution remain
explicit deferred checks for staging and preflight.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
import json
import re
from typing import Any, Iterable, Mapping

from .metadata import TYPE_COMPATIBILITY
from .reference_keys import (
    matches_standard_reference_key,
    standard_reference_key,
)
from .value_rules import (
    CASE_MODES,
    CHARACTER_CLASSES,
    MAX_RULE_SIZE,
    ROUNDING_MODES,
    SEARCH_MODES,
    SEGMENT_LOCATIONS,
    ScalarRuleError,
    ScalarTransformPolicy,
    ScalarValidationPolicy,
    prepare_rule_text,
    round_decimal_value,
    validate_formula,
    validate_pattern,
    validate_scalar_value,
)


MAPPING_CONTRACT_VERSION = 6
MAPPING_VALIDATOR_VERSION = "6.0.0"
MAX_VALUE_MAPPINGS = 1_000
MAX_VALUE_MAPPING_LENGTH = 10_000
MAX_CONTROL_TOTALS_PER_DATASET = 3
_RELATION_TYPES = frozenset({"many2one", "many2many", "one2many"})
_VALUE_TYPES = frozenset(TYPE_COMPATIBILITY)
_NULL_POLICIES = frozenset(
    {"distinct", "equivalent", "ignore_source_null"}
)
_DECIMAL_LOCALES = frozenset({"invariant", "en_US", "de_DE", "fr_FR"})
_DATE_FORMATS = {
    "iso": "%Y-%m-%d",
    "dmy_slash": "%d/%m/%Y",
    "mdy_slash": "%m/%d/%Y",
    "dmy_dot": "%d.%m.%Y",
}
_DATETIME_FORMATS = {
    "iso": None,
    "dmy_slash": "%d/%m/%Y %H:%M:%S",
    "mdy_slash": "%m/%d/%Y %H:%M:%S",
    "dmy_dot": "%d.%m.%Y %H:%M:%S",
}


class MappingTargetMode(StrEnum):
    UPSERT = "upsert"
    CREATE = "create"
    REFERENCE = "reference"


class ResolverOrigin(StrEnum):
    DATASET = "dataset"
    TARGET_CATALOG = "target_catalog"


class BusinessKeyStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"


class MappingValidationStatus(StrEnum):
    VALID = "VALID"
    VALID_WITH_WARNINGS = "VALID_WITH_WARNINGS"
    INVALID = "INVALID"


class ScalarValueSource(StrEnum):
    """How one scalar target value is supplied."""

    SOURCE = "source"
    CONSTANT = "constant"
    SOURCE_WITH_FALLBACK = "source_with_fallback"
    ODOO_DEFAULT = "odoo_default"


@dataclass(frozen=True, slots=True)
class BusinessKeyDefinition:
    """Governed natural identity for one captured Odoo model."""

    key_id: str
    model: str
    key_fields: tuple[str, ...]
    scope_fields: tuple[str, ...] = ()
    description: str = ""
    status: BusinessKeyStatus = BusinessKeyStatus.CANDIDATE

    def __post_init__(self) -> None:
        key_id = self.key_id.strip()
        model = self.model.strip()
        description = self.description.strip()
        key_fields = tuple(item.strip() for item in self.key_fields)
        scope_fields = tuple(item.strip() for item in self.scope_fields)
        if not key_id or not model:
            raise ValueError("Business-key ID and model must not be blank")
        if len(key_id) > 200 or len(model) > 200:
            raise ValueError("Business-key ID or model is too long")
        if len(description) > 1000:
            raise ValueError("Business-key description is too long")
        if not key_fields:
            raise ValueError("A business key requires at least one key field")
        all_fields = (*key_fields, *scope_fields)
        if any(not item.strip() for item in all_fields):
            raise ValueError("Business-key fields must not be blank")
        if len(set(all_fields)) != len(all_fields):
            raise ValueError("Business-key fields and scope must be unique")
        object.__setattr__(self, "key_id", key_id)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "key_fields", key_fields)
        object.__setattr__(self, "scope_fields", scope_fields)
        object.__setattr__(self, "status", BusinessKeyStatus(self.status))


@dataclass(frozen=True, slots=True)
class SchemaGovernance:
    """Versioned model scope and business keys bound to a schema catalog."""

    governance_id: str
    version: int
    project_id: str
    catalog_hash: str
    permitted_models: tuple[str, ...]
    business_keys: tuple[BusinessKeyDefinition, ...]
    recorded_at: datetime
    recorded_by: str

    @property
    def content_hash(self) -> str:
        return _content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "governance_id": self.governance_id,
            "version": self.version,
            "project_id": self.project_id,
            "catalog_hash": self.catalog_hash,
            "permitted_models": list(self.permitted_models),
            "business_keys": [
                {
                    **asdict(item),
                    "status": item.status.value,
                }
                for item in self.business_keys
            ],
            "recorded_at": self.recorded_at.isoformat(),
            "recorded_by": self.recorded_by,
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "SchemaGovernance":
        payload = json.loads(value)
        result = cls(
            governance_id=str(payload["governance_id"]),
            version=int(payload["version"]),
            project_id=str(payload["project_id"]),
            catalog_hash=str(payload["catalog_hash"]),
            permitted_models=tuple(payload["permitted_models"]),
            business_keys=tuple(
                BusinessKeyDefinition(
                    key_id=str(item["key_id"]),
                    model=str(item["model"]),
                    key_fields=tuple(item["key_fields"]),
                    scope_fields=tuple(item.get("scope_fields", ())),
                    description=str(item.get("description", "")),
                    status=BusinessKeyStatus(item["status"]),
                )
                for item in payload["business_keys"]
            ),
            recorded_at=datetime.fromisoformat(payload["recorded_at"]),
            recorded_by=str(payload["recorded_by"]),
        )
        if payload.get("content_hash") != result.content_hash:
            raise ValueError("Schema-governance content hash is invalid")
        return result


@dataclass(frozen=True, slots=True)
class ReferenceKeyMapping:
    source_column_key: str
    target_field: str


@dataclass(frozen=True, slots=True)
class ValueMapping:
    """Map one visible source choice to one portable Odoo choice."""

    source_value: str
    target_value: str

    def __post_init__(self) -> None:
        if (
            not self.source_value
            or len(self.source_value) > MAX_VALUE_MAPPING_LENGTH
        ):
            raise ValueError("Source choice is invalid")
        if (
            not self.target_value
            or len(self.target_value) > MAX_VALUE_MAPPING_LENGTH
        ):
            raise ValueError("Odoo choice is invalid")


def _normalized_value_mappings(
    mappings: tuple[ValueMapping, ...],
) -> tuple[ValueMapping, ...]:
    if len(mappings) > MAX_VALUE_MAPPINGS:
        raise ValueError(
            f"Value matching is limited to {MAX_VALUE_MAPPINGS} choices"
        )
    ordered = tuple(sorted(mappings, key=lambda item: item.source_value))
    if len({item.source_value for item in ordered}) != len(ordered):
        raise ValueError("Each source choice can be matched only once")
    return ordered


@dataclass(frozen=True, slots=True)
class RelationshipResolver:
    origin: ResolverOrigin
    dataset_id: str | None = None
    model: str | None = None
    key_mappings: tuple[ReferenceKeyMapping, ...] = ()
    scope_mappings: tuple[ReferenceKeyMapping, ...] = ()
    value_mappings: tuple[ValueMapping, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "origin", ResolverOrigin(self.origin))
        object.__setattr__(
            self,
            "value_mappings",
            _normalized_value_mappings(self.value_mappings),
        )


@dataclass(frozen=True, slots=True)
class IdentityComponentMapping:
    source_column_keys: tuple[str, ...]
    target_fields: tuple[str, ...]
    value_type: str = "string"
    resolver: RelationshipResolver | None = None


@dataclass(frozen=True, slots=True)
class ScalarFieldMapping:
    target_field: str
    source_column_key: str | None = None
    value_source: ScalarValueSource = ScalarValueSource.SOURCE
    literal_value: str | None = None
    transform: ScalarTransformPolicy = field(
        default_factory=ScalarTransformPolicy
    )
    validation: ScalarValidationPolicy = field(
        default_factory=ScalarValidationPolicy
    )
    value_mappings: tuple[ValueMapping, ...] = ()
    value_type: str = "string"
    required: bool = False
    required_on_create: bool = False
    compare: bool = True
    validate_only: bool = False
    null_policy: str = "distinct"

    def __post_init__(self) -> None:
        if not self.target_field.strip() or len(self.target_field) > 200:
            raise ValueError("Scalar target field is invalid")
        if (
            self.source_column_key is not None
            and len(self.source_column_key) > 500
        ):
            raise ValueError("Scalar source-column key is too long")
        if self.literal_value is not None and len(self.literal_value) > 10_000:
            raise ValueError("Scalar literal value is too long")
        object.__setattr__(
            self,
            "value_source",
            ScalarValueSource(self.value_source),
        )
        object.__setattr__(
            self,
            "value_mappings",
            _normalized_value_mappings(self.value_mappings),
        )


class ScalarValueError(ValueError):
    """Raised when a governed scalar value cannot be canonicalized."""


class ScalarValueRuleError(ScalarValueError):
    """Raised when a row fails one configured transformation or check."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def evaluate_scalar_mapping_value(
    mapping: ScalarFieldMapping,
    raw_source_value: Any,
    *,
    source_values_by_ordinal: Mapping[int, Any] | None = None,
) -> str | int | Decimal | bool | date | datetime | None:
    """Evaluate one scalar through the shared preview/runtime boundary."""

    return canonicalize_scalar_value(
        mapping,
        raw_source_value,
        formula_context={
            "value": raw_source_value,
            **{
                f"column_{ordinal}": value
                for ordinal, value in sorted(
                    (source_values_by_ordinal or {}).items()
                )
            },
        },
    )


def canonicalize_scalar_value(
    mapping: ScalarFieldMapping,
    raw_source_value: Any,
    *,
    formula_context: Mapping[str, Any] | None = None,
) -> str | int | Decimal | bool | date | datetime | None:
    """Apply one browser-authored value provider and transformation policy."""

    if mapping.value_source is ScalarValueSource.ODOO_DEFAULT:
        raise ScalarValueError("Odoo-default fields have no local proposed value")
    source_choice = (
        str(raw_source_value).strip()
        if raw_source_value is not None
        else None
    )
    matched_value = next(
        (
            item.target_value
            for item in mapping.value_mappings
            if item.source_value == source_choice
        ),
        None,
    )
    if matched_value is not None:
        prepared = matched_value
    else:
        if mapping.value_source is ScalarValueSource.CONSTANT:
            raw_value = mapping.literal_value
        elif mapping.value_source is ScalarValueSource.SOURCE_WITH_FALLBACK:
            prepared_source = _transform_scalar_text(
                raw_source_value,
                mapping.transform,
            )
            raw_value = (
                mapping.literal_value
                if prepared_source is None
                else prepared_source
            )
        else:
            raw_value = raw_source_value

        try:
            rule_context = dict(formula_context or {})
            rule_context["value"] = raw_value
            prepared = prepare_rule_text(
                raw_value,
                mapping.transform,
                formula_context=rule_context,
            )
        except ScalarRuleError as error:
            raise ScalarValueRuleError(error.code, str(error)) from error
        if prepared is None:
            if mapping.required:
                raise ScalarValueError(
                    "Required value is empty after transformation"
                )
            return None

    try:
        if mapping.value_type == "string":
            result: Any = prepared
        elif mapping.value_type == "integer":
            if not re.fullmatch(r"[+-]?\d+", prepared):
                raise ValueError
            result = int(prepared, 10)
        elif mapping.value_type == "decimal":
            result = _parse_decimal(prepared, mapping.transform.decimal_locale)
        elif mapping.value_type == "boolean":
            token = prepared.casefold()
            if token in {"true", "1", "yes", "y"}:
                result = True
            elif token in {"false", "0", "no", "n"}:
                result = False
            else:
                raise ValueError
        elif mapping.value_type == "date":
            result = datetime.strptime(
                prepared,
                _DATE_FORMATS[mapping.transform.date_format],
            ).date()
        elif mapping.value_type == "datetime":
            date_format = _DATETIME_FORMATS[mapping.transform.date_format]
            parsed = (
                datetime.fromisoformat(prepared.replace("Z", "+00:00"))
                if date_format is None
                else datetime.strptime(prepared, date_format)
            )
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            result = parsed.astimezone(timezone.utc)
        else:
            raise ScalarValueError(
                f"Unsupported canonical value type {mapping.value_type!r}."
            )
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise ScalarValueError(
            f"Cannot parse {prepared!r} as {mapping.value_type}."
        ) from error
    try:
        if isinstance(result, Decimal):
            result = round_decimal_value(result, mapping.transform)
        validate_scalar_value(result, mapping.validation)
    except ScalarRuleError as error:
        raise ScalarValueRuleError(error.code, str(error)) from error
    return result


def _transform_scalar_text(
    raw_value: Any,
    policy: ScalarTransformPolicy,
) -> str | None:
    if raw_value is None:
        return None
    value = str(raw_value)
    if policy.trim:
        value = value.strip()
    if policy.collapse_whitespace:
        value = re.sub(r"\s+", " ", value)
    if policy.case_mode == "uppercase":
        value = value.upper()
    elif policy.case_mode == "lowercase":
        value = value.lower()
    elif policy.case_mode == "sentence":
        value = next(
            (
                f"{value[:index]}{character.upper()}{value[index + 1:]}"
                for index, character in enumerate(value)
                if character.isalpha()
            ),
            value,
        )
    elif policy.case_mode == "title":
        value = value.title()
    if policy.empty_as_null and value == "":
        return None
    return value


def _parse_decimal(value: str, locale: str) -> Decimal:
    patterns = {
        "invariant": (r"[+-]?\d+(?:\.\d+)?", "", "."),
        "en_US": (
            r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?",
            ",",
            ".",
        ),
        "de_DE": (
            r"[+-]?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?",
            ".",
            ",",
        ),
        "fr_FR": (
            r"[+-]?(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:,\d+)?",
            " ",
            ",",
        ),
    }
    pattern, grouping, decimal_separator = patterns[locale]
    if re.fullmatch(pattern, value) is None:
        raise ValueError
    normalized = value
    if locale == "fr_FR":
        normalized = re.sub(r"[ \u00a0\u202f]", "", normalized)
    elif grouping:
        normalized = normalized.replace(grouping, "")
    if decimal_separator != ".":
        normalized = normalized.replace(decimal_separator, ".")
    return Decimal(normalized)


@dataclass(frozen=True, slots=True)
class RelationshipMapping:
    target_field: str
    kind: str
    source_column_keys: tuple[str, ...]
    resolver: RelationshipResolver
    compare: bool = True
    validate_only: bool = False
    required: bool = False
    required_on_create: bool = False
    on_missing: str = "error"
    on_ambiguous: str = "error"
    operation: str = "replace"
    separator: str = ";"
    null_policy: str = "distinct"


@dataclass(frozen=True, slots=True)
class DatasetMapping:
    dataset_id: str
    target_model: str
    mode: MappingTargetMode = MappingTargetMode.UPSERT
    on_existing: str | None = None
    source_identity_column_keys: tuple[str, ...] = ()
    target_identity: tuple[IdentityComponentMapping, ...] = ()
    target_scope: tuple[IdentityComponentMapping, ...] = ()
    fields: tuple[ScalarFieldMapping, ...] = ()
    relationships: tuple[RelationshipMapping, ...] = ()
    control_totals: tuple["BusinessControlTotal", ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", MappingTargetMode(self.mode))
        if len(self.control_totals) > MAX_CONTROL_TOTALS_PER_DATASET:
            raise ValueError(
                "A dataset has too many declared business control totals"
            )


@dataclass(frozen=True, slots=True)
class BusinessControlTotal:
    """One explicitly named expected sum over a prepared numeric field."""

    name: str
    target_field: str
    expected_total: str
    unit: str = ""
    tolerance: str = "0"

    def __post_init__(self) -> None:
        name = self.name.strip()
        target_field = self.target_field.strip()
        unit = self.unit.strip()
        if not name or len(name) > 120:
            raise ValueError("Control-total name is required and must be concise")
        if not target_field or len(target_field) > 200:
            raise ValueError("Control-total field is invalid")
        if len(unit) > 40:
            raise ValueError("Control-total unit is too long")
        try:
            expected = Decimal(self.expected_total.strip())
            tolerance = Decimal(self.tolerance.strip() or "0")
        except (InvalidOperation, AttributeError) as error:
            raise ValueError("Control totals require plain numeric values") from error
        if not expected.is_finite() or not tolerance.is_finite():
            raise ValueError("Control totals require finite numeric values")
        if tolerance < 0:
            raise ValueError("Control-total tolerance cannot be negative")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "target_field", target_field)
        object.__setattr__(self, "expected_total", format(expected, "f"))
        object.__setattr__(self, "unit", unit)
        object.__setattr__(self, "tolerance", format(tolerance, "f"))


@dataclass(frozen=True, slots=True)
class MappingDefinition:
    """Portable mapping meaning, independent of revision/audit metadata."""

    mapping_id: str
    source_selection_hash: str
    schema_hash: str
    datasets: tuple[DatasetMapping, ...]
    contract_version: int = MAPPING_CONTRACT_VERSION

    @property
    def content_hash(self) -> str:
        return _content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload = {
            "mapping_id": self.mapping_id,
            "contract_version": self.contract_version,
            "source_selection_hash": self.source_selection_hash,
            "schema_hash": self.schema_hash,
            "datasets": [
                _dataset_mapping_to_dict(item, self.contract_version)
                for item in sorted(
                    (
                        replace(
                            item,
                            fields=tuple(
                                sorted(
                                    item.fields,
                                    key=lambda field: field.target_field,
                                )
                            ),
                            relationships=tuple(
                                sorted(
                                    item.relationships,
                                    key=lambda relation: relation.target_field,
                                )
                            ),
                            control_totals=tuple(
                                sorted(
                                    item.control_totals,
                                    key=lambda control: (
                                        control.target_field,
                                        control.name.casefold(),
                                    ),
                                )
                            ),
                        )
                        for item in self.datasets
                    ),
                    key=lambda item: item.dataset_id,
                )
            ],
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MappingDefinition":
        definition = cls(
            mapping_id=str(payload["mapping_id"]),
            contract_version=int(
                payload.get("contract_version", MAPPING_CONTRACT_VERSION)
            ),
            source_selection_hash=str(payload["source_selection_hash"]),
            schema_hash=str(payload["schema_hash"]),
            datasets=tuple(
                _dataset_mapping_from_dict(item)
                for item in payload.get("datasets", ())
            ),
        )
        expected = payload.get("content_hash")
        if expected is not None and expected != definition.content_hash:
            raise ValueError("Mapping-definition content hash is invalid")
        return definition

    @classmethod
    def from_json(cls, value: str) -> "MappingDefinition":
        return cls.from_dict(json.loads(value))


@dataclass(frozen=True, slots=True)
class MappingRevision:
    mapping_id: str
    version: int
    parent_version: int | None
    definition: MappingDefinition
    created_at: datetime
    created_by: str

    def to_json(self) -> str:
        return _canonical_json(
            {
                "mapping_id": self.mapping_id,
                "version": self.version,
                "parent_version": self.parent_version,
                "definition": self.definition.to_dict(),
                "created_at": self.created_at.isoformat(),
                "created_by": self.created_by,
            }
        )

    @classmethod
    def from_json(cls, value: str) -> "MappingRevision":
        payload = json.loads(value)
        return cls(
            mapping_id=str(payload["mapping_id"]),
            version=int(payload["version"]),
            parent_version=(
                int(payload["parent_version"])
                if payload.get("parent_version") is not None
                else None
            ),
            definition=MappingDefinition.from_dict(payload["definition"]),
            created_at=datetime.fromisoformat(payload["created_at"]),
            created_by=str(payload["created_by"]),
        )


@dataclass(frozen=True, slots=True)
class MappingValidationIssue:
    code: str
    severity: str
    path: str
    message: str
    remediation: str
    dataset_id: str | None = None
    source_column_key: str | None = None
    target_model: str | None = None
    target_field: str | None = None


@dataclass(frozen=True, slots=True)
class DeferredRuntimeCheck:
    code: str
    dataset_id: str
    message: str


@dataclass(frozen=True, slots=True)
class MappingValidationResult:
    mapping_content_hash: str
    source_selection_hash: str
    schema_hash: str
    status: MappingValidationStatus
    issues: tuple[MappingValidationIssue, ...]
    coverage: tuple[Mapping[str, Any], ...]
    deferred_runtime_checks: tuple[DeferredRuntimeCheck, ...]
    validator_version: str = MAPPING_VALIDATOR_VERSION
    contract_version: int = 1

    @property
    def validation_hash(self) -> str:
        return _content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "validator_version": self.validator_version,
            "mapping_content_hash": self.mapping_content_hash,
            "source_selection_hash": self.source_selection_hash,
            "schema_hash": self.schema_hash,
            "status": self.status.value,
            "issues": [_portable(asdict(item)) for item in self.issues],
            "coverage": [_portable(dict(item)) for item in self.coverage],
            "deferred_runtime_checks": [
                _portable(asdict(item)) for item in self.deferred_runtime_checks
            ],
        }
        if include_hash:
            payload["validation_hash"] = self.validation_hash
        return payload

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "MappingValidationResult":
        payload = json.loads(value)
        result = cls(
            contract_version=int(payload["contract_version"]),
            validator_version=str(payload["validator_version"]),
            mapping_content_hash=str(payload["mapping_content_hash"]),
            source_selection_hash=str(payload["source_selection_hash"]),
            schema_hash=str(payload["schema_hash"]),
            status=MappingValidationStatus(payload["status"]),
            issues=tuple(
                MappingValidationIssue(**item) for item in payload["issues"]
            ),
            coverage=tuple(payload["coverage"]),
            deferred_runtime_checks=tuple(
                DeferredRuntimeCheck(**item)
                for item in payload["deferred_runtime_checks"]
            ),
        )
        if payload.get("validation_hash") != result.validation_hash:
            raise ValueError("Mapping-validation hash is invalid")
        return result


@dataclass(frozen=True, slots=True)
class MappingSubmission:
    submission_id: str
    mapping_id: str
    version: int
    mapping_content_hash: str
    validation_hash: str
    warning_acknowledgements: tuple[str, ...]
    submitted_at: datetime
    submitted_by: str

    def to_json(self) -> str:
        return _canonical_json(_portable(asdict(self)))

    @classmethod
    def from_json(cls, value: str) -> "MappingSubmission":
        payload = json.loads(value)
        return cls(
            submission_id=str(payload["submission_id"]),
            mapping_id=str(payload["mapping_id"]),
            version=int(payload["version"]),
            mapping_content_hash=str(payload["mapping_content_hash"]),
            validation_hash=str(payload["validation_hash"]),
            warning_acknowledgements=tuple(
                payload.get("warning_acknowledgements", ())
            ),
            submitted_at=datetime.fromisoformat(payload["submitted_at"]),
            submitted_by=str(payload["submitted_by"]),
        )


@dataclass(frozen=True, slots=True)
class CompiledMapping:
    """Canonical semantic form used by validation and persistence."""

    definition: MappingDefinition

    @property
    def content_hash(self) -> str:
        return self.definition.content_hash


class MappingCompiler:
    """Canonicalize order-insensitive mapping collections."""

    def compile(self, definition: MappingDefinition) -> CompiledMapping:
        datasets = tuple(
            sorted(
                (
                    replace(
                        item,
                        fields=tuple(
                            sorted(
                                item.fields,
                                key=lambda field: field.target_field,
                            )
                        ),
                        relationships=tuple(
                            sorted(
                                item.relationships,
                                key=lambda relation: relation.target_field,
                            )
                        ),
                    )
                    for item in definition.datasets
                ),
                key=lambda item: item.dataset_id,
            )
        )
        return CompiledMapping(replace(definition, datasets=datasets))


class MappingSemanticValidator:
    """Validate a mapping against source/schema objects without data access."""

    def validate(
        self,
        definition: MappingDefinition,
        source_selection: Any,
        schema_catalog: Any,
        schema_governance: SchemaGovernance | None,
    ) -> MappingValidationResult:
        definition = MappingCompiler().compile(definition).definition
        issues: list[MappingValidationIssue] = []
        coverage: list[Mapping[str, Any]] = []
        deferred: list[DeferredRuntimeCheck] = []

        if definition.contract_version != MAPPING_CONTRACT_VERSION:
            issues.append(
                _issue(
                    "MAPPING_CONTRACT_UNSUPPORTED",
                    "/contract_version",
                    "The mapping contract version is unsupported.",
                    "Create a new mapping revision with the current editor.",
                )
            )
        if definition.source_selection_hash != source_selection.content_hash:
            issues.append(
                _issue(
                    "MAPPING_SOURCE_SELECTION_STALE",
                    "/source_selection_hash",
                    "The mapping does not match the current frozen sources.",
                    "Create a new mapping revision from the current source selection.",
                )
            )
        expected_schema_hash = (
            schema_governance.content_hash
            if schema_governance is not None
            else schema_catalog.content_hash
        )
        if definition.schema_hash != expected_schema_hash:
            issues.append(
                _issue(
                    "MAPPING_SCHEMA_STALE",
                    "/schema_hash",
                    "The mapping does not match the current governed schema.",
                    "Reopen and validate the mapping against the current schema.",
                )
            )
        if (
            schema_governance is None
            or schema_governance.catalog_hash != schema_catalog.content_hash
        ):
            issues.append(
                _issue(
                    "MAPPING_SCHEMA_GOVERNANCE_MISSING",
                    "/schema_hash",
                    "The captured schema has no current governance definition.",
                    "Confirm the permitted model scope and business keys.",
                )
            )

        source_datasets = {
            item.dataset_id: item for item in source_selection.datasets
        }
        schema_models = {item.name: item for item in schema_catalog.models}
        governed_keys = tuple(
            item
            for item in (
                schema_governance.business_keys
                if schema_governance is not None
                else ()
            )
            if item.status is BusinessKeyStatus.CONFIRMED
        )
        dataset_targets = {
            item.dataset_id: item.target_model for item in definition.datasets
        }
        seen_dataset_ids: set[str] = set()
        dependencies: dict[str, set[str]] = {}

        for dataset_index, dataset in enumerate(definition.datasets):
            base = f"/datasets/{dataset_index}"
            dependencies.setdefault(dataset.dataset_id, set())
            if dataset.dataset_id in seen_dataset_ids:
                issues.append(
                    _issue(
                        "MAPPING_DATASET_DUPLICATE",
                        f"{base}/dataset_id",
                        "The same source dataset is mapped more than once.",
                        "Keep one target dataset mapping per frozen source dataset.",
                        dataset=dataset,
                    )
                )
            seen_dataset_ids.add(dataset.dataset_id)
            source_dataset = source_datasets.get(dataset.dataset_id)
            if source_dataset is None:
                issues.append(
                    _issue(
                        "MAPPING_DATASET_UNKNOWN",
                        f"{base}/dataset_id",
                        "The mapping references an unknown source dataset.",
                        "Choose a dataset from the current frozen selection.",
                        dataset=dataset,
                    )
                )
                continue
            columns = {
                item.stable_key: item for item in source_dataset.columns
            }
            model = schema_models.get(dataset.target_model)
            if model is None:
                issues.append(
                    _issue(
                        "MAPPING_TARGET_MODEL_UNKNOWN",
                        f"{base}/target_model",
                        "The target model is absent from the permitted schema.",
                        "Add it to schema scope and recapture the schema.",
                        dataset=dataset,
                    )
                )
                continue
            fields = {item.name: item for item in model.fields}
            if (
                dataset.mode is MappingTargetMode.CREATE
                and dataset.on_existing not in {"block", "unchanged"}
            ):
                issues.append(
                    _issue(
                        "MAPPING_CREATE_POLICY_MISSING",
                        f"{base}/on_existing",
                        "Create mode requires an existing-identity policy.",
                        "Choose block or unchanged.",
                        dataset=dataset,
                    )
                )
            if (
                dataset.mode is not MappingTargetMode.CREATE
                and dataset.on_existing is not None
            ):
                issues.append(
                    _issue(
                        "MAPPING_CREATE_POLICY_INVALID",
                        f"{base}/on_existing",
                        "The existing-identity policy is only valid in create mode.",
                        "Remove the policy or choose create mode.",
                        dataset=dataset,
                    )
                )

            self._validate_source_identity(
                dataset, base, columns, issues
            )
            provided: set[str] = set()
            identity_fields: list[str] = []
            scope_fields: list[str] = []
            for group_name, components, collected in (
                ("target_identity", dataset.target_identity, identity_fields),
                ("target_scope", dataset.target_scope, scope_fields),
            ):
                for component_index, component in enumerate(components):
                    component_path = (
                        f"{base}/{group_name}/{component_index}"
                    )
                    self._validate_identity_component(
                        dataset,
                        component,
                        component_path,
                        columns,
                        fields,
                        schema_models,
                        governed_keys,
                        dataset_targets,
                        dependencies,
                        issues,
                    )
                    provided.update(component.target_fields)
                    collected.extend(component.target_fields)
            if not dataset.target_identity:
                issues.append(
                    _issue(
                        "MAPPING_TARGET_IDENTITY_MISSING",
                        f"{base}/target_identity",
                        "A target identity is required.",
                        "Choose one confirmed business key and map its components.",
                        dataset=dataset,
                    )
                )
            elif not _matches_business_key(
                governed_keys,
                dataset.target_model,
                tuple(identity_fields),
                tuple(scope_fields),
            ):
                issues.append(
                    _issue(
                        "MAPPING_BUSINESS_KEY_NOT_GOVERNED",
                        f"{base}/target_identity",
                        (
                            "Target identity and scope do not match a "
                            "confirmed business key."
                        ),
                        "Select a confirmed key definition for this model.",
                        dataset=dataset,
                    )
                )

            target_owners: dict[str, str] = {
                item: "identity" for item in provided
            }
            for field_index, field_mapping in enumerate(dataset.fields):
                path = f"{base}/fields/{field_index}"
                self._validate_scalar(
                    dataset,
                    field_mapping,
                    path,
                    columns,
                    fields,
                    issues,
                )
                self._claim_target(
                    dataset,
                    field_mapping.target_field,
                    path,
                    target_owners,
                    issues,
                )
                provided.add(field_mapping.target_field)

            seen_control_fields: set[str] = set()
            scalar_by_target = {
                item.target_field: item for item in dataset.fields
            }
            for control_index, control in enumerate(dataset.control_totals):
                path = f"{base}/control_totals/{control_index}"
                scalar = scalar_by_target.get(control.target_field)
                metadata = fields.get(control.target_field)
                if scalar is None:
                    issues.append(
                        _issue(
                            "MAPPING_CONTROL_TOTAL_FIELD_UNMAPPED",
                            f"{path}/target_field",
                            "The totals check uses an Odoo field that is not mapped.",
                            "Map that numeric field or remove the totals check.",
                            dataset=dataset,
                            target_field=control.target_field,
                        )
                    )
                elif scalar.value_type not in {"integer", "decimal"}:
                    issues.append(
                        _issue(
                            "MAPPING_CONTROL_TOTAL_FIELD_NOT_NUMERIC",
                            f"{path}/target_field",
                            "The totals check requires a mapped number or amount field.",
                            "Choose a mapped numeric Odoo field.",
                            dataset=dataset,
                            target_field=control.target_field,
                        )
                    )
                elif metadata is None or metadata.type not in {
                    "integer",
                    "float",
                    "monetary",
                }:
                    issues.append(
                        _issue(
                            "MAPPING_CONTROL_TOTAL_TARGET_NOT_NUMERIC",
                            f"{path}/target_field",
                            "The Odoo field selected for this total is not numeric.",
                            "Choose a number, quantity, or amount field.",
                            dataset=dataset,
                            target_field=control.target_field,
                        )
                    )
                if control.target_field in seen_control_fields:
                    issues.append(
                        _issue(
                            "MAPPING_CONTROL_TOTAL_DUPLICATE",
                            f"{path}/target_field",
                            "The same field has more than one totals check.",
                            "Keep one named expected total for this field.",
                            dataset=dataset,
                            target_field=control.target_field,
                        )
                    )
                seen_control_fields.add(control.target_field)

            for relation_index, relation in enumerate(
                dataset.relationships
            ):
                path = f"{base}/relationships/{relation_index}"
                self._validate_relationship(
                    dataset,
                    relation,
                    path,
                    columns,
                    fields,
                    schema_models,
                    governed_keys,
                    dataset_targets,
                    dependencies,
                    issues,
                )
                self._claim_target(
                    dataset,
                    relation.target_field,
                    path,
                    target_owners,
                    issues,
                )
                provided.add(relation.target_field)

            if dataset.mode is not MappingTargetMode.REFERENCE:
                for target_field in sorted(fields):
                    metadata = fields[target_field]
                    if (
                        metadata.required
                        and not metadata.readonly
                        and target_field not in provided
                    ):
                        issues.append(
                            _issue(
                                "MAPPING_REQUIRED_FIELD_UNMAPPED",
                                f"{base}/target_model",
                                (
                                    f"Required target field {dataset.target_model}."
                                    f"{target_field} has no value provider."
                                ),
                                "Map a source value or add a later governed default.",
                                dataset=dataset,
                                target_field=target_field,
                            )
                        )

            coverage.append(
                {
                    "dataset_id": dataset.dataset_id,
                    "dataset_name": source_dataset.name,
                    "target_model": dataset.target_model,
                    "source_columns": len(columns),
                    "mapped_scalar_fields": len(dataset.fields),
                    "mapped_relationships": len(dataset.relationships),
                    "identity_components": len(dataset.target_identity),
                    "scope_components": len(dataset.target_scope),
                }
            )
            deferred.extend(
                (
                    DeferredRuntimeCheck(
                        code="SOURCE_IDENTITY_UNIQUENESS",
                        dataset_id=dataset.dataset_id,
                        message=(
                            "Verify source identity uniqueness after governed "
                            "normalization."
                        ),
                    ),
                    DeferredRuntimeCheck(
                        code="TARGET_IDENTITY_UNIQUENESS",
                        dataset_id=dataset.dataset_id,
                        message=(
                            "Verify target business-key uniqueness in the "
                            "captured record catalog."
                        ),
                    ),
                    DeferredRuntimeCheck(
                        code="REQUIRED_ROW_VALUES",
                        dataset_id=dataset.dataset_id,
                        message="Verify required values on every staged row.",
                    ),
                    DeferredRuntimeCheck(
                        code="REFERENCE_RESOLUTION",
                        dataset_id=dataset.dataset_id,
                        message=(
                            "Resolve every logical relationship by business key."
                        ),
                    ),
                )
            )

        for dataset_id in sorted(set(source_datasets).difference(seen_dataset_ids)):
            source_dataset = source_datasets[dataset_id]
            issues.append(
                _issue(
                    "MAPPING_DATASET_UNMAPPED",
                    "/datasets",
                    (
                        f"Frozen source dataset {source_dataset.name!r} has no "
                        "target mapping."
                    ),
                    "Map every frozen dataset or create a new source selection.",
                )
            )

        self._validate_dependencies(dependencies, definition.datasets, issues)
        sorted_issues = tuple(
            sorted(
                issues,
                key=lambda item: (
                    item.severity,
                    item.path,
                    item.code,
                    item.message,
                ),
            )
        )
        status = (
            MappingValidationStatus.INVALID
            if any(item.severity == "error" for item in sorted_issues)
            else (
                MappingValidationStatus.VALID_WITH_WARNINGS
                if sorted_issues
                else MappingValidationStatus.VALID
            )
        )
        return MappingValidationResult(
            mapping_content_hash=definition.content_hash,
            source_selection_hash=definition.source_selection_hash,
            schema_hash=definition.schema_hash,
            status=status,
            issues=sorted_issues,
            coverage=tuple(
                sorted(coverage, key=lambda item: str(item["dataset_id"]))
            ),
            deferred_runtime_checks=tuple(
                sorted(
                    deferred,
                    key=lambda item: (item.dataset_id, item.code),
                )
            ),
        )

    @staticmethod
    def _validate_source_identity(
        dataset: DatasetMapping,
        base: str,
        columns: Mapping[str, Any],
        issues: list[MappingValidationIssue],
    ) -> None:
        if not dataset.source_identity_column_keys:
            issues.append(
                _issue(
                    "MAPPING_SOURCE_IDENTITY_MISSING",
                    f"{base}/source_identity_column_keys",
                    "A source trace identity is required.",
                    "Choose one or more stable source columns.",
                    dataset=dataset,
                )
            )
        if len(set(dataset.source_identity_column_keys)) != len(
            dataset.source_identity_column_keys
        ):
            issues.append(
                _issue(
                    "MAPPING_SOURCE_IDENTITY_DUPLICATE",
                    f"{base}/source_identity_column_keys",
                    "A source identity column is repeated.",
                    "Keep each source identity component once.",
                    dataset=dataset,
                )
            )
        for column in dataset.source_identity_column_keys:
            _check_column(dataset, column, base, columns, issues)

    def _validate_identity_component(
        self,
        dataset: DatasetMapping,
        component: IdentityComponentMapping,
        path: str,
        columns: Mapping[str, Any],
        fields: Mapping[str, Any],
        schema_models: Mapping[str, Any],
        governed_keys: tuple[BusinessKeyDefinition, ...],
        dataset_targets: Mapping[str, str],
        dependencies: dict[str, set[str]],
        issues: list[MappingValidationIssue],
    ) -> None:
        for column in component.source_column_keys:
            _check_column(dataset, column, path, columns, issues)
        if not component.target_fields:
            issues.append(
                _issue(
                    "MAPPING_IDENTITY_ARITY_INVALID",
                    f"{path}/target_fields",
                    "An identity component requires a target field.",
                    "Choose the corresponding target business-key field.",
                    dataset=dataset,
                )
            )
            return
        if component.resolver is None:
            if len(component.source_column_keys) != len(
                component.target_fields
            ):
                issues.append(
                    _issue(
                        "MAPPING_IDENTITY_ARITY_INVALID",
                        path,
                        "Scalar identity source and target arity differ.",
                        "Map one source column to each target component.",
                        dataset=dataset,
                    )
                )
            for target_field in component.target_fields:
                metadata = fields.get(target_field)
                if metadata is None:
                    issues.append(
                        _target_unknown(dataset, path, target_field)
                    )
                elif component.value_type not in _VALUE_TYPES or (
                    metadata.type
                    not in TYPE_COMPATIBILITY.get(
                        component.value_type, frozenset()
                    )
                ):
                    issues.append(
                        _issue(
                            "MAPPING_TYPE_INCOMPATIBLE",
                            path,
                            f"Identity field {target_field} is type-incompatible.",
                            "Choose a compatible canonical type.",
                            dataset=dataset,
                            target_field=target_field,
                        )
                    )
            return

        if len(component.target_fields) != 1:
            issues.append(
                _issue(
                    "MAPPING_IDENTITY_ARITY_INVALID",
                    path,
                    "A relational identity component targets one many2one field.",
                    "Choose exactly one relational target field.",
                    dataset=dataset,
                )
            )
            return
        target_field = component.target_fields[0]
        metadata = fields.get(target_field)
        if metadata is None:
            issues.append(_target_unknown(dataset, path, target_field))
            return
        if metadata.type != "many2one":
            issues.append(
                _issue(
                    "MAPPING_RELATION_KIND_INCORRECT",
                    path,
                    f"Relational identity field {target_field} is not many2one.",
                    "Choose a many2one identity/scope field.",
                    dataset=dataset,
                    target_field=target_field,
                )
            )
        self._validate_resolver(
            dataset,
            component.resolver,
            path,
            component.source_column_keys,
            metadata.relation,
            schema_models,
            governed_keys,
            dataset_targets,
            dependencies,
            issues,
            require_governed_key=True,
        )

    @staticmethod
    def _claim_target(
        dataset: DatasetMapping,
        target_field: str,
        path: str,
        owners: dict[str, str],
        issues: list[MappingValidationIssue],
    ) -> None:
        previous = owners.get(target_field)
        if previous is not None:
            issues.append(
                _issue(
                    "MAPPING_TARGET_FIELD_DUPLICATE",
                    path,
                    f"Target field {target_field} already has a {previous} provider.",
                    "Keep one provider for each target field in a dataset.",
                    dataset=dataset,
                    target_field=target_field,
                )
            )
        else:
            owners[target_field] = path

    @staticmethod
    def _validate_scalar(
        dataset: DatasetMapping,
        field_mapping: ScalarFieldMapping,
        path: str,
        columns: Mapping[str, Any],
        fields: Mapping[str, Any],
        issues: list[MappingValidationIssue],
    ) -> None:
        source_required = field_mapping.value_source in {
            ScalarValueSource.SOURCE,
            ScalarValueSource.SOURCE_WITH_FALLBACK,
        }
        literal_required = field_mapping.value_source in {
            ScalarValueSource.CONSTANT,
            ScalarValueSource.SOURCE_WITH_FALLBACK,
        }
        if source_required:
            if field_mapping.source_column_key:
                _check_column(
                    dataset,
                    field_mapping.source_column_key,
                    path,
                    columns,
                    issues,
                )
            else:
                issues.append(
                    _issue(
                        "MAPPING_VALUE_PROVIDER_INVALID",
                        path,
                        "The selected value provider requires a source column.",
                        "Choose a frozen source column.",
                        dataset=dataset,
                        target_field=field_mapping.target_field,
                    )
                )
        elif field_mapping.source_column_key is not None:
            issues.append(
                _issue(
                    "MAPPING_VALUE_PROVIDER_INVALID",
                    path,
                    "This value provider must not reference a source column.",
                    "Clear the source column or choose a source-based provider.",
                    dataset=dataset,
                    source_column=field_mapping.source_column_key,
                    target_field=field_mapping.target_field,
                )
            )
        if literal_required and field_mapping.literal_value is None:
            issues.append(
                _issue(
                    "MAPPING_VALUE_PROVIDER_INVALID",
                    path,
                    "The selected value provider requires a literal value.",
                    "Enter a constant or fallback value.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        elif (
            not literal_required
            and field_mapping.literal_value is not None
        ):
            issues.append(
                _issue(
                    "MAPPING_VALUE_PROVIDER_INVALID",
                    path,
                    "This value provider must not contain a literal value.",
                    "Clear the literal or choose constant/fallback.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        metadata = fields.get(field_mapping.target_field)
        if metadata is None:
            issues.append(
                _target_unknown(dataset, path, field_mapping.target_field)
            )
            return
        if metadata.type in _RELATION_TYPES:
            issues.append(
                _issue(
                    "MAPPING_RELATION_KIND_INCORRECT",
                    path,
                    f"{field_mapping.target_field} is relational, not scalar.",
                    "Configure it in the relationship builder.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if field_mapping.value_mappings:
            if field_mapping.value_source not in {
                ScalarValueSource.SOURCE,
                ScalarValueSource.SOURCE_WITH_FALLBACK,
            }:
                issues.append(
                    _issue(
                        "MAPPING_VALUE_MATCH_INVALID",
                        path,
                        "Value matching requires a source column.",
                        "Choose Source column or Source + fallback.",
                        dataset=dataset,
                        target_field=field_mapping.target_field,
                    )
                )
            selection_keys = {str(item[0]) for item in metadata.selection}
            if not selection_keys:
                issues.append(
                    _issue(
                        "MAPPING_VALUE_MATCH_INVALID",
                        path,
                        "This Odoo field does not provide a list of choices.",
                        "Use value matching only for captured Odoo choices.",
                        dataset=dataset,
                        target_field=field_mapping.target_field,
                    )
                )
            invalid_target = next(
                (
                    item.target_value
                    for item in field_mapping.value_mappings
                    if item.target_value not in selection_keys
                ),
                None,
            )
            if invalid_target is not None:
                issues.append(
                    _issue(
                        "MAPPING_SELECTION_VALUE_INVALID",
                        path,
                        (
                            f"{invalid_target!r} is not an available Odoo "
                            f"choice for {field_mapping.target_field}."
                        ),
                        "Choose one of the captured Odoo choices.",
                        dataset=dataset,
                        target_field=field_mapping.target_field,
                    )
                )
        if field_mapping.value_type not in _VALUE_TYPES or (
            metadata.type
            not in TYPE_COMPATIBILITY.get(
                field_mapping.value_type, frozenset()
            )
        ):
            issues.append(
                _issue(
                    "MAPPING_TYPE_INCOMPATIBLE",
                    path,
                    (
                        f"{field_mapping.target_field} is {metadata.type}, "
                        f"not compatible with {field_mapping.value_type}."
                    ),
                    "Choose a compatible canonical value type.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        MappingSemanticValidator._validate_transform_policy(
            dataset,
            field_mapping,
            path,
            columns,
            issues,
        )
        if metadata.readonly and not field_mapping.validate_only:
            issues.append(
                _issue(
                    "MAPPING_TARGET_FIELD_READONLY",
                    path,
                    f"{field_mapping.target_field} is readonly.",
                    "Remove it or mark it validate-only.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if field_mapping.validate_only and field_mapping.compare:
            issues.append(
                _issue(
                    "MAPPING_FIELD_POLICY_INVALID",
                    path,
                    "A validate-only scalar cannot also be compared.",
                    "Disable comparison or validate-only.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if (
            field_mapping.value_source is ScalarValueSource.ODOO_DEFAULT
            and (
                field_mapping.compare
                or field_mapping.validate_only
                or field_mapping.required
                or field_mapping.required_on_create
                or field_mapping.transform.search_value
                or field_mapping.transform.formula
                or field_mapping.transform.case_mode != "preserve"
                or field_mapping.transform.decimal_places is not None
                or field_mapping.validation.configured
            )
        ):
            issues.append(
                _issue(
                    "MAPPING_FIELD_POLICY_INVALID",
                    path,
                    "An Odoo-default field has no local value to compare or validate.",
                    "Disable compare, validate-only, and required value checks.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if field_mapping.value_source is ScalarValueSource.ODOO_DEFAULT:
            issues.append(
                _issue(
                    "MAPPING_ODOO_DEFAULT_UNVERIFIED",
                    path,
                    (
                        f"{field_mapping.target_field} will be omitted so Odoo "
                        "can apply its runtime default."
                    ),
                    "Acknowledge this warning and verify the default on the target.",
                    severity="warning",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if field_mapping.null_policy not in _NULL_POLICIES:
            issues.append(
                _issue(
                    "MAPPING_FIELD_POLICY_INVALID",
                    path,
                    "The scalar null policy is unsupported.",
                    "Choose distinct, equivalent, or ignore_source_null.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if literal_required and field_mapping.literal_value is not None:
            try:
                proposed = canonicalize_scalar_value(
                    field_mapping,
                    None,
                )
            except ScalarValueError as error:
                issues.append(
                    _issue(
                        "MAPPING_LITERAL_INVALID",
                        path,
                        str(error),
                        "Correct the literal or its parsing policy.",
                        dataset=dataset,
                        target_field=field_mapping.target_field,
                    )
                )
            else:
                selection_keys = {
                    str(item[0]) for item in metadata.selection
                }
                if metadata.required and proposed in {None, ""}:
                    issues.append(
                        _issue(
                            "MAPPING_LITERAL_INVALID",
                            path,
                            (
                                f"{field_mapping.target_field} is required but "
                                "the governed literal resolves to empty."
                            ),
                            "Enter a non-empty constant or fallback.",
                            dataset=dataset,
                            target_field=field_mapping.target_field,
                        )
                    )
                if (
                    selection_keys
                    and proposed is not None
                    and str(proposed) not in selection_keys
                ):
                    issues.append(
                        _issue(
                            "MAPPING_SELECTION_VALUE_INVALID",
                            path,
                            (
                                f"{proposed!r} is not an allowed selection "
                                f"value for {field_mapping.target_field}."
                            ),
                            "Choose one of the captured Odoo selection keys.",
                            dataset=dataset,
                            target_field=field_mapping.target_field,
                        )
                    )
        column = columns.get(field_mapping.source_column_key)
        expected_candidate = {
            "boolean": "boolean",
            "integer": "integer",
            "decimal": "decimal",
            "date": "date",
            "datetime": "datetime",
        }.get(field_mapping.value_type)
        if (
            column is not None
            and expected_candidate is not None
            and column.candidate_type
            not in {expected_candidate, "string", "mixed", "empty"}
        ):
            issues.append(
                _issue(
                    "MAPPING_SOURCE_TYPE_ADVISORY_MISMATCH",
                    path,
                    (
                        f"Source candidate type {column.candidate_type} differs "
                        f"from {field_mapping.value_type}."
                    ),
                    "Review samples; candidate types are advisory.",
                    severity="warning",
                    dataset=dataset,
                    source_column=field_mapping.source_column_key,
                    target_field=field_mapping.target_field,
                )
            )

    @staticmethod
    def _validate_transform_policy(
        dataset: DatasetMapping,
        field_mapping: ScalarFieldMapping,
        path: str,
        columns: Mapping[str, Any],
        issues: list[MappingValidationIssue],
    ) -> None:
        policy = field_mapping.transform
        if policy.case_mode not in CASE_MODES:
            issues.append(
                _issue(
                    "MAPPING_TRANSFORM_INVALID",
                    path,
                    "The case transformation is unsupported.",
                    (
                        "Choose keep as-is, uppercase, lowercase, sentence "
                        "case, or title case."
                    ),
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        elif (
            policy.case_mode != "preserve"
            and field_mapping.value_type != "string"
        ):
            issues.append(
                _issue(
                    "MAPPING_TRANSFORM_INVALID",
                    path,
                    "Case transformations apply only to string values.",
                    "Preserve case or choose the string canonical type.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if policy.search_mode not in SEARCH_MODES:
            issues.append(
                _issue(
                    "MAPPING_TRANSFORM_INVALID",
                    path,
                    "The find-and-replace mode is unsupported.",
                    "Choose plain text or advanced custom pattern.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        elif policy.search_mode == "pattern" and policy.search_value:
            try:
                validate_pattern(policy.search_value)
            except ValueError as error:
                issues.append(
                    _issue(
                        "MAPPING_TRANSFORM_INVALID",
                        path,
                        str(error),
                        "Correct the advanced find pattern or use plain text.",
                        dataset=dataset,
                        target_field=field_mapping.target_field,
                    )
                )
        if policy.replacement_value and not policy.search_value:
            issues.append(
                _issue(
                    "MAPPING_TRANSFORM_INVALID",
                    path,
                    "Replacement text was entered without text to find.",
                    "Enter the text to find or clear the replacement.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if policy.search_value and field_mapping.value_type != "string":
            issues.append(
                _issue(
                    "MAPPING_TRANSFORM_INVALID",
                    path,
                    "Find and replace applies only to text values.",
                    "Choose the string value type or clear find and replace.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if policy.formula:
            aliases = {
                f"column_{getattr(column, 'ordinal', index + 1)}"
                for index, column in enumerate(columns.values())
            }
            try:
                validate_formula(policy.formula, allowed_names=aliases)
            except ValueError as error:
                issues.append(
                    _issue(
                        "MAPPING_FORMULA_INVALID",
                        path,
                        str(error),
                        "Correct the formula using the available column names.",
                        dataset=dataset,
                        target_field=field_mapping.target_field,
                    )
                )
        if policy.rounding_mode not in ROUNDING_MODES:
            issues.append(
                _issue(
                    "MAPPING_TRANSFORM_INVALID",
                    path,
                    "The rounding method is unsupported.",
                    "Choose one of the listed rounding methods.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if policy.decimal_places is not None and (
            policy.decimal_places < 0
            or policy.decimal_places > 18
            or field_mapping.value_type != "decimal"
        ):
            issues.append(
                _issue(
                    "MAPPING_TRANSFORM_INVALID",
                    path,
                    "Decimal rounding needs a decimal value and 0 to 18 places.",
                    "Choose decimal as the value type and a valid number of places.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if policy.decimal_locale not in _DECIMAL_LOCALES:
            issues.append(
                _issue(
                    "MAPPING_TRANSFORM_INVALID",
                    path,
                    "The decimal locale is unsupported.",
                    "Choose invariant, en_US, de_DE, or fr_FR.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if policy.date_format not in _DATE_FORMATS:
            issues.append(
                _issue(
                    "MAPPING_TRANSFORM_INVALID",
                    path,
                    "The date format is unsupported.",
                    "Choose one of the explicit date formats.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if policy.timezone != "UTC":
            issues.append(
                _issue(
                    "MAPPING_TRANSFORM_INVALID",
                    path,
                    "The current mapping rules support the explicit UTC timezone only.",
                    "Choose UTC; broader IANA timezone support is deferred.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        MappingSemanticValidator._validate_scalar_rules(
            dataset,
            field_mapping,
            path,
            issues,
        )

    @staticmethod
    def _validate_scalar_rules(
        dataset: DatasetMapping,
        field_mapping: ScalarFieldMapping,
        path: str,
        issues: list[MappingValidationIssue],
    ) -> None:
        policy = field_mapping.validation
        if policy.configured and field_mapping.value_type != "string":
            issues.append(
                _issue(
                    "MAPPING_VALUE_RULE_INVALID",
                    path,
                    "Length and character checks apply only to text values.",
                    "Choose the string value type or clear the text checks.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if policy.exact_length is not None and not (
            1 <= policy.exact_length <= MAX_RULE_SIZE
        ):
            issues.append(
                _issue(
                    "MAPPING_VALUE_RULE_INVALID",
                    path,
                    f"Exact length must be between 1 and {MAX_RULE_SIZE}.",
                    "Enter a valid exact length.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if policy.segment_location not in SEGMENT_LOCATIONS:
            issues.append(
                _issue(
                    "MAPPING_VALUE_RULE_INVALID",
                    path,
                    "The part of the value to check is unsupported.",
                    "Choose whole value, first characters, or last characters.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if policy.character_class not in CHARACTER_CLASSES:
            issues.append(
                _issue(
                    "MAPPING_VALUE_RULE_INVALID",
                    path,
                    "The required character type is unsupported.",
                    "Choose digits, capital letters, or lowercase letters.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if policy.character_class != "none" and policy.segment_location == "none":
            issues.append(
                _issue(
                    "MAPPING_VALUE_RULE_INVALID",
                    path,
                    "Choose which part of the value should be checked.",
                    "Choose whole value, first characters, or last characters.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if policy.segment_location != "none" and policy.character_class == "none":
            issues.append(
                _issue(
                    "MAPPING_VALUE_RULE_INVALID",
                    path,
                    "Choose what kind of characters are allowed.",
                    "Choose digits, capital letters, or lowercase letters.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if policy.segment_location in {"first", "last"} and (
            policy.segment_length is None
            or not 1 <= policy.segment_length <= MAX_RULE_SIZE
        ):
            issues.append(
                _issue(
                    "MAPPING_VALUE_RULE_INVALID",
                    path,
                    f"First/last character count must be between 1 and {MAX_RULE_SIZE}.",
                    "Enter how many characters should be checked.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if policy.segment_location in {"none", "entire"} and (
            policy.segment_length is not None
        ):
            issues.append(
                _issue(
                    "MAPPING_VALUE_RULE_INVALID",
                    path,
                    "A character count is used only for first or last characters.",
                    "Clear the count or choose first/last characters.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if (
            policy.exact_length is not None
            and policy.segment_length is not None
            and policy.segment_length > policy.exact_length
        ):
            issues.append(
                _issue(
                    "MAPPING_VALUE_RULE_INVALID",
                    path,
                    "The character check is longer than the exact field length.",
                    "Reduce the checked character count or increase exact length.",
                    dataset=dataset,
                    target_field=field_mapping.target_field,
                )
            )
        if policy.pattern:
            try:
                validate_pattern(policy.pattern)
            except ValueError as error:
                issues.append(
                    _issue(
                        "MAPPING_VALUE_RULE_INVALID",
                        path,
                        str(error),
                        "Correct the advanced custom pattern.",
                        dataset=dataset,
                        target_field=field_mapping.target_field,
                    )
                )

    def _validate_relationship(
        self,
        dataset: DatasetMapping,
        relation: RelationshipMapping,
        path: str,
        columns: Mapping[str, Any],
        fields: Mapping[str, Any],
        schema_models: Mapping[str, Any],
        governed_keys: tuple[BusinessKeyDefinition, ...],
        dataset_targets: Mapping[str, str],
        dependencies: dict[str, set[str]],
        issues: list[MappingValidationIssue],
    ) -> None:
        if not relation.source_column_keys:
            issues.append(
                _issue(
                    "MAPPING_REFERENCE_KEY_INVALID",
                    path,
                    "A relationship requires source reference data.",
                    "Choose the source key column or list column.",
                    dataset=dataset,
                    target_field=relation.target_field,
                )
            )
        for column in relation.source_column_keys:
            _check_column(dataset, column, path, columns, issues)
        metadata = fields.get(relation.target_field)
        if metadata is None:
            issues.append(_target_unknown(dataset, path, relation.target_field))
            return
        if metadata.type == "one2many":
            inverse = (
                f" through inverse {metadata.relation_field}"
                if getattr(metadata, "relation_field", None)
                else ""
            )
            issues.append(
                _issue(
                    "MAPPING_ONE2MANY_OWNER_INVALID",
                    path,
                    f"{relation.target_field} is one2many{inverse}.",
                    "Map a child dataset to the inverse many2one field.",
                    dataset=dataset,
                    target_field=relation.target_field,
                )
            )
        if metadata.type != relation.kind:
            issues.append(
                _issue(
                    "MAPPING_RELATION_KIND_INCORRECT",
                    path,
                    (
                        f"{relation.target_field} is {metadata.type}, "
                        f"not {relation.kind}."
                    ),
                    "Use the relation kind captured from Odoo.",
                    dataset=dataset,
                    target_field=relation.target_field,
                )
            )
        if metadata.readonly and not relation.validate_only:
            issues.append(
                _issue(
                    "MAPPING_TARGET_FIELD_READONLY",
                    path,
                    f"{relation.target_field} is readonly.",
                    "Remove it or mark it validate-only.",
                    dataset=dataset,
                    target_field=relation.target_field,
                )
            )
        if relation.validate_only and relation.compare:
            issues.append(
                _issue(
                    "MAPPING_RELATION_POLICY_UNSAFE",
                    path,
                    "A validate-only relation cannot also be compared.",
                    "Disable comparison or validate-only.",
                    dataset=dataset,
                    target_field=relation.target_field,
                )
            )
        if (
            relation.compare or relation.required or relation.required_on_create
        ) and relation.on_missing != "error":
            issues.append(
                _issue(
                    "MAPPING_RELATION_POLICY_UNSAFE",
                    path,
                    "A compared or required relation must fail when missing.",
                    "Use on_missing: error.",
                    dataset=dataset,
                    target_field=relation.target_field,
                )
            )
        if relation.compare and relation.on_ambiguous != "error":
            issues.append(
                _issue(
                    "MAPPING_RELATION_POLICY_UNSAFE",
                    path,
                    "A compared relation must fail when ambiguous.",
                    "Use on_ambiguous: error.",
                    dataset=dataset,
                    target_field=relation.target_field,
                )
            )
        if relation.kind == "many2one" and relation.operation != "replace":
            issues.append(
                _issue(
                    "MAPPING_RELATION_POLICY_UNSAFE",
                    path,
                    "Many2one supports replace only.",
                    "Choose replace.",
                    dataset=dataset,
                    target_field=relation.target_field,
                )
            )
        if relation.kind == "many2many":
            if len(relation.source_column_keys) != 1:
                issues.append(
                    _issue(
                        "MAPPING_REFERENCE_KEY_INVALID",
                        path,
                        "Many2many requires one list-valued source column.",
                        "Choose exactly one source column.",
                        dataset=dataset,
                        target_field=relation.target_field,
                    )
                )
            if relation.operation not in {"replace", "add", "remove"}:
                issues.append(
                    _issue(
                        "MAPPING_RELATION_POLICY_UNSAFE",
                        path,
                        "The many2many operation is unsupported.",
                        "Choose replace, add, or remove.",
                        dataset=dataset,
                        target_field=relation.target_field,
                    )
                )
            if not relation.separator or len(relation.separator) != 1:
                issues.append(
                    _issue(
                        "MAPPING_RELATION_POLICY_UNSAFE",
                        path,
                        "The many2many separator must be one character.",
                        "Choose one explicit separator.",
                        dataset=dataset,
                        target_field=relation.target_field,
                    )
                )
        if relation.null_policy not in _NULL_POLICIES:
            issues.append(
                _issue(
                    "MAPPING_RELATION_POLICY_UNSAFE",
                    path,
                    "The relation null policy is unsupported.",
                    "Choose a supported null policy.",
                    dataset=dataset,
                    target_field=relation.target_field,
                )
            )
        if relation.on_missing not in {"error", "warning"}:
            issues.append(
                _issue(
                    "MAPPING_RELATION_POLICY_UNSAFE",
                    path,
                    "The missing-reference policy is unsupported.",
                    "Choose error or warning.",
                    dataset=dataset,
                    target_field=relation.target_field,
                )
            )
        if relation.on_ambiguous not in {"error", "warning"}:
            issues.append(
                _issue(
                    "MAPPING_RELATION_POLICY_UNSAFE",
                    path,
                    "The ambiguous-reference policy is unsupported.",
                    "Choose error or warning.",
                    dataset=dataset,
                    target_field=relation.target_field,
                )
            )
        self._validate_resolver(
            dataset,
            relation.resolver,
            path,
            relation.source_column_keys,
            metadata.relation,
            schema_models,
            governed_keys,
            dataset_targets,
            dependencies,
            issues,
            require_governed_key=True,
        )

    @staticmethod
    def _validate_resolver(
        dataset: DatasetMapping,
        resolver: RelationshipResolver,
        path: str,
        source_columns: tuple[str, ...],
        expected_model: str | None,
        schema_models: Mapping[str, Any],
        governed_keys: tuple[BusinessKeyDefinition, ...],
        dataset_targets: Mapping[str, str],
        dependencies: dict[str, set[str]],
        issues: list[MappingValidationIssue],
        *,
        require_governed_key: bool,
    ) -> None:
        if resolver.origin is ResolverOrigin.DATASET:
            if not resolver.dataset_id:
                issues.append(
                    _issue(
                        "MAPPING_REFERENCE_KEY_INVALID",
                        path,
                        "Incoming resolution requires a referenced dataset.",
                        "Choose the parent/reference dataset.",
                        dataset=dataset,
                    )
                )
                return
            dependencies.setdefault(dataset.dataset_id, set()).add(
                resolver.dataset_id
            )
            referenced_model = dataset_targets.get(resolver.dataset_id)
            if (
                referenced_model is not None
                and expected_model is not None
                and referenced_model != expected_model
            ):
                issues.append(
                    _issue(
                        "MAPPING_RELATED_MODEL_INCORRECT",
                        path,
                        (
                            f"Referenced dataset targets {referenced_model}, "
                            f"but the relation expects {expected_model}."
                        ),
                        "Choose a dataset mapped to the captured related model.",
                        dataset=dataset,
                    )
                )
            if (
                resolver.model
                or resolver.key_mappings
                or resolver.scope_mappings
                or resolver.value_mappings
            ):
                issues.append(
                    _issue(
                        "MAPPING_REFERENCE_KEY_INVALID",
                        path,
                        "Incoming resolution must derive keys from its dataset.",
                        "Remove target-catalog key settings.",
                        dataset=dataset,
                    )
                )
            return

        if resolver.dataset_id is not None:
            issues.append(
                _issue(
                    "MAPPING_REFERENCE_KEY_INVALID",
                    path,
                    "Target-catalog resolution cannot name an incoming dataset.",
                    "Remove the dataset selection.",
                    dataset=dataset,
                )
            )
        if not resolver.model or resolver.model != expected_model:
            issues.append(
                _issue(
                    "MAPPING_RELATED_MODEL_INCORRECT",
                    path,
                    (
                        f"Resolver model {resolver.model!r} does not match "
                        f"{expected_model!r}."
                    ),
                    "Use the related model captured from Odoo.",
                    dataset=dataset,
                )
            )
            return
        model = schema_models.get(resolver.model)
        standard_key = standard_reference_key(resolver.model)
        if model is None and standard_key is None:
            issues.append(
                _issue(
                    "MAPPING_TARGET_MODEL_UNKNOWN",
                    path,
                    "The resolver model is absent from the permitted schema.",
                    "Add it to schema scope and recapture.",
                    dataset=dataset,
                )
            )
            return
        model_fields = {item.name for item in model.fields} if model else set()
        if standard_key is not None:
            model_fields.update(standard_key.key_fields)
            model_fields.add(standard_key.display_field)
        if not resolver.key_mappings:
            issues.append(
                _issue(
                    "MAPPING_REFERENCE_KEY_INVALID",
                    path,
                    "Target-catalog resolution requires a business key.",
                    "Choose a confirmed business-key definition.",
                    dataset=dataset,
                )
            )
        for mapping in (*resolver.key_mappings, *resolver.scope_mappings):
            if mapping.source_column_key not in source_columns:
                issues.append(
                    _issue(
                        "MAPPING_REFERENCE_KEY_INVALID",
                        path,
                        "Resolver source keys must be declared relation columns.",
                        "Add the source key to the relation mapping.",
                        dataset=dataset,
                        source_column=mapping.source_column_key,
                    )
                )
            if mapping.target_field not in model_fields:
                issues.append(
                    _issue(
                        "MAPPING_TARGET_FIELD_UNKNOWN",
                        path,
                        (
                            f"Resolver field {resolver.model}."
                            f"{mapping.target_field} is unavailable."
                        ),
                        "Choose a captured field.",
                        dataset=dataset,
                        target_field=mapping.target_field,
                    )
                )
        mapped_sources = tuple(
            item.source_column_key
            for item in (*resolver.key_mappings, *resolver.scope_mappings)
        )
        if mapped_sources != source_columns:
            issues.append(
                _issue(
                    "MAPPING_REFERENCE_KEY_INVALID",
                    path,
                    (
                        "Resolver keys must consume every declared source "
                        "column once and in business-key order."
                    ),
                    "Align source columns with the selected key and scope.",
                    dataset=dataset,
                )
            )
        if resolver.value_mappings and (
            len(source_columns) != 1
            or len(resolver.key_mappings) != 1
            or bool(resolver.scope_mappings)
        ):
            issues.append(
                _issue(
                    "MAPPING_VALUE_MATCH_INVALID",
                    path,
                    (
                        "Value matching currently supports one source choice "
                        "and one Odoo business-key field."
                    ),
                    "Choose a single-column, single-key relationship.",
                    dataset=dataset,
                )
            )
        elif resolver.value_mappings:
            key_field = resolver.key_mappings[0].target_field
            key_metadata = next(
                (
                    item
                    for item in (model.fields if model else ())
                    if item.name == key_field
                ),
                None,
            )
            if key_metadata is not None and key_metadata.type not in {
                "char",
                "text",
                "selection",
            }:
                issues.append(
                    _issue(
                        "MAPPING_VALUE_MATCH_INVALID",
                        path,
                        "Quick matching requires a text-based Odoo key.",
                        "Use the normal governed mapping for this key type.",
                        dataset=dataset,
                    )
                )
        resolver_key_fields = tuple(
            item.target_field for item in resolver.key_mappings
        )
        resolver_scope_fields = tuple(
            item.target_field for item in resolver.scope_mappings
        )
        if require_governed_key and not (
            _matches_business_key(
                governed_keys,
                resolver.model,
                resolver_key_fields,
                resolver_scope_fields,
            )
            or matches_standard_reference_key(
                resolver.model,
                resolver_key_fields,
                resolver_scope_fields,
            )
        ):
            issues.append(
                _issue(
                    "MAPPING_BUSINESS_KEY_NOT_GOVERNED",
                    path,
                    "Resolver key and scope are not a confirmed business key.",
                    "Select one confirmed definition for the related model.",
                    dataset=dataset,
                )
            )

    @staticmethod
    def _validate_dependencies(
        dependencies: Mapping[str, set[str]],
        datasets: tuple[DatasetMapping, ...],
        issues: list[MappingValidationIssue],
    ) -> None:
        known = {item.dataset_id for item in datasets}
        for owner, targets in dependencies.items():
            for target in targets:
                if target not in known:
                    dataset = next(
                        (item for item in datasets if item.dataset_id == owner),
                        None,
                    )
                    issues.append(
                        _issue(
                            "MAPPING_DATASET_UNKNOWN",
                            "/datasets",
                            "An incoming relationship references an unknown dataset.",
                            "Choose a configured source dataset.",
                            dataset=dataset,
                        )
                    )
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                dataset = next(
                    (item for item in datasets if item.dataset_id == node),
                    None,
                )
                issues.append(
                    _issue(
                        "MAPPING_DEPENDENCY_CYCLE",
                        "/datasets",
                        "Incoming relationships contain a dependency cycle.",
                        "Remove the cycle or defer a reviewed multi-pass strategy.",
                        dataset=dataset,
                    )
                )
                return
            if node in visited:
                return
            visiting.add(node)
            for child in sorted(dependencies.get(node, ())):
                if child in known:
                    visit(child)
            visiting.remove(node)
            visited.add(node)

        for item in sorted(known):
            visit(item)


def _dataset_mapping_from_dict(payload: Mapping[str, Any]) -> DatasetMapping:
    return DatasetMapping(
        dataset_id=str(payload["dataset_id"]),
        target_model=str(payload.get("target_model", "")),
        mode=MappingTargetMode(payload.get("mode", "upsert")),
        on_existing=payload.get("on_existing"),
        source_identity_column_keys=tuple(
            payload.get("source_identity_column_keys", ())
        ),
        target_identity=tuple(
            _identity_component_from_dict(item)
            for item in payload.get("target_identity", ())
        ),
        target_scope=tuple(
            _identity_component_from_dict(item)
            for item in payload.get("target_scope", ())
        ),
        fields=tuple(
            _scalar_field_mapping_from_dict(item)
            for item in payload.get("fields", ())
        ),
        relationships=tuple(
            _relationship_from_dict(item)
            for item in payload.get("relationships", ())
        ),
        control_totals=tuple(
            BusinessControlTotal(
                name=str(item["name"]),
                target_field=str(item["target_field"]),
                expected_total=str(item["expected_total"]),
                unit=str(item.get("unit", "")),
                tolerance=str(item.get("tolerance", "0")),
            )
            for item in payload.get("control_totals", ())
        ),
    )


def _dataset_mapping_to_dict(
    mapping: DatasetMapping,
    contract_version: int,
) -> dict[str, Any]:
    payload = _portable(asdict(mapping))
    if contract_version < 3:
        for item in payload.get("fields", ()):
            item.pop("value_source", None)
            item.pop("literal_value", None)
            item.pop("transform", None)
    if contract_version < 4:
        for item in payload.get("fields", ()):
            item.pop("validation", None)
            transform = item.get("transform", {})
            for name in (
                "search_value",
                "replacement_value",
                "search_mode",
                "replace_all",
                "decimal_places",
                "rounding_mode",
                "formula",
            ):
                transform.pop(name, None)
    if contract_version < 5:
        for item in payload.get("fields", ()):
            item.pop("value_mappings", None)
        for component in (
            *payload.get("target_identity", ()),
            *payload.get("target_scope", ()),
        ):
            resolver = component.get("resolver")
            if resolver:
                resolver.pop("value_mappings", None)
        for relation in payload.get("relationships", ()):
            relation.get("resolver", {}).pop("value_mappings", None)
    if contract_version < 6:
        payload.pop("control_totals", None)
    return payload


def _scalar_field_mapping_from_dict(
    payload: Mapping[str, Any],
) -> ScalarFieldMapping:
    transform_payload = payload.get("transform", {})
    if not isinstance(transform_payload, Mapping):
        raise ValueError("Scalar transform policy must be an object")
    validation_payload = payload.get("validation", {})
    if not isinstance(validation_payload, Mapping):
        raise ValueError("Scalar validation policy must be an object")
    return ScalarFieldMapping(
        target_field=str(payload.get("target_field", "")),
        source_column_key=(
            str(payload["source_column_key"])
            if payload.get("source_column_key") is not None
            else None
        ),
        value_source=ScalarValueSource(
            payload.get("value_source", ScalarValueSource.SOURCE.value)
        ),
        literal_value=(
            str(payload["literal_value"])
            if payload.get("literal_value") is not None
            else None
        ),
        transform=ScalarTransformPolicy(
            trim=bool(transform_payload.get("trim", False)),
            collapse_whitespace=bool(
                transform_payload.get("collapse_whitespace", False)
            ),
            empty_as_null=bool(
                transform_payload.get("empty_as_null", False)
            ),
            case_mode=str(
                transform_payload.get("case_mode", "preserve")
            ),
            decimal_locale=str(
                transform_payload.get("decimal_locale", "invariant")
            ),
            date_format=str(
                transform_payload.get("date_format", "iso")
            ),
            timezone=str(transform_payload.get("timezone", "UTC")),
            search_value=str(transform_payload.get("search_value", "")),
            replacement_value=str(
                transform_payload.get("replacement_value", "")
            ),
            search_mode=str(transform_payload.get("search_mode", "literal")),
            replace_all=bool(transform_payload.get("replace_all", True)),
            decimal_places=(
                int(transform_payload["decimal_places"])
                if transform_payload.get("decimal_places") is not None
                else None
            ),
            rounding_mode=str(
                transform_payload.get("rounding_mode", "half_up")
            ),
            formula=str(transform_payload.get("formula", "")),
        ),
        validation=ScalarValidationPolicy(
            exact_length=(
                int(validation_payload["exact_length"])
                if validation_payload.get("exact_length") is not None
                else None
            ),
            segment_location=str(
                validation_payload.get("segment_location", "none")
            ),
            segment_length=(
                int(validation_payload["segment_length"])
                if validation_payload.get("segment_length") is not None
                else None
            ),
            character_class=str(
                validation_payload.get("character_class", "none")
            ),
            pattern=str(validation_payload.get("pattern", "")),
        ),
        value_mappings=tuple(
            ValueMapping(**item)
            for item in payload.get("value_mappings", ())
        ),
        value_type=str(payload.get("value_type", "string")),
        required=bool(payload.get("required", False)),
        required_on_create=bool(
            payload.get("required_on_create", False)
        ),
        compare=bool(payload.get("compare", True)),
        validate_only=bool(payload.get("validate_only", False)),
        null_policy=str(payload.get("null_policy", "distinct")),
    )


def _identity_component_from_dict(
    payload: Mapping[str, Any],
) -> IdentityComponentMapping:
    return IdentityComponentMapping(
        source_column_keys=tuple(payload.get("source_column_keys", ())),
        target_fields=tuple(payload.get("target_fields", ())),
        value_type=str(payload.get("value_type", "string")),
        resolver=(
            _resolver_from_dict(payload["resolver"])
            if payload.get("resolver") is not None
            else None
        ),
    )


def _relationship_from_dict(
    payload: Mapping[str, Any],
) -> RelationshipMapping:
    return RelationshipMapping(
        target_field=str(payload.get("target_field", "")),
        kind=str(payload.get("kind", "")),
        source_column_keys=tuple(payload.get("source_column_keys", ())),
        resolver=_resolver_from_dict(payload["resolver"]),
        compare=bool(payload.get("compare", True)),
        validate_only=bool(payload.get("validate_only", False)),
        required=bool(payload.get("required", False)),
        required_on_create=bool(payload.get("required_on_create", False)),
        on_missing=str(payload.get("on_missing", "error")),
        on_ambiguous=str(payload.get("on_ambiguous", "error")),
        operation=str(payload.get("operation", "replace")),
        separator=str(payload.get("separator", ";")),
        null_policy=str(payload.get("null_policy", "distinct")),
    )


def _resolver_from_dict(
    payload: Mapping[str, Any],
) -> RelationshipResolver:
    return RelationshipResolver(
        origin=ResolverOrigin(payload["origin"]),
        dataset_id=payload.get("dataset_id"),
        model=payload.get("model"),
        key_mappings=tuple(
            ReferenceKeyMapping(**item)
            for item in payload.get("key_mappings", ())
        ),
        scope_mappings=tuple(
            ReferenceKeyMapping(**item)
            for item in payload.get("scope_mappings", ())
        ),
        value_mappings=tuple(
            ValueMapping(**item)
            for item in payload.get("value_mappings", ())
        ),
    )


def _matches_business_key(
    definitions: Iterable[BusinessKeyDefinition],
    model: str,
    key_fields: tuple[str, ...],
    scope_fields: tuple[str, ...],
) -> bool:
    return any(
        item.model == model
        and item.status is BusinessKeyStatus.CONFIRMED
        and item.key_fields == key_fields
        and item.scope_fields == scope_fields
        for item in definitions
    )


def _check_column(
    dataset: DatasetMapping,
    column: str,
    path: str,
    columns: Mapping[str, Any],
    issues: list[MappingValidationIssue],
) -> None:
    if column not in columns:
        issues.append(
            _issue(
                "MAPPING_SOURCE_COLUMN_UNKNOWN",
                path,
                "The mapping references an unknown source column.",
                "Choose a column from the current frozen dataset.",
                dataset=dataset,
                source_column=column,
            )
        )


def _target_unknown(
    dataset: DatasetMapping,
    path: str,
    target_field: str,
) -> MappingValidationIssue:
    return _issue(
        "MAPPING_TARGET_FIELD_UNKNOWN",
        path,
        f"Target field {dataset.target_model}.{target_field} is unavailable.",
        "Choose a field from the captured schema.",
        dataset=dataset,
        target_field=target_field,
    )


def _issue(
    code: str,
    path: str,
    message: str,
    remediation: str,
    *,
    severity: str = "error",
    dataset: DatasetMapping | None = None,
    source_column: str | None = None,
    target_field: str | None = None,
) -> MappingValidationIssue:
    return MappingValidationIssue(
        code=code,
        severity=severity,
        path=path,
        message=message,
        remediation=remediation,
        dataset_id=dataset.dataset_id if dataset else None,
        source_column_key=source_column,
        target_model=dataset.target_model if dataset else None,
        target_field=target_field,
    )


def mapping_issue_fingerprint(issue: MappingValidationIssue) -> str:
    """Return the stable acknowledgement key for one validation issue."""

    return _content_hash(
        {
            "code": issue.code,
            "path": issue.path,
            "message": issue.message,
        }
    )


def _portable(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _portable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_portable(item) for item in value]
    if isinstance(value, list):
        return [_portable(item) for item in value]
    return value


def _content_hash(payload: object) -> str:
    return "sha256:" + sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()


def _canonical_json(payload: object) -> str:
    return json.dumps(
        _portable(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
