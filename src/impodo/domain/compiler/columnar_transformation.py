"""Compile browser mappings into backend-neutral columnar programs.

This module is the semantic boundary between browser-authored mapping policy
and a future native execution adapter.  It deliberately has no Polars import:
the program describes ordered providers, expressions, conversions, checks,
identity work, lineage, impact accounting, and set/global requirements using
portable domain contracts only.

Slice 3 does not select or execute a new production backend.  Unsupported
semantics produce deterministic whole-dataset fallback reasons so Slice 4 can
route a dataset before reading any row and can never fall back per cell.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Mapping, Sequence, cast

from ...workspace_contracts import SourceDataset, SourceSelection
from ..mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ResolverOrigin,
    ScalarFieldMapping,
    ScalarValueSource,
    TargetFieldHandling,
)
from ..mapping.descriptions import transformation_rule_summary
from ..serialization import content_hash, portable


COLUMNAR_PROGRAM_CONTRACT_VERSION = 4
COLUMNAR_COMPILER_VERSION = 4


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_integer(value: object) -> int | None:
    return None if value is None else int(cast(int, value))


def _optional_boolean(value: object) -> bool | None:
    return None if value is None else bool(value)


class ColumnarCompilationError(ValueError):
    """A mapping and frozen selection cannot form a coherent program."""


class ColumnarExecutionClass(StrEnum):
    """Locate one operation in native, set/global, or oracle execution."""

    NATIVE_COLUMNAR = "native_columnar"
    SET_GLOBAL = "set_global"
    PYTHON_ORACLE = "python_oracle"


class ColumnarSupport(StrEnum):
    """Choose one backend for the complete effective dataset."""

    SUPPORTED = "supported"
    PYTHON_FALLBACK = "python_fallback"


class ColumnarDatasetKind(StrEnum):
    """Describe how an effective dataset originated before transformation."""

    DIRECT = "direct"
    RELATED_PARENT = "related_parent"
    RELATED_CHILD = "related_child"
    DERIVED_LOOKUP = "derived_lookup"
    STRUCTURAL = "structural"


class ColumnarOperationKind(StrEnum):
    """Every operation currently considered by the capability compiler."""

    READ_SOURCE = "read_source"
    USE_CONSTANT = "use_constant"
    SOURCE_FALLBACK = "source_fallback"
    CONDITIONAL_SELECTION = "conditional_selection"
    OMIT_ODOO_DEFAULT = "omit_odoo_default"
    OMIT_ODOO_MANAGED = "omit_odoo_managed"
    INLINE_VALUE_MAPPING = "inline_value_mapping"
    REFERENCE_LOOKUP = "reference_lookup"
    FORMULA = "formula"
    RENDER_TEXT = "render_text"
    TRIM = "trim"
    COLLAPSE_WHITESPACE = "collapse_whitespace"
    REPLACE_LITERAL = "replace_literal"
    REPLACE_PREFIX = "replace_prefix"
    REPLACE_SUFFIX = "replace_suffix"
    REPLACE_PATTERN = "replace_pattern"
    REMOVE_SEPARATORS_BETWEEN_DIGITS = "remove_separators_between_digits"
    CASE_UPPER = "case_upper"
    CASE_LOWER = "case_lower"
    CASE_SENTENCE = "case_sentence"
    CASE_TITLE = "case_title"
    EMPTY_AS_NULL = "empty_as_null"
    VALIDATE_RULE_OUTPUT_LENGTH = "validate_rule_output_length"
    REQUIRE_VALUE = "require_value"
    PARSE_STRING = "parse_string"
    PARSE_INTEGER = "parse_integer"
    PARSE_DECIMAL = "parse_decimal"
    ROUND_DECIMAL = "round_decimal"
    PARSE_BOOLEAN = "parse_boolean"
    PARSE_DATE = "parse_date"
    PARSE_ISO_DATE = "parse_iso_date"
    PARSE_DATETIME = "parse_datetime"
    PARSE_ISO_DATETIME = "parse_iso_datetime"
    VALIDATE_EXACT_LENGTH = "validate_exact_length"
    VALIDATE_CHARACTER_CLASS = "validate_character_class"
    VALIDATE_TYPED_VALUE = "validate_typed_value"
    VALIDATE_PATTERN = "validate_pattern"
    SOURCE_IDENTITY_NORMALIZATION = "source_identity_normalization"
    TARGET_IDENTITY_NORMALIZATION = "target_identity_normalization"
    IDENTITY_RESOLVER = "identity_resolver"
    RELATIONSHIP_POLICY = "relationship_policy"
    RELATIONSHIP_KEY_NORMALIZATION = "relationship_key_normalization"
    RELATIONSHIP_RESOLUTION = "relationship_resolution"
    DIRECT_LINEAGE = "direct_lineage"
    TRANSFORMATION_IMPACT = "transformation_impact"
    DUPLICATE_IDENTITY_GROUPING = "duplicate_identity_grouping"
    CONTROL_TOTAL = "control_total"
    NON_DIRECT_DATASET = "non_direct_dataset"


@dataclass(frozen=True, slots=True)
class ColumnarCapabilityDefinition:
    """One stable entry in the complete compiler capability matrix."""

    operation: ColumnarOperationKind
    execution_class: ColumnarExecutionClass
    fallback_code: str | None = None
    fallback_message: str | None = None

    def __post_init__(self) -> None:
        oracle = self.execution_class is ColumnarExecutionClass.PYTHON_ORACLE
        if oracle != bool(self.fallback_code and self.fallback_message):
            raise ValueError(
                "Python-oracle capabilities require one explicit fallback reason"
            )

    def to_portable_dict(self) -> dict[str, object]:
        return cast(dict[str, object], portable(asdict(self)))


def _native(operation: ColumnarOperationKind) -> ColumnarCapabilityDefinition:
    return ColumnarCapabilityDefinition(
        operation=operation,
        execution_class=ColumnarExecutionClass.NATIVE_COLUMNAR,
    )


def _set_global(operation: ColumnarOperationKind) -> ColumnarCapabilityDefinition:
    return ColumnarCapabilityDefinition(
        operation=operation,
        execution_class=ColumnarExecutionClass.SET_GLOBAL,
    )


def _oracle(
    operation: ColumnarOperationKind,
    code: str,
    message: str,
) -> ColumnarCapabilityDefinition:
    return ColumnarCapabilityDefinition(
        operation=operation,
        execution_class=ColumnarExecutionClass.PYTHON_ORACLE,
        fallback_code=code,
        fallback_message=message,
    )


COLUMNAR_CAPABILITY_MATRIX = (
    _native(ColumnarOperationKind.READ_SOURCE),
    _native(ColumnarOperationKind.USE_CONSTANT),
    _native(ColumnarOperationKind.SOURCE_FALLBACK),
    _native(ColumnarOperationKind.CONDITIONAL_SELECTION),
    _native(ColumnarOperationKind.OMIT_ODOO_DEFAULT),
    _native(ColumnarOperationKind.OMIT_ODOO_MANAGED),
    _native(ColumnarOperationKind.INLINE_VALUE_MAPPING),
    _oracle(
        ColumnarOperationKind.REFERENCE_LOOKUP,
        "COLUMNAR_REFERENCE_LOOKUP_UNSUPPORTED",
        "Approved reference lookups still require the Python oracle.",
    ),
    _oracle(
        ColumnarOperationKind.FORMULA,
        "COLUMNAR_FORMULA_UNSUPPORTED",
        "Formula evaluation still requires the Python oracle.",
    ),
    _native(ColumnarOperationKind.RENDER_TEXT),
    _native(ColumnarOperationKind.TRIM),
    _native(ColumnarOperationKind.COLLAPSE_WHITESPACE),
    _native(ColumnarOperationKind.REPLACE_LITERAL),
    _native(ColumnarOperationKind.REPLACE_PREFIX),
    _native(ColumnarOperationKind.REPLACE_SUFFIX),
    _oracle(
        ColumnarOperationKind.REPLACE_PATTERN,
        "COLUMNAR_PATTERN_REPLACEMENT_UNSUPPORTED",
        "Pattern replacement still requires the Python oracle.",
    ),
    _oracle(
        ColumnarOperationKind.REMOVE_SEPARATORS_BETWEEN_DIGITS,
        "COLUMNAR_SEPARATOR_REMOVAL_UNSUPPORTED",
        "Guided separator removal still requires the Python oracle.",
    ),
    _native(ColumnarOperationKind.CASE_UPPER),
    _native(ColumnarOperationKind.CASE_LOWER),
    _oracle(
        ColumnarOperationKind.CASE_SENTENCE,
        "COLUMNAR_SENTENCE_CASE_UNSUPPORTED",
        "Sentence-case Unicode parity has not been proven for native execution.",
    ),
    _oracle(
        ColumnarOperationKind.CASE_TITLE,
        "COLUMNAR_TITLE_CASE_UNSUPPORTED",
        "Title-case Unicode parity has not been proven for native execution.",
    ),
    _native(ColumnarOperationKind.EMPTY_AS_NULL),
    _native(ColumnarOperationKind.VALIDATE_RULE_OUTPUT_LENGTH),
    _native(ColumnarOperationKind.REQUIRE_VALUE),
    _native(ColumnarOperationKind.PARSE_STRING),
    _native(ColumnarOperationKind.PARSE_INTEGER),
    _native(ColumnarOperationKind.PARSE_DECIMAL),
    _oracle(
        ColumnarOperationKind.ROUND_DECIMAL,
        "COLUMNAR_DECIMAL_ROUNDING_UNSUPPORTED",
        "Arbitrary-precision decimal rounding still requires the Python oracle.",
    ),
    _native(ColumnarOperationKind.PARSE_BOOLEAN),
    _native(ColumnarOperationKind.PARSE_DATE),
    _oracle(
        ColumnarOperationKind.PARSE_ISO_DATE,
        "COLUMNAR_ISO_DATE_UNSUPPORTED",
        (
            "Python ISO-date identity grammar is broader than the currently "
            "proven native parser."
        ),
    ),
    _native(ColumnarOperationKind.PARSE_DATETIME),
    _oracle(
        ColumnarOperationKind.PARSE_ISO_DATETIME,
        "COLUMNAR_ISO_DATETIME_UNSUPPORTED",
        (
            "Python ISO-datetime grammar is broader than the currently proven "
            "native parser."
        ),
    ),
    _native(ColumnarOperationKind.VALIDATE_EXACT_LENGTH),
    _native(ColumnarOperationKind.VALIDATE_CHARACTER_CLASS),
    _oracle(
        ColumnarOperationKind.VALIDATE_TYPED_VALUE,
        "COLUMNAR_TYPED_VALIDATION_UNSUPPORTED",
        (
            "Validation of converted non-string values still requires the "
            "Python oracle."
        ),
    ),
    _oracle(
        ColumnarOperationKind.VALIDATE_PATTERN,
        "COLUMNAR_CUSTOM_PATTERN_UNSUPPORTED",
        "Custom-pattern validation still requires the Python oracle.",
    ),
    _native(ColumnarOperationKind.SOURCE_IDENTITY_NORMALIZATION),
    _native(ColumnarOperationKind.TARGET_IDENTITY_NORMALIZATION),
    _oracle(
        ColumnarOperationKind.IDENTITY_RESOLVER,
        "COLUMNAR_IDENTITY_RESOLVER_UNSUPPORTED",
        "Relational identity and scope components still require the Python oracle.",
    ),
    _oracle(
        ColumnarOperationKind.RELATIONSHIP_POLICY,
        "COLUMNAR_RELATIONSHIP_UNSUPPORTED",
        "This relationship shape still requires the Python oracle.",
    ),
    _native(ColumnarOperationKind.RELATIONSHIP_KEY_NORMALIZATION),
    _set_global(ColumnarOperationKind.RELATIONSHIP_RESOLUTION),
    _native(ColumnarOperationKind.DIRECT_LINEAGE),
    _native(ColumnarOperationKind.TRANSFORMATION_IMPACT),
    _set_global(ColumnarOperationKind.DUPLICATE_IDENTITY_GROUPING),
    _set_global(ColumnarOperationKind.CONTROL_TOTAL),
    _oracle(
        ColumnarOperationKind.NON_DIRECT_DATASET,
        "COLUMNAR_NON_DIRECT_DATASET_UNSUPPORTED",
        "Related, derived, and structural datasets are outside the direct native slice.",
    ),
)

_CAPABILITY_BY_OPERATION = {
    item.operation: item for item in COLUMNAR_CAPABILITY_MATRIX
}
if set(_CAPABILITY_BY_OPERATION) != set(ColumnarOperationKind):
    raise RuntimeError("The columnar capability matrix is incomplete")


@dataclass(frozen=True, slots=True)
class ColumnarInputColumn:
    """One selected source input addressed by stable key and ordinal."""

    stable_key: str
    ordinal: int
    source_name: str
    candidate_type: str

    def to_portable_dict(self) -> dict[str, object]:
        return cast(dict[str, object], portable(asdict(self)))


@dataclass(frozen=True, slots=True)
class ColumnarExpressionStep:
    """One ordered expression with a bounded, operation-specific argument."""

    operation: ColumnarOperationKind
    text: str | None = None
    replacement: str | None = None
    integer: int | None = None
    flag: bool | None = None
    character_class: str | None = None
    segment_location: str | None = None
    segment_length: int | None = None
    error_code: str | None = None

    def to_portable_dict(self) -> dict[str, object]:
        return cast(dict[str, object], portable(asdict(self)))


@dataclass(frozen=True, slots=True)
class ColumnarSelectionConditionProgram:
    """One source comparison in a native conditional Selection provider."""

    source: ColumnarInputColumn
    operator: str
    comparison_value: str | None
    value_type: str


@dataclass(frozen=True, slots=True)
class ColumnarSelectionRuleProgram:
    """One ordered first-match-wins branch in a Selection provider."""

    conditions: tuple[ColumnarSelectionConditionProgram, ...]
    target_value: str
    join: str


@dataclass(frozen=True, slots=True)
class ColumnarValueProviderProgram:
    """Provide a scalar and preserve the evaluator's conditional branches.

    Inline value matching is evaluated first against ``str(raw).strip()`` and
    a match bypasses the text-rule steps, but not required/conversion/final
    validation.  Source-with-fallback uses the probe steps to decide whether
    to use the literal, then applies the normal field text-rule steps to the
    selected value exactly as the Python oracle does.
    """

    operation: ColumnarOperationKind
    source: ColumnarInputColumn | None
    literal_value: str | None
    value_mappings: tuple[tuple[str, str], ...]
    fallback_probe_steps: tuple[ColumnarExpressionStep, ...] = ()
    value_mapping_bypasses_transforms: bool = True
    selection_rules: tuple[ColumnarSelectionRuleProgram, ...] = ()
    selection_otherwise_value: str | None = None

    def to_portable_dict(self) -> dict[str, object]:
        return cast(dict[str, object], portable(asdict(self)))


@dataclass(frozen=True, slots=True)
class ColumnarFailureSemantics:
    """Portable final issue outcomes at the current canonical boundary."""

    required_value_code: str = "SOURCE_REQUIRED_VALUE_MISSING"
    parse_value_code: str = "SOURCE_TYPE_INVALID"
    invalid_rule_value: str = "Invalid"

    def to_portable_dict(self) -> dict[str, object]:
        return cast(dict[str, object], portable(asdict(self)))


@dataclass(frozen=True, slots=True)
class ColumnarScalarFieldProgram:
    """One canonical scalar output and its exact ordered expression program."""

    target_field: str
    output_ordinal: int
    value_type: str
    source_label: str
    transformation_rules: str
    provider: ColumnarValueProviderProgram
    transform_steps: tuple[ColumnarExpressionStep, ...]
    required_step: ColumnarExpressionStep | None
    conversion_step: ColumnarExpressionStep
    post_conversion_steps: tuple[ColumnarExpressionStep, ...]
    validation_steps: tuple[ColumnarExpressionStep, ...]
    required: bool
    required_on_create: bool
    compare: bool
    validate_only: bool
    null_policy: str
    impact_required: bool
    failures: ColumnarFailureSemantics

    def to_portable_dict(self) -> dict[str, object]:
        return cast(dict[str, object], portable(asdict(self)))


@dataclass(frozen=True, slots=True)
class ColumnarIdentityComponentProgram:
    """One ordered source, target-identity, or target-scope component."""

    role: str
    source_columns: tuple[ColumnarInputColumn, ...]
    source_label: str
    target_fields: tuple[str, ...]
    value_type: str
    normalization_steps: tuple[ColumnarExpressionStep, ...]
    required: bool = True
    failure_code: str = "SOURCE_IDENTITY_INVALID"

    def to_portable_dict(self) -> dict[str, object]:
        return cast(dict[str, object], portable(asdict(self)))


@dataclass(frozen=True, slots=True)
class ColumnarRelationshipProgram:
    """One incoming many2one key normalized once for all consumers."""

    target_field: str
    parent_dataset_id: str
    parent_dataset_name: str
    key: ColumnarIdentityComponentProgram
    compare: bool
    validate_only: bool
    required: bool
    required_on_create: bool
    on_missing: str
    on_ambiguous: str
    operation: str
    null_policy: str

    def to_portable_dict(self) -> dict[str, object]:
        return cast(dict[str, object], portable(asdict(self)))


@dataclass(frozen=True, slots=True)
class ColumnarSetRequirement:
    """One set/global fact computed after native row-local expressions."""

    operation: ColumnarOperationKind
    target_field: str | None = None
    name: str | None = None

    def to_portable_dict(self) -> dict[str, object]:
        return cast(dict[str, object], portable(asdict(self)))


@dataclass(frozen=True, slots=True)
class ColumnarCapabilityUse:
    """One path-specific use of a capability during compilation."""

    path: str
    operation: ColumnarOperationKind
    target_field: str | None = None

    @property
    def execution_class(self) -> ColumnarExecutionClass:
        return _CAPABILITY_BY_OPERATION[self.operation].execution_class

    def to_portable_dict(self) -> dict[str, object]:
        return {
            "execution_class": self.execution_class.value,
            "operation": self.operation.value,
            "path": self.path,
            "target_field": self.target_field,
        }


@dataclass(frozen=True, slots=True)
class ColumnarFallbackReason:
    """One stable reason the complete dataset must use the Python oracle."""

    code: str
    message: str
    path: str
    operation: ColumnarOperationKind
    target_field: str | None = None

    def to_portable_dict(self) -> dict[str, object]:
        return cast(dict[str, object], portable(asdict(self)))


@dataclass(frozen=True, slots=True)
class ColumnarTransformationProgram:
    """Complete backend-neutral native program for one direct dataset."""

    dataset_id: str
    dataset_name: str
    target_model: str
    target_mode: str
    mapping_content_hash: str
    source_selection_hash: str
    schema_hash: str
    inputs: tuple[ColumnarInputColumn, ...]
    source_identity: tuple[ColumnarIdentityComponentProgram, ...]
    target_identity: tuple[ColumnarIdentityComponentProgram, ...]
    target_scope: tuple[ColumnarIdentityComponentProgram, ...]
    scalar_fields: tuple[ColumnarScalarFieldProgram, ...]
    relationships: tuple[ColumnarRelationshipProgram, ...]
    set_requirements: tuple[ColumnarSetRequirement, ...]
    preserve_source_row: bool = True
    preserve_source_order: bool = True
    sparse_transformation_impacts: bool = True
    contract_version: int = COLUMNAR_PROGRAM_CONTRACT_VERSION
    compiler_version: int = COLUMNAR_COMPILER_VERSION

    def to_portable_dict(self) -> dict[str, object]:
        return cast(dict[str, object], portable(asdict(self)))

    @classmethod
    def from_portable_dict(
        cls,
        payload: Mapping[str, object],
    ) -> "ColumnarTransformationProgram":
        """Restore one exact persisted native program for value projection."""

        def input_column(value: object) -> ColumnarInputColumn:
            item = cast(Mapping[str, object], value)
            return ColumnarInputColumn(
                stable_key=str(item["stable_key"]),
                ordinal=int(cast(int, item["ordinal"])),
                source_name=str(item["source_name"]),
                candidate_type=str(item["candidate_type"]),
            )

        def step(value: object) -> ColumnarExpressionStep:
            item = cast(Mapping[str, object], value)
            return ColumnarExpressionStep(
                operation=ColumnarOperationKind(str(item["operation"])),
                text=_optional_string(item.get("text")),
                replacement=_optional_string(item.get("replacement")),
                integer=_optional_integer(item.get("integer")),
                flag=_optional_boolean(item.get("flag")),
                character_class=_optional_string(item.get("character_class")),
                segment_location=_optional_string(item.get("segment_location")),
                segment_length=_optional_integer(item.get("segment_length")),
                error_code=_optional_string(item.get("error_code")),
            )

        def provider(value: object) -> ColumnarValueProviderProgram:
            item = cast(Mapping[str, object], value)
            raw_source = item.get("source")
            selection_rules = []
            for raw_rule in cast(
                Sequence[Mapping[str, object]],
                item.get("selection_rules", ()),
            ):
                selection_rules.append(
                    ColumnarSelectionRuleProgram(
                        conditions=tuple(
                            ColumnarSelectionConditionProgram(
                                source=input_column(raw_condition["source"]),
                                operator=str(raw_condition["operator"]),
                                comparison_value=_optional_string(
                                    raw_condition.get("comparison_value")
                                ),
                                value_type=str(raw_condition["value_type"]),
                            )
                            for raw_condition in cast(
                                Sequence[Mapping[str, object]],
                                raw_rule["conditions"],
                            )
                        ),
                        target_value=str(raw_rule["target_value"]),
                        join=str(raw_rule["join"]),
                    )
                )
            return ColumnarValueProviderProgram(
                operation=ColumnarOperationKind(str(item["operation"])),
                source=(input_column(raw_source) if raw_source is not None else None),
                literal_value=_optional_string(item.get("literal_value")),
                value_mappings=tuple(
                    (str(pair[0]), str(pair[1]))
                    for pair in cast(Sequence[Sequence[object]], item["value_mappings"])
                ),
                fallback_probe_steps=tuple(
                    step(item_step)
                    for item_step in cast(
                        Sequence[object],
                        item.get("fallback_probe_steps", ()),
                    )
                ),
                value_mapping_bypasses_transforms=bool(
                    item.get("value_mapping_bypasses_transforms", True)
                ),
                selection_rules=tuple(selection_rules),
                selection_otherwise_value=_optional_string(
                    item.get("selection_otherwise_value")
                ),
            )

        def failures(value: object) -> ColumnarFailureSemantics:
            item = cast(Mapping[str, object], value)
            return ColumnarFailureSemantics(
                required_value_code=str(item["required_value_code"]),
                parse_value_code=str(item["parse_value_code"]),
                invalid_rule_value=str(item["invalid_rule_value"]),
            )

        def scalar(value: object) -> ColumnarScalarFieldProgram:
            item = cast(Mapping[str, object], value)
            raw_required = item.get("required_step")
            return ColumnarScalarFieldProgram(
                target_field=str(item["target_field"]),
                output_ordinal=int(cast(int, item["output_ordinal"])),
                value_type=str(item["value_type"]),
                source_label=str(item["source_label"]),
                transformation_rules=str(item["transformation_rules"]),
                provider=provider(item["provider"]),
                transform_steps=tuple(
                    step(item_step)
                    for item_step in cast(Sequence[object], item["transform_steps"])
                ),
                required_step=(
                    step(raw_required) if raw_required is not None else None
                ),
                conversion_step=step(item["conversion_step"]),
                post_conversion_steps=tuple(
                    step(item_step)
                    for item_step in cast(
                        Sequence[object],
                        item["post_conversion_steps"],
                    )
                ),
                validation_steps=tuple(
                    step(item_step)
                    for item_step in cast(Sequence[object], item["validation_steps"])
                ),
                required=bool(item["required"]),
                required_on_create=bool(item["required_on_create"]),
                compare=bool(item["compare"]),
                validate_only=bool(item["validate_only"]),
                null_policy=str(item["null_policy"]),
                impact_required=bool(item["impact_required"]),
                failures=failures(item["failures"]),
            )

        def identity(value: object) -> ColumnarIdentityComponentProgram:
            item = cast(Mapping[str, object], value)
            return ColumnarIdentityComponentProgram(
                role=str(item["role"]),
                source_columns=tuple(
                    input_column(column)
                    for column in cast(Sequence[object], item["source_columns"])
                ),
                source_label=str(item["source_label"]),
                target_fields=tuple(
                    str(field)
                    for field in cast(Sequence[object], item["target_fields"])
                ),
                value_type=str(item["value_type"]),
                normalization_steps=tuple(
                    step(item_step)
                    for item_step in cast(
                        Sequence[object],
                        item["normalization_steps"],
                    )
                ),
                required=bool(item.get("required", True)),
                failure_code=str(
                    item.get("failure_code", "SOURCE_IDENTITY_INVALID")
                ),
            )

        def relationship(value: object) -> ColumnarRelationshipProgram:
            item = cast(Mapping[str, object], value)
            return ColumnarRelationshipProgram(
                target_field=str(item["target_field"]),
                parent_dataset_id=str(item["parent_dataset_id"]),
                parent_dataset_name=str(item["parent_dataset_name"]),
                key=identity(item["key"]),
                compare=bool(item["compare"]),
                validate_only=bool(item["validate_only"]),
                required=bool(item["required"]),
                required_on_create=bool(item["required_on_create"]),
                on_missing=str(item["on_missing"]),
                on_ambiguous=str(item["on_ambiguous"]),
                operation=str(item["operation"]),
                null_policy=str(item["null_policy"]),
            )

        def set_requirement(value: object) -> ColumnarSetRequirement:
            item = cast(Mapping[str, object], value)
            return ColumnarSetRequirement(
                operation=ColumnarOperationKind(str(item["operation"])),
                target_field=_optional_string(item.get("target_field")),
                name=_optional_string(item.get("name")),
            )

        program = cls(
            dataset_id=str(payload["dataset_id"]),
            dataset_name=str(payload["dataset_name"]),
            target_model=str(payload["target_model"]),
            target_mode=str(payload["target_mode"]),
            mapping_content_hash=str(payload["mapping_content_hash"]),
            source_selection_hash=str(payload["source_selection_hash"]),
            schema_hash=str(payload["schema_hash"]),
            inputs=tuple(
                input_column(item)
                for item in cast(Sequence[object], payload["inputs"])
            ),
            source_identity=tuple(
                identity(item)
                for item in cast(Sequence[object], payload["source_identity"])
            ),
            target_identity=tuple(
                identity(item)
                for item in cast(Sequence[object], payload["target_identity"])
            ),
            target_scope=tuple(
                identity(item)
                for item in cast(Sequence[object], payload["target_scope"])
            ),
            scalar_fields=tuple(
                scalar(item)
                for item in cast(Sequence[object], payload["scalar_fields"])
            ),
            relationships=tuple(
                relationship(item)
                for item in cast(Sequence[object], payload["relationships"])
            ),
            set_requirements=tuple(
                set_requirement(item)
                for item in cast(Sequence[object], payload["set_requirements"])
            ),
            preserve_source_row=bool(payload.get("preserve_source_row", True)),
            preserve_source_order=bool(payload.get("preserve_source_order", True)),
            sparse_transformation_impacts=bool(
                payload.get("sparse_transformation_impacts", True)
            ),
            contract_version=int(
                cast(int, payload["contract_version"])
            ),
            compiler_version=int(
                cast(int, payload.get("compiler_version", COLUMNAR_COMPILER_VERSION))
            ),
        )
        return program

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_portable_dict())


@dataclass(frozen=True, slots=True)
class ColumnarCompilationDecision:
    """A deterministic supported program or a whole-dataset fallback."""

    dataset_id: str
    dataset_name: str
    dataset_kind: ColumnarDatasetKind
    support: ColumnarSupport
    capability_uses: tuple[ColumnarCapabilityUse, ...]
    fallback_reasons: tuple[ColumnarFallbackReason, ...]
    program: ColumnarTransformationProgram | None

    def __post_init__(self) -> None:
        supported = self.support is ColumnarSupport.SUPPORTED
        if supported != (self.program is not None):
            raise ValueError("Only a supported decision can contain a program")
        if supported == bool(self.fallback_reasons):
            raise ValueError("A fallback decision requires explicit reasons")

    def to_portable_dict(self) -> dict[str, object]:
        return {
            "capability_uses": [
                item.to_portable_dict() for item in self.capability_uses
            ],
            "dataset_id": self.dataset_id,
            "dataset_kind": self.dataset_kind.value,
            "dataset_name": self.dataset_name,
            "fallback_reasons": [
                item.to_portable_dict() for item in self.fallback_reasons
            ],
            "program": self.program.to_portable_dict() if self.program else None,
            "program_hash": self.program.content_hash if self.program else None,
            "support": self.support.value,
        }

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_portable_dict())


@dataclass(slots=True)
class _CompilationDraft:
    uses: list[ColumnarCapabilityUse]
    required_keys: set[str]

    def use(
        self,
        operation: ColumnarOperationKind,
        path: str,
        *,
        target_field: str | None = None,
    ) -> None:
        self.uses.append(
            ColumnarCapabilityUse(
                path=path,
                operation=operation,
                target_field=target_field,
            )
        )


def compile_columnar_transformation_programs(
    definition: MappingDefinition,
    selection: SourceSelection,
    *,
    dataset_kinds: Mapping[str, ColumnarDatasetKind | str] | None = None,
) -> tuple[ColumnarCompilationDecision, ...]:
    """Compile every mapped dataset without inspecting any source row value.

    Omitted ``dataset_kinds`` mean direct datasets, which is the bounded scope
    of Slices 3–6.  Callers that already know a dataset is related, derived, or
    structural must pass that role and receive an explicit oracle fallback.
    """

    if definition.source_selection_hash != selection.content_hash:
        raise ColumnarCompilationError(
            "The mapping no longer matches the frozen source selection"
        )
    datasets = _unique_by_id(selection.datasets, "frozen source")
    mappings = _unique_by_id(definition.datasets, "mapping")
    if set(datasets) != set(mappings):
        raise ColumnarCompilationError(
            "The mapping must cover every frozen dataset exactly once"
        )
    raw_kinds = dataset_kinds or {}
    unknown_kind_ids = set(raw_kinds).difference(datasets)
    if unknown_kind_ids:
        raise ColumnarCompilationError("A dataset kind names an unknown dataset")
    return tuple(
        _compile_dataset(
            definition,
            datasets[dataset_id],
            mappings[dataset_id],
            ColumnarDatasetKind(raw_kinds.get(dataset_id, ColumnarDatasetKind.DIRECT)),
            datasets,
        )
        for dataset_id in sorted(datasets)
    )


def compile_columnar_transformation_program(
    definition: MappingDefinition,
    selection: SourceSelection,
    dataset_id: str,
    *,
    dataset_kind: ColumnarDatasetKind | str = ColumnarDatasetKind.DIRECT,
) -> ColumnarCompilationDecision:
    """Compile one dataset using the same deterministic complete-map checks."""

    decisions = compile_columnar_transformation_programs(
        definition,
        selection,
        dataset_kinds={dataset_id: dataset_kind},
    )
    for decision in decisions:
        if decision.dataset_id == dataset_id:
            return decision
    raise ColumnarCompilationError("The requested dataset is not mapped")


def _unique_by_id(items: tuple[object, ...], label: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for item in items:
        dataset_id = str(getattr(item, "dataset_id"))
        if dataset_id in result:
            raise ColumnarCompilationError(f"The {label} contains duplicate datasets")
        result[dataset_id] = item
    return result


def _compile_dataset(
    definition: MappingDefinition,
    dataset: object,
    mapping: object,
    dataset_kind: ColumnarDatasetKind,
    datasets: Mapping[str, object],
) -> ColumnarCompilationDecision:
    source = cast(SourceDataset, dataset)
    authored = cast(DatasetMapping, mapping)
    columns = {item.stable_key: item for item in source.columns}
    draft = _CompilationDraft(uses=[], required_keys=set())
    if dataset_kind is not ColumnarDatasetKind.DIRECT:
        draft.use(ColumnarOperationKind.NON_DIRECT_DATASET, "/dataset_kind")

    source_identity = tuple(
        _source_identity_component(key, columns, draft, index)
        for index, key in enumerate(authored.source_identity_column_keys)
    )
    target_identity = tuple(
        _identity_component(component, "target_identity", columns, draft, index)
        for index, component in enumerate(authored.target_identity)
    )
    target_scope = tuple(
        _identity_component(component, "target_scope", columns, draft, index)
        for index, component in enumerate(authored.target_scope)
    )

    scalar_fields: list[ColumnarScalarFieldProgram] = []
    for disposition in authored.target_field_dispositions:
        draft.use(
            (
                ColumnarOperationKind.OMIT_ODOO_DEFAULT
                if disposition.handling is TargetFieldHandling.ODOO_DEFAULT
                else ColumnarOperationKind.OMIT_ODOO_MANAGED
            ),
            f"/target_field_dispositions/{disposition.target_field}",
            target_field=disposition.target_field,
        )
    for output_ordinal, field in enumerate(
        sorted(authored.fields, key=lambda item: item.target_field)
    ):
        path = f"/fields/{field.target_field}"
        if field.value_source is ScalarValueSource.ODOO_DEFAULT:
            draft.use(
                ColumnarOperationKind.OMIT_ODOO_DEFAULT,
                f"{path}/provider",
                target_field=field.target_field,
            )
            continue
        scalar_fields.append(
            _scalar_field_program(
                field,
                output_ordinal,
                path,
                columns,
                draft,
            )
        )

    relationships: list[ColumnarRelationshipProgram] = []
    for index, relationship in enumerate(
        sorted(authored.relationships, key=lambda item: item.target_field)
    ):
        path = f"/relationships/{index}"
        parent_id = relationship.resolver.dataset_id
        if (
            relationship.kind == "many2one"
            and relationship.resolver.origin is ResolverOrigin.DATASET
            and parent_id is not None
        ):
            parent_dataset = next(
                (
                    item
                    for item in definition.datasets
                    if item.dataset_id == parent_id
                ),
                None,
            )
            parent_source = datasets.get(parent_id)
            if parent_dataset is not None and parent_source is not None:
                key_columns = tuple(
                    _require_column(key, columns, draft)
                    for key in relationship.source_column_keys
                )
                relationships.append(
                    ColumnarRelationshipProgram(
                        target_field=relationship.target_field,
                        parent_dataset_id=parent_id,
                        parent_dataset_name=str(
                            getattr(parent_source, "name")
                        ),
                        key=ColumnarIdentityComponentProgram(
                            role="relationship",
                            source_columns=key_columns,
                            source_label=" + ".join(
                                item.source_name for item in key_columns
                            ),
                            target_fields=(relationship.target_field,),
                            value_type="string",
                            normalization_steps=(
                                ColumnarExpressionStep(
                                    ColumnarOperationKind.TRIM
                                ),
                                ColumnarExpressionStep(
                                    ColumnarOperationKind.EMPTY_AS_NULL
                                ),
                                ColumnarExpressionStep(
                                    ColumnarOperationKind.PARSE_STRING
                                ),
                            ),
                            required=relationship.required,
                            failure_code="SOURCE_REQUIRED_VALUE_MISSING",
                        ),
                        compare=relationship.compare,
                        validate_only=relationship.validate_only,
                        required=relationship.required,
                        required_on_create=relationship.required_on_create,
                        on_missing=relationship.on_missing,
                        on_ambiguous=relationship.on_ambiguous,
                        operation=relationship.operation,
                        null_policy=relationship.null_policy,
                    )
                )
                draft.use(
                    ColumnarOperationKind.RELATIONSHIP_KEY_NORMALIZATION,
                    f"{path}/key",
                    target_field=relationship.target_field,
                )
                draft.use(
                    ColumnarOperationKind.RELATIONSHIP_RESOLUTION,
                    f"{path}/resolution",
                    target_field=relationship.target_field,
                )
                continue
        for key in relationship.source_column_keys:
            _require_column(key, columns, draft)
        draft.use(
            ColumnarOperationKind.RELATIONSHIP_POLICY,
            path,
            target_field=relationship.target_field,
        )

    draft.use(ColumnarOperationKind.DIRECT_LINEAGE, "/lineage")
    draft.use(ColumnarOperationKind.TRANSFORMATION_IMPACT, "/impacts")
    draft.use(
        ColumnarOperationKind.DUPLICATE_IDENTITY_GROUPING,
        "/set/duplicate_identity",
    )
    set_requirements = [
        ColumnarSetRequirement(ColumnarOperationKind.DUPLICATE_IDENTITY_GROUPING)
    ]
    set_requirements.extend(
        ColumnarSetRequirement(
            ColumnarOperationKind.RELATIONSHIP_RESOLUTION,
            target_field=item.target_field,
            name=item.parent_dataset_name,
        )
        for item in relationships
    )
    for control in sorted(
        authored.effective_control_totals,
        key=lambda item: (item.target_field, item.name.casefold()),
    ):
        draft.use(
            ColumnarOperationKind.CONTROL_TOTAL,
            f"/set/control_totals/{control.target_field}/{control.name}",
            target_field=control.target_field,
        )
        set_requirements.append(
            ColumnarSetRequirement(
                operation=ColumnarOperationKind.CONTROL_TOTAL,
                target_field=control.target_field,
                name=control.name,
            )
        )

    uses = tuple(
        sorted(
            draft.uses,
            key=lambda item: (
                item.path,
                item.operation.value,
                item.target_field or "",
            ),
        )
    )
    reasons = tuple(
        sorted(
            (
                _fallback_reason(item)
                for item in uses
                if item.execution_class is ColumnarExecutionClass.PYTHON_ORACLE
            ),
            key=lambda item: (
                item.path,
                item.code,
                item.operation.value,
                item.target_field or "",
            ),
        )
    )
    if reasons:
        return ColumnarCompilationDecision(
            dataset_id=source.dataset_id,
            dataset_name=source.name,
            dataset_kind=dataset_kind,
            support=ColumnarSupport.PYTHON_FALLBACK,
            capability_uses=uses,
            fallback_reasons=reasons,
            program=None,
        )

    ordered_inputs = tuple(
        _input_column(columns[key])
        for key in sorted(draft.required_keys, key=lambda item: columns[item].ordinal)
    )
    program = ColumnarTransformationProgram(
        dataset_id=source.dataset_id,
        dataset_name=source.name,
        target_model=authored.target_model,
        target_mode=authored.mode.value,
        mapping_content_hash=definition.content_hash,
        source_selection_hash=definition.source_selection_hash,
        schema_hash=definition.schema_hash,
        inputs=ordered_inputs,
        source_identity=source_identity,
        target_identity=target_identity,
        target_scope=target_scope,
        scalar_fields=tuple(scalar_fields),
        relationships=tuple(relationships),
        set_requirements=tuple(set_requirements),
    )
    return ColumnarCompilationDecision(
        dataset_id=source.dataset_id,
        dataset_name=source.name,
        dataset_kind=dataset_kind,
        support=ColumnarSupport.SUPPORTED,
        capability_uses=uses,
        fallback_reasons=(),
        program=program,
    )


def _source_identity_component(
    key: str,
    columns: Mapping[str, object],
    draft: _CompilationDraft,
    index: int,
) -> ColumnarIdentityComponentProgram:
    source = _require_column(key, columns, draft)
    path = f"/source_identity/{index}"
    draft.use(ColumnarOperationKind.SOURCE_IDENTITY_NORMALIZATION, path)
    return ColumnarIdentityComponentProgram(
        role="source_identity",
        source_columns=(source,),
        source_label=source.source_name,
        target_fields=(),
        value_type="string",
        normalization_steps=(
            ColumnarExpressionStep(ColumnarOperationKind.TRIM),
            ColumnarExpressionStep(ColumnarOperationKind.EMPTY_AS_NULL),
        ),
    )


def _identity_component(
    component: IdentityComponentMapping,
    role: str,
    columns: Mapping[str, object],
    draft: _CompilationDraft,
    index: int,
) -> ColumnarIdentityComponentProgram:
    path = f"/{role}/{index}"
    inputs = tuple(
        _require_column(key, columns, draft)
        for key in component.source_column_keys
    )
    draft.use(ColumnarOperationKind.TARGET_IDENTITY_NORMALIZATION, path)
    if component.resolver is not None:
        draft.use(
            ColumnarOperationKind.IDENTITY_RESOLVER,
            f"{path}/resolver",
            target_field=(component.target_fields[0] if component.target_fields else None),
        )
        conversion_steps: tuple[ColumnarExpressionStep, ...] = ()
    else:
        conversion = _conversion_operation(
            component.value_type,
            identity_semantics=True,
        )
        draft.use(
            conversion,
            f"{path}/parse",
            target_field=(
                component.target_fields[0] if component.target_fields else None
            ),
        )
        conversion_steps = (ColumnarExpressionStep(conversion),)
    return ColumnarIdentityComponentProgram(
        role=role,
        source_columns=inputs,
        source_label=" + ".join(item.source_name for item in inputs),
        target_fields=component.target_fields,
        value_type=component.value_type,
        normalization_steps=(
            ColumnarExpressionStep(ColumnarOperationKind.TRIM),
            ColumnarExpressionStep(ColumnarOperationKind.COLLAPSE_WHITESPACE),
            ColumnarExpressionStep(ColumnarOperationKind.EMPTY_AS_NULL),
            *conversion_steps,
        ),
    )


def _scalar_field_program(
    field: ScalarFieldMapping,
    output_ordinal: int,
    path: str,
    columns: Mapping[str, object],
    draft: _CompilationDraft,
) -> ColumnarScalarFieldProgram:
    source = (
        _require_column(field.source_column_key, columns, draft)
        if field.source_column_key is not None
        else None
    )
    provider_operation = {
        ScalarValueSource.SOURCE: ColumnarOperationKind.READ_SOURCE,
        ScalarValueSource.CONSTANT: ColumnarOperationKind.USE_CONSTANT,
        ScalarValueSource.SOURCE_WITH_FALLBACK: ColumnarOperationKind.SOURCE_FALLBACK,
        ScalarValueSource.CONDITIONAL_RULES: ColumnarOperationKind.CONDITIONAL_SELECTION,
    }[field.value_source]
    draft.use(provider_operation, f"{path}/provider", target_field=field.target_field)
    fallback_probe = (
        _fallback_probe_steps(field, path, draft)
        if field.value_source is ScalarValueSource.SOURCE_WITH_FALLBACK
        else ()
    )
    if field.value_mappings:
        draft.use(
            ColumnarOperationKind.INLINE_VALUE_MAPPING,
            f"{path}/value_mappings",
            target_field=field.target_field,
        )
    if field.reference_lookup is not None:
        for key in field.reference_lookup.key_source_column_keys:
            _require_column(key, columns, draft)
        draft.use(
            ColumnarOperationKind.REFERENCE_LOOKUP,
            f"{path}/reference_lookup",
            target_field=field.target_field,
        )
    selection_rule_programs = tuple(
        ColumnarSelectionRuleProgram(
            conditions=tuple(
                ColumnarSelectionConditionProgram(
                    source=_require_column(
                        condition.source_column_key,
                        columns,
                        draft,
                    ),
                    operator=condition.operator.value,
                    comparison_value=condition.comparison_value,
                    value_type=condition.value_type,
                )
                for condition in rule.conditions
            ),
            target_value=rule.target_value,
            join=rule.join.value,
        )
        for rule in (
            field.selection_rules.rules
            if field.selection_rules is not None
            else ()
        )
    )
    provider = ColumnarValueProviderProgram(
        operation=provider_operation,
        source=source,
        literal_value=field.literal_value,
        value_mappings=tuple(
            (item.source_value, item.target_value)
            for item in field.value_mappings
        ),
        fallback_probe_steps=fallback_probe,
        selection_rules=selection_rule_programs,
        selection_otherwise_value=(
            field.selection_rules.otherwise_value
            if field.selection_rules is not None
            else None
        ),
    )
    transforms = _transform_steps(field, path, draft)
    required_step = None
    if field.required:
        required_step = ColumnarExpressionStep(
            ColumnarOperationKind.REQUIRE_VALUE,
            error_code="SOURCE_REQUIRED_VALUE_MISSING",
        )
        draft.use(
            ColumnarOperationKind.REQUIRE_VALUE,
            f"{path}/required",
            target_field=field.target_field,
        )
    conversion = _conversion_operation(
        field.value_type,
        date_format=field.transform.date_format,
    )
    conversion_step = ColumnarExpressionStep(
        conversion,
        text=(
            field.transform.date_format
            if field.value_type in {"date", "datetime"}
            else (
                field.transform.decimal_locale
                if field.value_type == "decimal"
                else None
            )
        ),
        error_code="SOURCE_TYPE_INVALID",
    )
    draft.use(
        conversion,
        f"{path}/parse",
        target_field=field.target_field,
    )
    post_conversion: tuple[ColumnarExpressionStep, ...] = ()
    if field.value_type == "decimal" and field.transform.decimal_places is not None:
        post_conversion = (
            ColumnarExpressionStep(
                ColumnarOperationKind.ROUND_DECIMAL,
                integer=field.transform.decimal_places,
                text=field.transform.rounding_mode,
                error_code="SOURCE_DECIMAL_ROUNDING_INVALID",
            ),
        )
        draft.use(
            ColumnarOperationKind.ROUND_DECIMAL,
            f"{path}/transform/round_decimal",
            target_field=field.target_field,
        )
    validations = _validation_steps(field, path, draft)
    draft.use(
        ColumnarOperationKind.TRANSFORMATION_IMPACT,
        f"{path}/impact",
        target_field=field.target_field,
    )
    return ColumnarScalarFieldProgram(
        target_field=field.target_field,
        output_ordinal=output_ordinal,
        value_type=field.value_type,
        source_label=(
            source.source_name
            if source is not None
            else (
                " + ".join(
                    dict.fromkeys(
                        condition.source.source_name
                        for rule in selection_rule_programs
                        for condition in rule.conditions
                    )
                )
                if selection_rule_programs
                else "Constant value"
            )
        ),
        transformation_rules=transformation_rule_summary(field),
        provider=provider,
        transform_steps=transforms,
        required_step=required_step,
        conversion_step=conversion_step,
        post_conversion_steps=post_conversion,
        validation_steps=validations,
        required=field.required,
        required_on_create=field.required_on_create,
        compare=field.compare,
        validate_only=field.validate_only,
        null_policy=field.null_policy,
        impact_required=True,
        failures=ColumnarFailureSemantics(),
    )


def _fallback_probe_steps(
    field: ScalarFieldMapping,
    path: str,
    draft: _CompilationDraft,
) -> tuple[ColumnarExpressionStep, ...]:
    policy = field.transform
    result: list[ColumnarExpressionStep] = []
    for enabled, operation in (
        (policy.trim, ColumnarOperationKind.TRIM),
        (policy.collapse_whitespace, ColumnarOperationKind.COLLAPSE_WHITESPACE),
    ):
        if enabled:
            result.append(ColumnarExpressionStep(operation))
            draft.use(operation, f"{path}/fallback_probe/{operation.value}")
    case_operation = _case_operation(policy.case_mode)
    if case_operation is not None:
        result.append(ColumnarExpressionStep(case_operation))
        draft.use(case_operation, f"{path}/fallback_probe/{case_operation.value}")
    if policy.empty_as_null:
        result.append(ColumnarExpressionStep(ColumnarOperationKind.EMPTY_AS_NULL))
        draft.use(
            ColumnarOperationKind.EMPTY_AS_NULL,
            f"{path}/fallback_probe/empty_as_null",
        )
    return tuple(result)


def _transform_steps(
    field: ScalarFieldMapping,
    path: str,
    draft: _CompilationDraft,
) -> tuple[ColumnarExpressionStep, ...]:
    policy = field.transform
    result: list[ColumnarExpressionStep] = []
    if policy.formula.strip():
        result.append(
            ColumnarExpressionStep(
                ColumnarOperationKind.FORMULA,
                text=policy.formula,
                error_code="SOURCE_FORMULA_INVALID",
            )
        )
        draft.use(
            ColumnarOperationKind.FORMULA,
            f"{path}/transform/formula",
            target_field=field.target_field,
        )
    result.append(ColumnarExpressionStep(ColumnarOperationKind.RENDER_TEXT))
    draft.use(
        ColumnarOperationKind.RENDER_TEXT,
        f"{path}/transform/render_text",
        target_field=field.target_field,
    )
    for enabled, operation in (
        (policy.trim, ColumnarOperationKind.TRIM),
        (policy.collapse_whitespace, ColumnarOperationKind.COLLAPSE_WHITESPACE),
    ):
        if enabled:
            result.append(ColumnarExpressionStep(operation))
            draft.use(
                operation,
                f"{path}/transform/{operation.value}",
                target_field=field.target_field,
            )
    for step_index, step in enumerate(policy.text_steps):
        if not step.configured:
            continue
        operation = (
            ColumnarOperationKind.REMOVE_SEPARATORS_BETWEEN_DIGITS
            if step.kind == "remove_separators_between_digits"
            else {
                "literal": ColumnarOperationKind.REPLACE_LITERAL,
                "starts_with": ColumnarOperationKind.REPLACE_PREFIX,
                "ends_with": ColumnarOperationKind.REPLACE_SUFFIX,
                "pattern": ColumnarOperationKind.REPLACE_PATTERN,
            }[step.search_mode]
        )
        result.append(
            ColumnarExpressionStep(
                operation,
                text=(
                    step.characters
                    if step.kind == "remove_separators_between_digits"
                    else step.search_value
                ),
                replacement=step.replacement_value,
                integer=step_index,
                flag=step.replace_all,
                error_code=(
                    "SOURCE_REPLACEMENT_INVALID"
                    if operation
                    in {
                        ColumnarOperationKind.REPLACE_PATTERN,
                        ColumnarOperationKind.REMOVE_SEPARATORS_BETWEEN_DIGITS,
                    }
                    else None
                ),
            )
        )
        draft.use(
            operation,
            f"{path}/transform/text_steps/{step_index}",
            target_field=field.target_field,
        )
    case_operation = _case_operation(policy.case_mode)
    if case_operation is not None:
        result.append(ColumnarExpressionStep(case_operation))
        draft.use(
            case_operation,
            f"{path}/transform/{case_operation.value}",
            target_field=field.target_field,
        )
    if policy.configured_text_steps or policy.formula:
        result.append(
            ColumnarExpressionStep(
                ColumnarOperationKind.VALIDATE_RULE_OUTPUT_LENGTH,
                integer=1_000_000,
                error_code="SOURCE_RULE_OUTPUT_TOO_LONG",
            )
        )
        draft.use(
            ColumnarOperationKind.VALIDATE_RULE_OUTPUT_LENGTH,
            f"{path}/transform/output_length",
            target_field=field.target_field,
        )
    if policy.empty_as_null:
        result.append(ColumnarExpressionStep(ColumnarOperationKind.EMPTY_AS_NULL))
        draft.use(
            ColumnarOperationKind.EMPTY_AS_NULL,
            f"{path}/transform/empty_as_null",
            target_field=field.target_field,
        )
    return tuple(result)


def _validation_steps(
    field: ScalarFieldMapping,
    path: str,
    draft: _CompilationDraft,
) -> tuple[ColumnarExpressionStep, ...]:
    policy = field.validation
    result: list[ColumnarExpressionStep] = []
    if policy.configured and field.value_type != "string":
        draft.use(
            ColumnarOperationKind.VALIDATE_TYPED_VALUE,
            f"{path}/validation/typed_value",
            target_field=field.target_field,
        )
    if policy.exact_length is not None:
        result.append(
            ColumnarExpressionStep(
                ColumnarOperationKind.VALIDATE_EXACT_LENGTH,
                integer=policy.exact_length,
                error_code="SOURCE_TEXT_LENGTH_INVALID",
            )
        )
        draft.use(
            ColumnarOperationKind.VALIDATE_EXACT_LENGTH,
            f"{path}/validation/exact_length",
            target_field=field.target_field,
        )
    if policy.character_class != "none":
        result.append(
            ColumnarExpressionStep(
                ColumnarOperationKind.VALIDATE_CHARACTER_CLASS,
                character_class=policy.character_class,
                segment_location=policy.segment_location,
                segment_length=policy.segment_length,
                error_code="SOURCE_TEXT_SEGMENT_INVALID",
            )
        )
        draft.use(
            ColumnarOperationKind.VALIDATE_CHARACTER_CLASS,
            f"{path}/validation/character_class",
            target_field=field.target_field,
        )
    if policy.pattern:
        result.append(
            ColumnarExpressionStep(
                ColumnarOperationKind.VALIDATE_PATTERN,
                text=policy.pattern,
                error_code="SOURCE_PATTERN_MISMATCH",
            )
        )
        draft.use(
            ColumnarOperationKind.VALIDATE_PATTERN,
            f"{path}/validation/pattern",
            target_field=field.target_field,
        )
    return tuple(result)


def _case_operation(case_mode: str) -> ColumnarOperationKind | None:
    return {
        "preserve": None,
        "uppercase": ColumnarOperationKind.CASE_UPPER,
        "lowercase": ColumnarOperationKind.CASE_LOWER,
        "sentence": ColumnarOperationKind.CASE_SENTENCE,
        "title": ColumnarOperationKind.CASE_TITLE,
    }.get(case_mode, ColumnarOperationKind.CASE_SENTENCE)


def _conversion_operation(
    value_type: str,
    *,
    date_format: str = "iso",
    identity_semantics: bool = False,
) -> ColumnarOperationKind:
    try:
        return {
            "string": ColumnarOperationKind.PARSE_STRING,
            "integer": ColumnarOperationKind.PARSE_INTEGER,
            "decimal": ColumnarOperationKind.PARSE_DECIMAL,
            "boolean": ColumnarOperationKind.PARSE_BOOLEAN,
            "date": (
                ColumnarOperationKind.PARSE_ISO_DATE
                if identity_semantics
                else ColumnarOperationKind.PARSE_DATE
            ),
            "datetime": (
                ColumnarOperationKind.PARSE_ISO_DATETIME
                if date_format == "iso"
                else ColumnarOperationKind.PARSE_DATETIME
            ),
        }[value_type]
    except KeyError as error:
        raise ColumnarCompilationError(
            f"Unsupported scalar value type {value_type!r}"
        ) from error


def _require_column(
    key: str | None,
    columns: Mapping[str, object],
    draft: _CompilationDraft,
) -> ColumnarInputColumn:
    if key is None or key not in columns:
        raise ColumnarCompilationError(
            f"Mapped source column {key!r} is not frozen"
        )
    draft.required_keys.add(key)
    return _input_column(columns[key])


def _input_column(column: object) -> ColumnarInputColumn:
    return ColumnarInputColumn(
        stable_key=str(getattr(column, "stable_key")),
        ordinal=int(getattr(column, "ordinal")),
        source_name=str(getattr(column, "source_name")),
        candidate_type=str(getattr(column, "candidate_type")),
    )


def _fallback_reason(use: ColumnarCapabilityUse) -> ColumnarFallbackReason:
    capability = _CAPABILITY_BY_OPERATION[use.operation]
    assert capability.fallback_code is not None
    assert capability.fallback_message is not None
    return ColumnarFallbackReason(
        code=capability.fallback_code,
        message=capability.fallback_message,
        path=use.path,
        operation=use.operation,
        target_field=use.target_field,
    )


__all__ = [
    "COLUMNAR_CAPABILITY_MATRIX",
    "COLUMNAR_COMPILER_VERSION",
    "COLUMNAR_PROGRAM_CONTRACT_VERSION",
    "ColumnarCapabilityDefinition",
    "ColumnarCapabilityUse",
    "ColumnarCompilationDecision",
    "ColumnarCompilationError",
    "ColumnarDatasetKind",
    "ColumnarExecutionClass",
    "ColumnarExpressionStep",
    "ColumnarFallbackReason",
    "ColumnarIdentityComponentProgram",
    "ColumnarInputColumn",
    "ColumnarOperationKind",
    "ColumnarScalarFieldProgram",
    "ColumnarRelationshipProgram",
    "ColumnarSetRequirement",
    "ColumnarSupport",
    "ColumnarTransformationProgram",
    "ColumnarValueProviderProgram",
    "compile_columnar_transformation_program",
    "compile_columnar_transformation_programs",
]
