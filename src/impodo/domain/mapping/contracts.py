"""Define the portable Stage D mapping language and its semantic hash.

Layer: domain contracts. A ``MappingDefinition`` binds every dataset mapping to
one exact source-selection hash and schema/governance hash. Its nested objects
describe value providers, transformations, identities, relationships, target
modes, and declared control totals without executable Python/SQL or numeric
Odoo IDs.

See ``docs/architecture/python-code-map.md``,
``docs/developer/contracts/evidence-lifecycle.md``, and
``tests/domain/mapping/test_validation.py``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields as dataclass_fields, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import json
from typing import Any, Mapping
from uuid import UUID

from impodo.domain.recipe.value_rules import (
    ScalarTransformPolicy,
    ScalarValidationPolicy,
    TextTransformStep,
)
from ..serialization import canonical_json as _canonical_json
from ..serialization import content_hash as _content_hash
from ..serialization import portable as _portable


MAPPING_CONTRACT_VERSION = 12
MAX_VALUE_MAPPINGS = 1_000
MAX_VALUE_MAPPING_LENGTH = 10_000
MAX_CONTROL_TOTALS_PER_DATASET = 3
MAX_SELECTION_RULES = 20
MAX_SELECTION_RULE_CONDITIONS = 8
MAX_SELECTION_RULE_COLUMNS = 20


class MappingTargetMode(StrEnum):
    """Choose whether a dataset upserts, creates only, or resolves references."""

    UPSERT = "upsert"
    CREATE = "create"
    REFERENCE = "reference"
    ODOO_PINNED_UPDATE = "odoo_pinned_update"


class ResolverOrigin(StrEnum):
    """Resolve a logical reference from a prepared dataset or target snapshot."""

    DATASET = "dataset"
    TARGET_CATALOG = "target_catalog"



class ScalarValueSource(StrEnum):
    """How one scalar target value is supplied."""

    SOURCE = "source"
    CONSTANT = "constant"
    SOURCE_WITH_FALLBACK = "source_with_fallback"
    CONDITIONAL_RULES = "conditional_rules"
    ODOO_DEFAULT = "odoo_default"


class SelectionRuleJoin(StrEnum):
    """Combine the conditions in one ordered Selection-field rule."""

    ALL = "all"
    ANY = "any"


class SelectionConditionOperator(StrEnum):
    """Bounded comparisons available to a Selection-field rule."""

    IS_BLANK = "is_blank"
    IS_NOT_BLANK = "is_not_blank"
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    EQUALS_IGNORE_CASE = "equals_ignore_case"
    CONTAINS = "contains"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    LESS_THAN = "less_than"
    LESS_THAN_OR_EQUAL = "less_than_or_equal"
    GREATER_THAN = "greater_than"
    GREATER_THAN_OR_EQUAL = "greater_than_or_equal"
    IS_TRUE = "is_true"
    IS_FALSE = "is_false"


class CategoricalCoveragePolicy(StrEnum):
    """Declare how a bounded categorical source domain is closed."""

    EXACT_TARGET_VALUE = "EXACT_TARGET_VALUE"
    EXPLICIT_VALUE_MATCH = "EXPLICIT_VALUE_MATCH"
    EXACT_BUSINESS_KEY = "EXACT_BUSINESS_KEY"
    EXPLICIT_KEY_MATCH = "EXPLICIT_KEY_MATCH"


class TargetFieldHandling(StrEnum):
    """Explain why Impodo intentionally omits one required Odoo field."""

    ODOO_DEFAULT = "odoo_default"
    ODOO_MANAGED = "odoo_managed"


@dataclass(frozen=True, slots=True)
class TargetFieldDisposition:
    """Record an explicit, audited decision not to provide a target value."""

    target_field: str
    handling: TargetFieldHandling

    def __post_init__(self) -> None:
        if not self.target_field.strip() or len(self.target_field) > 200:
            raise ValueError("Target-field disposition is invalid")
        object.__setattr__(
            self,
            "handling",
            TargetFieldHandling(self.handling),
        )



@dataclass(frozen=True, slots=True)
class ReferenceKeyMapping:
    """Map one source key/scope component to one related target field."""

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


@dataclass(frozen=True, slots=True)
class ReferenceLookupMapping:
    """Resolve a scalar from one exact immutable reference-data package."""

    reference_id: str
    reference_content_hash: str
    key_source_column_keys: tuple[str, ...]
    value_field: str
    on_blank: str = "block"
    on_unknown: str = "block"

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "reference_id", str(UUID(self.reference_id)))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("Reference lookup identifier is invalid") from error
        if (
            not self.reference_content_hash.startswith("sha256:")
            or len(self.reference_content_hash) != 71
        ):
            raise ValueError("Reference lookup content hash is invalid")
        try:
            int(self.reference_content_hash[7:], 16)
        except ValueError as error:
            raise ValueError("Reference lookup content hash is invalid") from error
        if not 1 <= len(self.key_source_column_keys) <= 5:
            raise ValueError("Reference lookups require one to five key fields")
        if (
            len(set(self.key_source_column_keys)) != len(self.key_source_column_keys)
            or any(not item or len(item) > 500 for item in self.key_source_column_keys)
        ):
            raise ValueError("Reference lookup key fields are invalid")
        if not self.value_field or len(self.value_field) > 128:
            raise ValueError("Reference lookup output field is invalid")
        if self.on_blank not in {"block", "null"}:
            raise ValueError("Reference blank policy is unsupported")
        if self.on_unknown not in {"block", "null"}:
            raise ValueError("Reference unknown policy is unsupported")


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
    """Declare how symbolic relationship values become business references.

    Dataset resolvers name another prepared dataset. Target-catalog resolvers
    name an Odoo model that Stage H may read through governed key/scope fields.
    Optional value mappings are exact reviewed translations.
    """

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
    """Map one typed source identity component onto target key/scope fields."""

    source_column_keys: tuple[str, ...]
    target_fields: tuple[str, ...]
    value_type: str = "string"
    resolver: RelationshipResolver | None = None


@dataclass(frozen=True, slots=True)
class SelectionCondition:
    """Compare one frozen source column in a bounded Selection-field rule."""

    condition_id: str
    source_column_key: str
    operator: SelectionConditionOperator
    comparison_value: str | None = None
    value_type: str = "string"

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "condition_id", str(UUID(self.condition_id)))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("Selection-rule condition identifier is invalid") from error
        if not self.source_column_key or len(self.source_column_key) > 500:
            raise ValueError("Selection-rule source column is invalid")
        object.__setattr__(
            self,
            "operator",
            SelectionConditionOperator(self.operator),
        )
        if self.value_type not in {
            "string",
            "integer",
            "decimal",
            "boolean",
            "date",
            "datetime",
        }:
            raise ValueError("Selection-rule comparison type is unsupported")
        unary = {
            SelectionConditionOperator.IS_BLANK,
            SelectionConditionOperator.IS_NOT_BLANK,
            SelectionConditionOperator.IS_TRUE,
            SelectionConditionOperator.IS_FALSE,
        }
        if self.operator in unary:
            if self.comparison_value is not None:
                raise ValueError("This Selection-rule comparison takes no value")
        elif self.comparison_value is None:
            raise ValueError("This Selection-rule comparison requires a value")
        elif len(self.comparison_value) > 10_000:
            raise ValueError("Selection-rule comparison value is too long")


@dataclass(frozen=True, slots=True)
class SelectionRule:
    """Return one Odoo technical choice when its ordered conditions match."""

    rule_id: str
    conditions: tuple[SelectionCondition, ...]
    target_value: str
    join: SelectionRuleJoin = SelectionRuleJoin.ALL

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "rule_id", str(UUID(self.rule_id)))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("Selection-rule identifier is invalid") from error
        object.__setattr__(self, "conditions", tuple(self.conditions))
        object.__setattr__(self, "join", SelectionRuleJoin(self.join))
        if not 1 <= len(self.conditions) <= MAX_SELECTION_RULE_CONDITIONS:
            raise ValueError(
                "Each Selection rule requires one to "
                f"{MAX_SELECTION_RULE_CONDITIONS} conditions"
            )
        if len({item.condition_id for item in self.conditions}) != len(
            self.conditions
        ):
            raise ValueError("Selection-rule condition identifiers must be unique")
        if not self.target_value or len(self.target_value) > 10_000:
            raise ValueError("Selection-rule Odoo choice is invalid")


@dataclass(frozen=True, slots=True)
class SelectionRuleSet:
    """Evaluate ordered rules first-match-wins, then an optional otherwise."""

    rules: tuple[SelectionRule, ...]
    otherwise_value: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "rules", tuple(self.rules))
        if not 1 <= len(self.rules) <= MAX_SELECTION_RULES:
            raise ValueError(
                f"Selection fields require one to {MAX_SELECTION_RULES} rules"
            )
        if len({item.rule_id for item in self.rules}) != len(self.rules):
            raise ValueError("Selection-rule identifiers must be unique")
        source_columns = {
            condition.source_column_key
            for rule in self.rules
            for condition in rule.conditions
        }
        if len(source_columns) > MAX_SELECTION_RULE_COLUMNS:
            raise ValueError(
                "Selection rules can use at most "
                f"{MAX_SELECTION_RULE_COLUMNS} source columns"
            )
        if self.otherwise_value is not None and (
            not self.otherwise_value or len(self.otherwise_value) > 10_000
        ):
            raise ValueError("Selection-rule otherwise choice is invalid")


@dataclass(frozen=True, slots=True)
class ScalarFieldMapping:
    """Declare one scalar provider, transformation, validation, and comparison.

    ``value_source`` chooses source, constant, fallback, conditional rules, or
    Odoo-default intent.
    Runtime preparation uses the shared scalar evaluator; this contract carries
    only allowlisted policy and portable literal evidence.
    """

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
    reference_lookup: ReferenceLookupMapping | None = None
    categorical_policy: CategoricalCoveragePolicy | None = None
    selection_rules: SelectionRuleSet | None = None

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
        if self.categorical_policy is not None:
            object.__setattr__(
                self,
                "categorical_policy",
                CategoricalCoveragePolicy(self.categorical_policy),
            )
        if self.reference_lookup is not None:
            if self.value_source is not ScalarValueSource.SOURCE:
                raise ValueError("Reference lookups require a source value provider")
            if self.value_mappings:
                raise ValueError("Reference lookups cannot also use inline value matches")
            if self.source_column_key != self.reference_lookup.key_source_column_keys[0]:
                raise ValueError(
                    "Reference lookup source must be its first key field"
                )
            if self.required and (
                self.reference_lookup.on_blank == "null"
                or self.reference_lookup.on_unknown == "null"
            ):
                raise ValueError("Required reference lookups must block missing values")
        if self.value_source is ScalarValueSource.CONDITIONAL_RULES:
            if self.selection_rules is None:
                raise ValueError("Conditional Selection rules are required")
            if (
                self.source_column_key is not None
                or self.literal_value is not None
                or self.value_mappings
                or self.reference_lookup is not None
            ):
                raise ValueError(
                    "Conditional Selection rules cannot carry another value provider"
                )
        elif self.selection_rules is not None:
            raise ValueError(
                "Selection rules require the conditional-rules value provider"
            )



@dataclass(frozen=True, slots=True)
class RelationshipMapping:
    """Declare one symbolic many2one/one2many/many2many field mapping."""

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
    categorical_policy: CategoricalCoveragePolicy | None = None

    def __post_init__(self) -> None:
        if self.categorical_policy is not None:
            object.__setattr__(
                self,
                "categorical_policy",
                CategoricalCoveragePolicy(self.categorical_policy),
            )


@dataclass(frozen=True, slots=True)
class DatasetMapping:
    """Map one frozen or derived dataset to one target model and behavior.

    The object groups source/target identity, scope, scalar providers,
    relationship providers, target mode, and optional business control totals.
    Semantic validation, not ``__post_init__``, checks cross-object meaning.
    """

    dataset_id: str
    target_model: str
    mode: MappingTargetMode = MappingTargetMode.UPSERT
    on_existing: str | None = None
    source_identity_column_keys: tuple[str, ...] = ()
    target_identity: tuple[IdentityComponentMapping, ...] = ()
    target_scope: tuple[IdentityComponentMapping, ...] = ()
    fields: tuple[ScalarFieldMapping, ...] = ()
    relationships: tuple[RelationshipMapping, ...] = ()
    target_field_dispositions: tuple[TargetFieldDisposition, ...] = ()
    approved_write_fields: tuple[str, ...] = ()
    control_definitions: tuple["BusinessControlDefinition", ...] = ()
    control_expectations: tuple["MappingControlExpectation", ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", MappingTargetMode(self.mode))
        approved_write_fields = tuple(sorted(set(self.approved_write_fields)))
        if any(
            not item.strip() or len(item) > 200
            for item in approved_write_fields
        ):
            raise ValueError("Approved Odoo write field is invalid")
        object.__setattr__(
            self,
            "approved_write_fields",
            approved_write_fields,
        )
        if max(
            len(self.control_definitions),
            len(self.control_expectations),
        ) > MAX_CONTROL_TOTALS_PER_DATASET:
            raise ValueError(
                "A dataset has too many declared business control totals"
            )

    @property
    def effective_control_totals(self) -> tuple["BusinessControlTotal", ...]:
        """Project reusable definitions and edition expectations for runtime."""

        expected_by_id = {
            item.control_id: item for item in self.control_expectations
        }
        return tuple(
            BusinessControlTotal(
                name=definition.name,
                target_field=definition.target_field,
                expected_total=expected_by_id[definition.control_id].expected_total,
                unit=definition.unit,
                tolerance=definition.tolerance,
            )
            for definition in self.control_definitions
            if definition.control_id in expected_by_id
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
class BusinessControlDefinition:
    """Reusable meaning of one business reconciliation control."""

    control_id: str
    name: str
    target_field: str
    unit: str = ""
    tolerance: str = "0"
    calculation: str = "SUM"
    invariant_expectation: bool = False

    def __post_init__(self) -> None:
        control_id = self.control_id.strip()
        if not control_id or len(control_id) > 300:
            raise ValueError("Control identifier is invalid")
        normalized = BusinessControlTotal(
            name=self.name,
            target_field=self.target_field,
            expected_total="0",
            unit=self.unit,
            tolerance=self.tolerance,
        )
        if self.calculation != "SUM":
            raise ValueError("Control calculation is unsupported")
        object.__setattr__(self, "control_id", control_id)
        object.__setattr__(self, "name", normalized.name)
        object.__setattr__(self, "target_field", normalized.target_field)
        object.__setattr__(self, "unit", normalized.unit)
        object.__setattr__(self, "tolerance", normalized.tolerance)


@dataclass(frozen=True, slots=True)
class MappingControlExpectation:
    """Hashable mapping projection of one edition-local expected value."""

    control_id: str
    expected_total: str

    def __post_init__(self) -> None:
        control_id = self.control_id.strip()
        if not control_id or len(control_id) > 300:
            raise ValueError("Control expectation identifier is invalid")
        try:
            expected = Decimal(self.expected_total.strip())
        except (InvalidOperation, AttributeError) as error:
            raise ValueError("Control expectation requires a plain number") from error
        if not expected.is_finite():
            raise ValueError("Control expectation requires a finite number")
        object.__setattr__(self, "control_id", control_id)
        object.__setattr__(self, "expected_total", format(expected, "f"))


@dataclass(frozen=True, slots=True)
class MappingDefinition:
    """Portable mapping meaning, independent of revision/audit metadata."""

    mapping_id: str
    source_selection_hash: str
    schema_hash: str
    datasets: tuple[DatasetMapping, ...]
    contract_version: int = MAPPING_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != MAPPING_CONTRACT_VERSION:
            raise ValueError(
                "Mapping contract version does not match the current contract"
            )

    @property
    def content_hash(self) -> str:
        """Return the deterministic semantic identity of the complete mapping."""

        return _content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        """Return the canonically ordered current portable representation."""

        payload = {
            "mapping_id": self.mapping_id,
            "contract_version": self.contract_version,
            "source_selection_hash": self.source_selection_hash,
            "schema_hash": self.schema_hash,
            "datasets": [
                _dataset_mapping_to_dict(item)
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
                            target_field_dispositions=tuple(
                                sorted(
                                    item.target_field_dispositions,
                                    key=lambda disposition: (
                                        disposition.target_field,
                                        disposition.handling.value,
                                    ),
                                )
                            ),
                            approved_write_fields=tuple(
                                sorted(item.approved_write_fields)
                            ),
                            control_definitions=tuple(
                                sorted(
                                    item.control_definitions,
                                    key=lambda control: control.control_id,
                                )
                            ),
                            control_expectations=tuple(
                                sorted(
                                    item.control_expectations,
                                    key=lambda expectation: expectation.control_id,
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
        """Serialize the mapping with its content hash."""

        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MappingDefinition":
        """Restore the exact current mapping contract and verify its hash."""

        if set(payload) != {
            "mapping_id",
            "contract_version",
            "source_selection_hash",
            "schema_hash",
            "datasets",
            "content_hash",
        }:
            raise ValueError("Mapping fields do not match the current contract")
        if int(payload["contract_version"]) != MAPPING_CONTRACT_VERSION:
            raise ValueError(
                "Mapping contract version does not match the current contract"
            )
        definition = cls(
            mapping_id=str(payload["mapping_id"]),
            contract_version=int(payload["contract_version"]),
            source_selection_hash=str(payload["source_selection_hash"]),
            schema_hash=str(payload["schema_hash"]),
            datasets=tuple(
                _dataset_mapping_from_dict(item)
                for item in payload["datasets"]
            ),
        )
        if payload["content_hash"] != definition.content_hash:
            raise ValueError("Mapping-definition content hash is invalid")
        return definition

    @classmethod
    def from_json(cls, value: str) -> "MappingDefinition":
        """Restore a mapping definition from its portable JSON envelope."""

        return cls.from_dict(json.loads(value))



def _dataset_mapping_from_dict(
    payload: Mapping[str, Any],
) -> DatasetMapping:
    _require_contract_fields(
        payload,
        _contract_fields(DatasetMapping),
        "Dataset mapping fields do not match the current contract",
    )
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
        target_field_dispositions=tuple(
            _target_field_disposition_from_dict(item)
            for item in payload.get("target_field_dispositions", ())
        ),
        approved_write_fields=tuple(payload.get("approved_write_fields", ())),
        control_definitions=tuple(
            _business_control_definition_from_dict(item)
            for item in payload.get("control_definitions", ())
        ),
        control_expectations=tuple(
            _mapping_control_expectation_from_dict(item)
            for item in payload.get("control_expectations", ())
        ),
    )


def _dataset_mapping_to_dict(
    mapping: DatasetMapping,
) -> dict[str, Any]:
    return _portable(asdict(mapping))


def _scalar_field_mapping_from_dict(
    payload: Mapping[str, Any],
) -> ScalarFieldMapping:
    _require_contract_fields(
        payload,
        _contract_fields(ScalarFieldMapping),
        "Scalar mapping fields do not match the current contract",
    )
    transform_payload = payload.get("transform", {})
    if not isinstance(transform_payload, Mapping):
        raise ValueError("Scalar transform policy must be an object")
    if set(transform_payload) != set(asdict(ScalarTransformPolicy())):
        raise ValueError("Scalar transform fields do not match the current contract")
    validation_payload = payload.get("validation", {})
    if not isinstance(validation_payload, Mapping):
        raise ValueError("Scalar validation policy must be an object")
    if set(validation_payload) != set(asdict(ScalarValidationPolicy())):
        raise ValueError("Scalar validation fields do not match the current contract")
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
            decimal_places=(
                int(transform_payload["decimal_places"])
                if transform_payload.get("decimal_places") is not None
                else None
            ),
            rounding_mode=str(
                transform_payload.get("rounding_mode", "half_up")
            ),
            formula=str(transform_payload.get("formula", "")),
            text_steps=tuple(
                _text_transform_step_from_dict(
                    item,
                )
                for item in transform_payload.get("text_steps", ())
            ),
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
            _value_mapping_from_dict(item)
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
        reference_lookup=(
            _reference_lookup_from_dict(
                payload["reference_lookup"],
            )
            if payload.get("reference_lookup") is not None
            else None
        ),
        categorical_policy=(
            CategoricalCoveragePolicy(payload["categorical_policy"])
            if payload.get("categorical_policy") is not None
            else None
        ),
        selection_rules=(
            _selection_rule_set_from_dict(payload["selection_rules"])
            if payload.get("selection_rules") is not None
            else None
        ),
    )


def _selection_rule_set_from_dict(payload: Mapping[str, Any]) -> SelectionRuleSet:
    _require_contract_fields(
        payload,
        _contract_fields(SelectionRuleSet),
        "Selection rule set fields do not match contract v12",
    )
    rules_payload = payload.get("rules")
    if not isinstance(rules_payload, list):
        raise ValueError("Selection rules must be a list")
    return SelectionRuleSet(
        rules=tuple(_selection_rule_from_dict(item) for item in rules_payload),
        otherwise_value=(
            str(payload["otherwise_value"])
            if payload.get("otherwise_value") is not None
            else None
        ),
    )


def _selection_rule_from_dict(payload: Mapping[str, Any]) -> SelectionRule:
    _require_contract_fields(
        payload,
        _contract_fields(SelectionRule),
        "Selection rule fields do not match contract v12",
    )
    conditions_payload = payload.get("conditions")
    if not isinstance(conditions_payload, list):
        raise ValueError("Selection-rule conditions must be a list")
    return SelectionRule(
        rule_id=str(payload.get("rule_id", "")),
        conditions=tuple(
            _selection_condition_from_dict(item) for item in conditions_payload
        ),
        target_value=str(payload.get("target_value", "")),
        join=SelectionRuleJoin(payload.get("join", SelectionRuleJoin.ALL.value)),
    )


def _selection_condition_from_dict(payload: Mapping[str, Any]) -> SelectionCondition:
    _require_contract_fields(
        payload,
        _contract_fields(SelectionCondition),
        "Selection-rule condition fields do not match contract v12",
    )
    return SelectionCondition(
        condition_id=str(payload.get("condition_id", "")),
        source_column_key=str(payload.get("source_column_key", "")),
        operator=SelectionConditionOperator(payload.get("operator", "")),
        comparison_value=(
            str(payload["comparison_value"])
            if payload.get("comparison_value") is not None
            else None
        ),
        value_type=str(payload.get("value_type", "string")),
    )


def _text_transform_step_from_dict(
    payload: Mapping[str, Any],
) -> TextTransformStep:
    _require_contract_fields(
        payload,
        _contract_fields(TextTransformStep),
        "Ordered text changes are invalid",
    )
    return TextTransformStep(
        kind=str(payload.get("kind", "find_replace")),
        search_value=str(payload.get("search_value", "")),
        replacement_value=str(payload.get("replacement_value", "")),
        search_mode=str(payload.get("search_mode", "literal")),
        replace_all=bool(payload.get("replace_all", True)),
        characters=str(payload.get("characters", "")),
    )


def _identity_component_from_dict(
    payload: Mapping[str, Any],
) -> IdentityComponentMapping:
    _require_contract_fields(
        payload,
        _contract_fields(IdentityComponentMapping),
        "Identity mapping fields do not match the current contract",
    )
    return IdentityComponentMapping(
        source_column_keys=tuple(payload.get("source_column_keys", ())),
        target_fields=tuple(payload.get("target_fields", ())),
        value_type=str(payload.get("value_type", "string")),
        resolver=(
            _resolver_from_dict(
                payload["resolver"],
            )
            if payload.get("resolver") is not None
            else None
        ),
    )


def _relationship_from_dict(
    payload: Mapping[str, Any],
) -> RelationshipMapping:
    _require_contract_fields(
        payload,
        _contract_fields(RelationshipMapping),
        "Relationship mapping fields do not match the current contract",
    )
    return RelationshipMapping(
        target_field=str(payload.get("target_field", "")),
        kind=str(payload.get("kind", "")),
        source_column_keys=tuple(payload.get("source_column_keys", ())),
        resolver=_resolver_from_dict(
            payload["resolver"],
        ),
        compare=bool(payload.get("compare", True)),
        validate_only=bool(payload.get("validate_only", False)),
        required=bool(payload.get("required", False)),
        required_on_create=bool(payload.get("required_on_create", False)),
        on_missing=str(payload.get("on_missing", "error")),
        on_ambiguous=str(payload.get("on_ambiguous", "error")),
        operation=str(payload.get("operation", "replace")),
        separator=str(payload.get("separator", ";")),
        null_policy=str(payload.get("null_policy", "distinct")),
        categorical_policy=(
            CategoricalCoveragePolicy(payload["categorical_policy"])
            if payload.get("categorical_policy") is not None
            else None
        ),
    )


def _resolver_from_dict(
    payload: Mapping[str, Any],
) -> RelationshipResolver:
    _require_contract_fields(
        payload,
        _contract_fields(RelationshipResolver),
        "Relationship resolver fields do not match the current contract",
    )
    return RelationshipResolver(
        origin=ResolverOrigin(payload["origin"]),
        dataset_id=payload.get("dataset_id"),
        model=payload.get("model"),
        key_mappings=tuple(
            _reference_key_mapping_from_dict(
                item,
            )
            for item in payload.get("key_mappings", ())
        ),
        scope_mappings=tuple(
            _reference_key_mapping_from_dict(
                item,
            )
            for item in payload.get("scope_mappings", ())
        ),
        value_mappings=tuple(
            _value_mapping_from_dict(item)
            for item in payload.get("value_mappings", ())
        ),
    )


def _contract_fields(contract_type: type[object]) -> set[str]:
    return {item.name for item in dataclass_fields(contract_type)}


def _require_contract_fields(
    payload: object,
    expected: set[str],
    message: str,
) -> None:
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise ValueError(message)


def _target_field_disposition_from_dict(
    payload: Mapping[str, Any],
) -> TargetFieldDisposition:
    _require_contract_fields(
        payload,
        _contract_fields(TargetFieldDisposition),
        "Target-field disposition fields do not match the current contract",
    )
    return TargetFieldDisposition(
        target_field=str(payload.get("target_field", "")),
        handling=TargetFieldHandling(
            payload.get("handling", TargetFieldHandling.ODOO_DEFAULT.value)
        ),
    )


def _business_control_definition_from_dict(
    payload: Mapping[str, Any],
) -> BusinessControlDefinition:
    _require_contract_fields(
        payload,
        _contract_fields(BusinessControlDefinition),
        "Control-definition fields do not match contract v11",
    )
    return BusinessControlDefinition(**payload)


def _mapping_control_expectation_from_dict(
    payload: Mapping[str, Any],
) -> MappingControlExpectation:
    _require_contract_fields(
        payload,
        _contract_fields(MappingControlExpectation),
        "Control-expectation fields do not match contract v11",
    )
    return MappingControlExpectation(**payload)


def _reference_lookup_from_dict(
    payload: Mapping[str, Any],
) -> ReferenceLookupMapping:
    _require_contract_fields(
        payload,
        _contract_fields(ReferenceLookupMapping),
        "Reference-lookup fields do not match the current contract",
    )
    return ReferenceLookupMapping(**payload)


def _reference_key_mapping_from_dict(
    payload: Mapping[str, Any],
) -> ReferenceKeyMapping:
    _require_contract_fields(
        payload,
        _contract_fields(ReferenceKeyMapping),
        "Reference-key fields do not match the current contract",
    )
    return ReferenceKeyMapping(**payload)


def _value_mapping_from_dict(
    payload: Mapping[str, Any],
) -> ValueMapping:
    _require_contract_fields(
        payload,
        _contract_fields(ValueMapping),
        "Value-match fields do not match the current contract",
    )
    return ValueMapping(**payload)
