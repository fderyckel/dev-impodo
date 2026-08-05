"""Portable, versioned mapping-definition contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import json
from typing import Any, Mapping

from ...value_rules import ScalarTransformPolicy, ScalarValidationPolicy
from ..serialization import canonical_json as _canonical_json
from ..serialization import content_hash as _content_hash
from ..serialization import portable as _portable


MAPPING_CONTRACT_VERSION = 6
MAX_VALUE_MAPPINGS = 1_000
MAX_VALUE_MAPPING_LENGTH = 10_000
MAX_CONTROL_TOTALS_PER_DATASET = 3


class MappingTargetMode(StrEnum):
    UPSERT = "upsert"
    CREATE = "create"
    REFERENCE = "reference"


class ResolverOrigin(StrEnum):
    DATASET = "dataset"
    TARGET_CATALOG = "target_catalog"



class ScalarValueSource(StrEnum):
    """How one scalar target value is supplied."""

    SOURCE = "source"
    CONSTANT = "constant"
    SOURCE_WITH_FALLBACK = "source_with_fallback"
    ODOO_DEFAULT = "odoo_default"



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
