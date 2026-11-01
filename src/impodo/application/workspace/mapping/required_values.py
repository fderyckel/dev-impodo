"""Project required-value decisions from bulk saved evidence, without row reads.

This advisory view reuses create-field policy and validation. It neither runs
validation nor authorizes a default, mapping submission, or Odoo write. Server
composition supplies the same metadata from verified worker projections.
"""

from dataclasses import dataclass
from enum import StrEnum

from impodo.domain.mapping.artifacts import MappingRevision
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    MappingTargetMode,
    RelationshipValueSource,
    ScalarValueSource,
    TargetFieldHandling,
)
from impodo.domain.mapping.create_field_policy import (
    CreateFieldCoverage,
    evaluate_create_field,
    required_create_hook_inputs,
)
from impodo.domain.mapping.validation.evidence import MappingValidationResult
from impodo.domain.matching_order import MatchingOrderCheck
from impodo.domain.odoo.compatibility import OdooOperation, assess_odoo_operation
from impodo.domain.schema.governance import SchemaGovernance
from impodo.domain.workspace.contracts import (
    MappingWorkingDraft,
    OdooSchemaCatalog,
    SourceSelection,
)
from impodo.domain.workspace.reference_keys import reference_policy_hash


class ValueCoverageState(StrEnum):
    """Separate saved choices from current proof and unresolved decisions."""

    CHOSEN = "chosen"
    CHECKED = "checked"
    MISSING = "missing"
    STALE = "stale"
    ATTENTION = "attention"
    NOT_APPLICABLE = "not_applicable"


class RequiredValueProvider(StrEnum):
    """Describe a provider without exposing fixed values or protected Odoo IDs."""

    SOURCE = "source"
    FIXED = "fixed"
    DEFAULT = "default"
    MANAGED = "managed"
    MISSING = "missing"


class FieldDecisionLocation(StrEnum):
    """Keep correction links on a rendered owner when a field is unavailable."""

    FIELD = "field"
    REQUIREMENTS = "requirements"
    READONLY_RECOVERY = "readonly_recovery"


@dataclass(frozen=True, slots=True)
class RequiredValue:
    """One required field and the existing decision that can repair it."""

    dataset_id: str
    target_field: str
    label: str
    relationship: bool
    identity: bool
    provider: RequiredValueProvider
    state: ValueCoverageState
    reason: str
    review_default: bool = False
    default_verified: bool = False
    correction: FieldDecisionLocation = FieldDecisionLocation.FIELD


@dataclass(frozen=True, slots=True)
class TableValueCoverage:
    """Cross-table progress keeps identity and parent blockers visible."""

    dataset_id: str
    dataset_name: str
    target_model: str
    model_label: str
    identity_state: ValueCoverageState
    parent_state: ValueCoverageState
    check_state: ValueCoverageState
    create_requirements: bool
    values: tuple[RequiredValue, ...]

    def count(self, state: ValueCoverageState) -> int:
        """Count field decisions, never rows or forecast creates."""
        return sum(item.state is state for item in self.values)


@dataclass(frozen=True, slots=True)
class RequiredValuesProjection:
    """A value-free advisory projection over one loaded input revision."""

    tables: tuple[TableValueCoverage, ...]
    inputs_current: bool
    validation_current: bool

    @property
    def field_count(self) -> int:
        return sum(len(table.values) for table in self.tables)

    @property
    def attention_count(self) -> int:
        return sum(
            table.count(ValueCoverageState.MISSING)
            + table.count(ValueCoverageState.ATTENTION)
            for table in self.tables
        )

    @property
    def stale_count(self) -> int:
        return sum(table.count(ValueCoverageState.STALE) for table in self.tables)


def project_required_values(
    selection: SourceSelection,
    schema: OdooSchemaCatalog,
    governance: SchemaGovernance | None,
    revision: MappingRevision | None,
    working_draft: MappingWorkingDraft | None,
    validation: MappingValidationResult | None,
    *,
    live_check: MatchingOrderCheck | None = None,
    live_check_current: bool = False,
) -> RequiredValuesProjection:
    """Read saved meaning once and project all tables without per-field queries."""
    definition = (
        working_draft.definition
        if working_draft
        else revision.definition
        if revision
        else None
    )
    schema_hash = governance.content_hash if governance else schema.content_hash
    inputs_current = bool(
        definition is not None
        and definition.source_selection_hash == selection.content_hash
        and definition.schema_hash == schema_hash
        and (governance is None or governance.catalog_hash == schema.content_hash)
    )
    version = assess_odoo_operation(schema.odoo_version, OdooOperation.COMPARE)
    expected_policy = (
        reference_policy_hash(version.version.major)
        if version.allowed and version.version.major is not None
        else None
    )
    validation_current = bool(
        inputs_current
        and definition is not None
        and validation is not None
        and validation.mapping_content_hash == definition.content_hash
        and validation.source_selection_hash == selection.content_hash
        and validation.schema_hash == schema_hash
        and expected_policy is not None
        and validation.reference_policy_hash == expected_policy
    )
    mappings = (
        {item.dataset_id: item for item in definition.datasets} if definition else {}
    )
    models = {item.name: item for item in schema.models}
    issues = {}
    dataset_errors = set()
    for issue in validation.issues if validation_current and validation else ():
        if issue.severity == "error":
            dataset_errors.add(issue.dataset_id)
            if issue.target_field:
                issues.setdefault((issue.dataset_id, issue.target_field), []).append(
                    issue
                )
    identity_results = (
        {item.dataset_id: item for item in live_check.identity_results}
        if live_check and live_check_current
        else {}
    )
    parent_results = (
        {
            (item.owner_dataset_id, item.target_field): item
            for item in live_check.relationship_health_results
        }
        if live_check and live_check_current
        else {}
    )
    tables = []
    for source in selection.datasets:
        mapping = mappings.get(source.dataset_id)
        model = models.get(mapping.target_model) if mapping else None
        fields = {item.name: item for item in model.fields} if model else {}
        creates = mapping is None or mapping.mode not in {
            MappingTargetMode.REFERENCE,
            MappingTargetMode.ODOO_PINNED_UPDATE,
        }
        identity_targets = (
            {
                field: component
                for component in (*mapping.target_identity, *mapping.target_scope)
                for field in component.target_fields
            }
            if mapping
            else {}
        )
        scalars = (
            {item.target_field: item for item in mapping.fields} if mapping else {}
        )
        relationships = (
            {item.target_field: item for item in mapping.relationships}
            if mapping
            else {}
        )
        dispositions = (
            {
                item.target_field: item.handling
                for item in mapping.target_field_dispositions
            }
            if mapping
            else {}
        )
        providers = {**identity_targets, **scalars, **relationships}
        provided = {
            name
            for name, rule in providers.items()
            if not getattr(rule, "validate_only", False)
            and getattr(rule, "value_source", None)
            is not ScalarValueSource.ODOO_DEFAULT
        }
        required = {
            name for name, field in fields.items() if creates and field.required
        }
        if mapping and creates:
            required.update(required_create_hook_inputs(mapping.target_model, provided))
        required.update(
            name
            for name, rule in {**scalars, **relationships}.items()
            if rule.required or creates and rule.required_on_create
        )
        required.update(
            name for name in (*providers, *dispositions) if name not in fields
        )
        values = []
        for name in sorted(required):
            field = fields.get(name)
            scalar, relationship = scalars.get(name), relationships.get(name)
            handling = dispositions.get(name)
            if scalar and scalar.value_source is ScalarValueSource.ODOO_DEFAULT:
                handling = TargetFieldHandling.ODOO_DEFAULT
            provider = RequiredValueProvider.MISSING
            state = ValueCoverageState.MISSING
            reason = "Choose a source value, fixed value, or supported Odoo decision."
            if name in provided:
                provider = (
                    RequiredValueProvider.FIXED
                    if (
                        scalar
                        and scalar.value_source is ScalarValueSource.CONSTANT
                        or relationship
                        and relationship.value_source
                        is RelationshipValueSource.CONSTANT_EXISTING
                    )
                    else RequiredValueProvider.SOURCE
                )
            if field is not None:
                assessment = evaluate_create_field(
                    field,
                    provided=name in provided,
                    handling=handling,
                    target_model=mapping.target_model if mapping else None,
                    odoo_version=schema.odoo_version,
                ).coverage
                if (
                    assessment is CreateFieldCoverage.OPTIONAL
                    and handling is TargetFieldHandling.ODOO_DEFAULT
                ):
                    assessment = (
                        CreateFieldCoverage.DEFAULT_CONFIRMED
                        if field.create_default_present
                        else CreateFieldCoverage.DEFAULT_UNVERIFIED
                    )
                if assessment is CreateFieldCoverage.PROVIDED:
                    state, reason = (
                        ValueCoverageState.CHOSEN,
                        "Saved provider; complete row checks run during preparation.",
                    )
                elif assessment in {
                    CreateFieldCoverage.DEFAULT_AVAILABLE,
                    CreateFieldCoverage.DEFAULT_CONFIRMED,
                    CreateFieldCoverage.DEFAULT_UNVERIFIED,
                }:
                    provider = RequiredValueProvider.DEFAULT
                    if assessment is CreateFieldCoverage.DEFAULT_CONFIRMED:
                        state, reason = (
                            ValueCoverageState.CHOSEN,
                            "Confirmed omission uses the captured Odoo default.",
                        )
                    elif assessment is CreateFieldCoverage.DEFAULT_AVAILABLE:
                        reason = "The captured default still needs confirmation."
                    else:
                        state, reason = (
                            ValueCoverageState.ATTENTION,
                            "The saved Odoo-default decision has no verified default in the captured context.",
                        )
                elif assessment in {
                    CreateFieldCoverage.READONLY_IGNORED,
                    CreateFieldCoverage.ODOO_MANAGED_CONFIRMED,
                }:
                    provider, state, reason = (
                        RequiredValueProvider.MANAGED,
                        ValueCoverageState.CHOSEN,
                        "Captured or qualified Odoo behavior supplies this field.",
                    )
                elif assessment in {
                    CreateFieldCoverage.READONLY_CONFLICT,
                    CreateFieldCoverage.ODOO_MANAGED_INVALID,
                }:
                    state, reason = (
                        ValueCoverageState.ATTENTION,
                        "Review this provider against the captured Odoo field.",
                    )
            else:
                state, reason = (
                    ValueCoverageState.ATTENTION,
                    "This field is unavailable in the captured Odoo data.",
                )
            field_issues = issues.get((source.dataset_id, name), ())
            review_default = any(
                item.code == "MAPPING_ODOO_DEFAULT_AVAILABLE" for item in field_issues
            )
            blocking = tuple(
                item
                for item in field_issues
                if item.code != "MAPPING_ODOO_DEFAULT_AVAILABLE"
            )
            if blocking:
                state = (
                    ValueCoverageState.MISSING
                    if blocking[0].code
                    in {
                        "MAPPING_REQUIRED_FIELD_UNMAPPED",
                        "MAPPING_CREATE_HOOK_INPUT_MISSING",
                    }
                    else ValueCoverageState.ATTENTION
                )
                reason = (
                    "Supply a value required by the current Odoo requirements."
                    if state is ValueCoverageState.MISSING
                    else "The current match check found a problem with this field; review its saved decision."
                )
            if not inputs_current and definition is not None:
                state, reason = (
                    ValueCoverageState.STALE,
                    "Source or Odoo requirements changed; refresh the saved matching choices.",
                )
            elif state is ValueCoverageState.CHOSEN:
                if validation_current:
                    state = ValueCoverageState.CHECKED
                elif validation is not None:
                    state, reason = (
                        ValueCoverageState.STALE,
                        "The saved provider changed since the last match check.",
                    )
            values.append(
                RequiredValue(
                    source.dataset_id,
                    name,
                    field.label if field else name,
                    bool(field and field.type in {"many2one", "many2many", "one2many"}),
                    name in identity_targets,
                    provider,
                    state,
                    reason,
                    review_default,
                    bool(
                        field
                        and field.create_default_present
                        and provider is RequiredValueProvider.DEFAULT
                    ),
                    FieldDecisionLocation.REQUIREMENTS
                    if field is None
                    or field.readonly
                    and provider is RequiredValueProvider.MANAGED
                    else FieldDecisionLocation.READONLY_RECOVERY
                    if field.readonly
                    and not getattr(providers.get(name), "validate_only", False)
                    else FieldDecisionLocation.FIELD,
                )
            )
        identity_state = _identity_state(
            mapping,
            identity_results.get(source.dataset_id),
            inputs_current,
            bool(live_check),
            live_check_current,
        )
        required_parents = tuple(
            item
            for item in values
            if item.relationship and item.provider is not RequiredValueProvider.MANAGED
        )
        parent_states = []
        for value in required_parents:
            result = parent_results.get((source.dataset_id, value.target_field))
            if (
                value.state
                in {
                    ValueCoverageState.MISSING,
                    ValueCoverageState.ATTENTION,
                    ValueCoverageState.STALE,
                }
                or value.provider is RequiredValueProvider.DEFAULT
            ):
                parent_states.append(value.state)
            elif result is None or not result.checked:
                parent_states.append(
                    ValueCoverageState.STALE
                    if live_check and not live_check_current
                    else ValueCoverageState.CHOSEN
                )
            else:
                parent_states.append(
                    ValueCoverageState.ATTENTION
                    if result.blocked_count
                    else ValueCoverageState.CHECKED
                )
        parent_state = next(
            (
                state
                for state in (
                    ValueCoverageState.MISSING,
                    ValueCoverageState.ATTENTION,
                    ValueCoverageState.STALE,
                    ValueCoverageState.CHOSEN,
                    ValueCoverageState.CHECKED,
                )
                if state in parent_states
            ),
            ValueCoverageState.NOT_APPLICABLE,
        )
        check_state = (
            ValueCoverageState.MISSING
            if mapping is None
            else ValueCoverageState.STALE
            if not inputs_current or validation is not None and not validation_current
            else ValueCoverageState.ATTENTION
            if source.dataset_id in dataset_errors
            else ValueCoverageState.CHECKED
            if validation_current
            else ValueCoverageState.CHOSEN
        )
        tables.append(
            TableValueCoverage(
                source.dataset_id,
                source.name,
                mapping.target_model if mapping else "",
                model.label if model else "Choose Odoo data",
                identity_state,
                parent_state,
                check_state,
                creates,
                tuple(values),
            )
        )
    return RequiredValuesProjection(tuple(tables), inputs_current, validation_current)


def _identity_state(
    mapping: DatasetMapping | None, result, inputs_current, has_check, check_current
):
    if mapping and mapping.mode is MappingTargetMode.ODOO_PINNED_UPDATE:
        return ValueCoverageState.NOT_APPLICABLE
    if (
        mapping is None
        or not mapping.source_identity_column_keys
        or not mapping.target_identity
    ):
        return ValueCoverageState.MISSING
    if not inputs_current or has_check and not check_current:
        return ValueCoverageState.STALE
    if result is not None and result.checked:
        return (
            ValueCoverageState.ATTENTION
            if result.blocked_count
            else ValueCoverageState.CHECKED
        )
    return ValueCoverageState.CHOSEN
