"""Coordinate pure Stage D mapping semantic validation.

Layer: domain. ``MappingSemanticValidator`` canonicalizes one complete mapping,
builds a read-only validation context, and delegates identities, scalar fields,
relationships, dependencies, and control totals to focused validators. It
performs no persistence, source-file read, or Odoo call.

See ``docs/architecture/python-code-map.md`` and
``tests/test_mapping_validation.py``.
"""

from __future__ import annotations

from typing import Any, Mapping

from ...odoo_source_policy import CURRENT_ODOO_SOURCE_POLICY
from ...source_binding import OdooSourceBinding, SourceOriginKind
from ...schema.governance import SchemaGovernance
from ..canonicalization import canonicalize_mapping_definition
from ..contracts import (
    DatasetMapping,
    MappingDefinition,
    MappingTargetMode,
    ScalarValueSource,
    TargetFieldHandling,
)
from .common import _issue
from .context import (
    SchemaCatalogView,
    SourceSelectionView,
    ValidationContext,
)
from .control_totals import _validate_control_totals
from .dependencies import _validate_dependencies
from .evidence import (
    DeferredRuntimeCheck,
    MappingValidationIssue,
    MappingValidationResult,
    MappingValidationStatus,
)
from .identities import (
    _validate_identity_component,
    _validate_source_identity,
)
from .relationships import _validate_relationship
from .scalars import _validate_scalar


def _is_pinned_write_field(metadata: object) -> bool:
    """Apply the fail-closed Tier-1 write policy to captured schema evidence."""

    return bool(
        getattr(metadata, "type", None)
        in CURRENT_ODOO_SOURCE_POLICY.writable_field_types
        and getattr(metadata, "stored", None) is True
        and getattr(metadata, "readonly", None) is False
        and getattr(metadata, "computed", None) is False
        and getattr(metadata, "related", None) is False
        and getattr(metadata, "translated", None) is False
        and getattr(metadata, "company_dependent", None) is False
    )


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


class MappingSemanticValidator:
    """Return deterministic issues and runtime checks for one mapping definition."""

    def validate(
        self,
        definition: MappingDefinition,
        source_selection: SourceSelectionView,
        schema_catalog: SchemaCatalogView,
        schema_governance: SchemaGovernance | None,
    ) -> MappingValidationResult:
        """Validate exact source/schema bindings and every declared dataset rule."""

        definition = canonicalize_mapping_definition(definition)
        context = ValidationContext.build(
            definition,
            source_selection,
            schema_catalog,
            schema_governance,
        )
        issues: list[MappingValidationIssue] = []
        coverage: list[Mapping[str, Any]] = []
        deferred: list[DeferredRuntimeCheck] = []

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
        pinned_only = bool(definition.datasets) and all(
            dataset.mode is MappingTargetMode.ODOO_PINNED_UPDATE
            and (
                source_dataset := context.source_datasets.get(dataset.dataset_id)
            ) is not None
            and source_dataset.origin is SourceOriginKind.ODOO
            for dataset in definition.datasets
        )
        if not pinned_only and (
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

        seen_dataset_ids: set[str] = set()
        dependencies: dict[str, set[str]] = {}
        required_on_create_dependencies: dict[str, set[str]] = {}

        for dataset_index, dataset in enumerate(definition.datasets):
            base = f"/datasets/{dataset_index}"
            dependencies.setdefault(dataset.dataset_id, set())
            required_on_create_dependencies.setdefault(dataset.dataset_id, set())
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
            source_dataset = context.source_datasets.get(dataset.dataset_id)
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
            is_odoo_source = source_dataset.origin is SourceOriginKind.ODOO
            is_pinned_update = (
                dataset.mode is MappingTargetMode.ODOO_PINNED_UPDATE
            )
            if is_odoo_source and not is_pinned_update:
                issues.append(
                    _issue(
                        "MAPPING_ODOO_PINNED_MODE_REQUIRED",
                        f"{base}/mode",
                        "Captured Odoo records can only use pinned update mode.",
                        "Use the protected records selected from Odoo; creating records is not available.",
                        dataset=dataset,
                    )
                )
            if is_pinned_update and not is_odoo_source:
                issues.append(
                    _issue(
                        "MAPPING_ODOO_PINNED_SOURCE_REQUIRED",
                        f"{base}/mode",
                        "Pinned update mode requires a protected Odoo capture.",
                        "Choose the normal file mapping mode for this dataset.",
                        dataset=dataset,
                    )
                )
            if not is_odoo_source and dataset.approved_write_fields:
                issues.append(
                    _issue(
                        "MAPPING_ODOO_WRITE_APPROVAL_INVALID",
                        f"{base}/approved_write_fields",
                        "Odoo update approvals require captured Odoo records.",
                        "Remove these approvals from the file mapping.",
                        dataset=dataset,
                    )
                )
            if is_odoo_source:
                source_binding = source_dataset.source
                if not isinstance(source_binding, OdooSourceBinding):
                    issues.append(
                        _issue(
                            "MAPPING_ODOO_SOURCE_BINDING_INVALID",
                            f"{base}/dataset_id",
                            "The Odoo source binding is not valid.",
                            "Refresh the Odoo capture before mapping.",
                            dataset=dataset,
                        )
                    )
                elif dataset.target_model != source_binding.model:
                    issues.append(
                        _issue(
                            "MAPPING_ODOO_TARGET_MODEL_MISMATCH",
                            f"{base}/target_model",
                            "Captured Odoo records must stay bound to their originating model.",
                            "Use the model recorded by the Odoo capture.",
                            dataset=dataset,
                        )
                    )
            model = context.schema_models.get(dataset.target_model)
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
            fields = context.fields_by_model[dataset.target_model]
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

            if is_odoo_source:
                if (
                    dataset.source_identity_column_keys
                    or dataset.target_identity
                    or dataset.target_scope
                ):
                    issues.append(
                        _issue(
                            "MAPPING_ODOO_PORTABLE_IDENTITY_FORBIDDEN",
                            f"{base}/source_identity_column_keys",
                            "Pinned Odoo updates do not use portable business or numeric record keys.",
                            "Remove source and target identity fields; protected capture evidence identifies each record.",
                            dataset=dataset,
                        )
                    )
            else:
                _validate_source_identity(dataset, base, columns, issues)
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
                    _validate_identity_component(
                        context,
                        dataset,
                        component,
                        component_path,
                        columns,
                        dependencies,
                        required_on_create_dependencies,
                        issues,
                    )
                    provided.update(component.target_fields)
                    collected.extend(component.target_fields)
            if is_odoo_source:
                pass
            elif not dataset.target_identity:
                issues.append(
                    _issue(
                        "MAPPING_TARGET_IDENTITY_MISSING",
                        f"{base}/target_identity",
                        "A target identity is required.",
                        "Choose one confirmed business key and map its components.",
                        dataset=dataset,
                    )
                )
            elif not context.has_governed_key(
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
            captured_field_names = {
                item.source_name for item in source_dataset.columns
            }
            approved_write_fields = set(dataset.approved_write_fields)
            mapped_write_fields: set[str] = set()
            for field_index, field_mapping in enumerate(dataset.fields):
                path = f"{base}/fields/{field_index}"
                _validate_scalar(
                    context,
                    dataset,
                    field_mapping,
                    path,
                    columns,
                    issues,
                )
                _claim_target(
                    dataset,
                    field_mapping.target_field,
                    path,
                    target_owners,
                    issues,
                )
                provided.add(field_mapping.target_field)
                if is_odoo_source and not field_mapping.validate_only:
                    mapped_write_fields.add(field_mapping.target_field)
                    metadata = fields.get(field_mapping.target_field)
                    if field_mapping.target_field not in approved_write_fields:
                        issues.append(
                            _issue(
                                "MAPPING_ODOO_WRITE_FIELD_UNAPPROVED",
                                path,
                                f"{field_mapping.target_field} is mapped for an update but has not been explicitly approved.",
                                "Select the separate update approval for this field.",
                                dataset=dataset,
                                target_field=field_mapping.target_field,
                            )
                        )
                    if metadata is not None and not _is_pinned_write_field(metadata):
                        issues.append(
                            _issue(
                                "MAPPING_ODOO_WRITE_FIELD_INELIGIBLE",
                                path,
                                f"{dataset.target_model}.{field_mapping.target_field} is outside the safe Tier-1 update policy.",
                                "Use a stored, writable scalar field without computed, related, translated, or company-dependent behavior.",
                                dataset=dataset,
                                target_field=field_mapping.target_field,
                            )
                        )
                    if field_mapping.target_field not in captured_field_names:
                        issues.append(
                            _issue(
                                "MAPPING_ODOO_WRITE_BASELINE_MISSING",
                                path,
                                f"The capture has no original value for {field_mapping.target_field}.",
                                "Refresh the capture and include this field before approving an update.",
                                dataset=dataset,
                                target_field=field_mapping.target_field,
                            )
                        )
                    if (
                        field_mapping.value_source is ScalarValueSource.ODOO_DEFAULT
                        or field_mapping.required_on_create
                    ):
                        issues.append(
                            _issue(
                                "MAPPING_ODOO_CREATE_SEMANTICS_FORBIDDEN",
                                path,
                                "Create-time field behavior is not valid for pinned updates.",
                                "Provide a captured value, a fixed value, or a transformation without create-only behavior.",
                                dataset=dataset,
                                target_field=field_mapping.target_field,
                            )
                        )

            if is_odoo_source:
                for target_field in sorted(
                    approved_write_fields.difference(mapped_write_fields)
                ):
                    issues.append(
                        _issue(
                            "MAPPING_ODOO_WRITE_APPROVAL_UNUSED",
                            f"{base}/approved_write_fields",
                            f"{target_field} is approved but has no active write mapping.",
                            "Map the field for update or remove its approval.",
                            dataset=dataset,
                            target_field=target_field,
                        )
                    )

            _validate_control_totals(context, dataset, base, issues)

            for relation_index, relation in enumerate(
                dataset.relationships
            ):
                path = f"{base}/relationships/{relation_index}"
                _validate_relationship(
                    context,
                    dataset,
                    relation,
                    path,
                    columns,
                    dependencies,
                    required_on_create_dependencies,
                    issues,
                )
                _claim_target(
                    dataset,
                    relation.target_field,
                    path,
                    target_owners,
                    issues,
                )
                provided.add(relation.target_field)

            if is_odoo_source and dataset.relationships:
                issues.append(
                    _issue(
                        "MAPPING_ODOO_RELATIONSHIP_UNSUPPORTED",
                        f"{base}/relationships",
                        "Relationship writes are not yet supported for captured Odoo records.",
                        "Remove relationship mappings from this pinned update.",
                        dataset=dataset,
                    )
                )

            intentionally_omitted: set[str] = set()
            if is_odoo_source and dataset.target_field_dispositions:
                issues.append(
                    _issue(
                        "MAPPING_ODOO_CREATE_SEMANTICS_FORBIDDEN",
                        f"{base}/target_field_dispositions",
                        "Create-time Odoo field decisions are not valid for pinned updates.",
                        "Remove Odoo default and managed-field decisions.",
                        dataset=dataset,
                    )
                )
            for disposition_index, disposition in enumerate(
                dataset.target_field_dispositions
            ):
                path = f"{base}/target_field_dispositions/{disposition_index}"
                metadata = fields.get(disposition.target_field)
                _claim_target(
                    dataset,
                    disposition.target_field,
                    path,
                    target_owners,
                    issues,
                )
                if metadata is None:
                    issues.append(
                        _issue(
                            "MAPPING_TARGET_FIELD_UNKNOWN",
                            path,
                            (
                                f"Target field {dataset.target_model}."
                                f"{disposition.target_field} is unavailable."
                            ),
                            "Choose a field from the captured schema.",
                            dataset=dataset,
                            target_field=disposition.target_field,
                        )
                    )
                    continue
                if dataset.mode is MappingTargetMode.REFERENCE:
                    issues.append(
                        _issue(
                            "MAPPING_TARGET_FIELD_DISPOSITION_INVALID",
                            path,
                            "Reference-only tables do not create Odoo records.",
                            "Remove this create-time field decision.",
                            dataset=dataset,
                            target_field=disposition.target_field,
                        )
                    )
                    continue
                if metadata.readonly or not metadata.required:
                    issues.append(
                        _issue(
                            "MAPPING_TARGET_FIELD_DISPOSITION_INVALID",
                            path,
                            (
                                f"{dataset.target_model}."
                                f"{disposition.target_field} does not need a "
                                "required-field decision."
                            ),
                            "Remove this create-time field decision.",
                            dataset=dataset,
                            target_field=disposition.target_field,
                        )
                    )
                    continue
                if (
                    disposition.handling is TargetFieldHandling.ODOO_MANAGED
                    and metadata.type not in {"one2many", "many2many"}
                    and metadata.computed is not True
                    and metadata.related is not True
                ):
                    issues.append(
                        _issue(
                            "MAPPING_TARGET_FIELD_DISPOSITION_INVALID",
                            path,
                            (
                                f"{dataset.target_model}."
                                f"{disposition.target_field} is not identified "
                                "as an Odoo-managed field."
                            ),
                            (
                                "Use an incoming value, a fixed value, or the "
                                "Odoo-default decision instead."
                            ),
                            dataset=dataset,
                            target_field=disposition.target_field,
                        )
                    )
                    continue
                intentionally_omitted.add(disposition.target_field)
                if disposition.handling is TargetFieldHandling.ODOO_DEFAULT:
                    issues.append(
                        _issue(
                            "MAPPING_ODOO_DEFAULT_UNVERIFIED",
                            path,
                            (
                                f"{disposition.target_field} will be omitted so "
                                "Odoo can apply its runtime default."
                            ),
                            (
                                "Acknowledge this warning and verify the default "
                                "on the target."
                            ),
                            severity="warning",
                            dataset=dataset,
                            target_field=disposition.target_field,
                        )
                    )
                else:
                    issues.append(
                        _issue(
                            "MAPPING_ODOO_MANAGED_UNVERIFIED",
                            path,
                            (
                                f"{disposition.target_field} will be omitted "
                                "because Odoo is expected to maintain it."
                            ),
                            (
                                "Acknowledge this warning and verify that Odoo "
                                "creates or computes the field."
                            ),
                            severity="warning",
                            dataset=dataset,
                            target_field=disposition.target_field,
                        )
                    )

            if (
                dataset.mode is not MappingTargetMode.REFERENCE
                and not is_odoo_source
            ):
                for target_field in sorted(fields):
                    metadata = fields[target_field]
                    if (
                        metadata.required
                        and not metadata.readonly
                        and target_field not in provided
                        and target_field not in intentionally_omitted
                    ):
                        issues.append(
                            _issue(
                                "MAPPING_REQUIRED_FIELD_UNMAPPED",
                                f"{base}/target_model",
                                (
                                    f"Required target field {dataset.target_model}."
                                    f"{target_field} has no value provider."
                                ),
                                (
                                    "Choose incoming data, one fixed value, an "
                                    "Odoo default, or an Odoo-managed field decision."
                                ),
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
            runtime_selection_fields = {
                field_mapping.target_field
                for field_mapping in dataset.fields
                if field_mapping.value_source is not ScalarValueSource.ODOO_DEFAULT
                and fields.get(field_mapping.target_field) is not None
                and fields[field_mapping.target_field].type == "selection"
            }
            runtime_selection_fields.update(
                target_field
                for component in (
                    *dataset.target_identity,
                    *dataset.target_scope,
                )
                if component.resolver is None
                for target_field in component.target_fields
                if fields.get(target_field) is not None
                and fields[target_field].type == "selection"
            )
            deferred.extend(
                DeferredRuntimeCheck(
                    code="SELECTION_VALUE_AVAILABLE",
                    dataset_id=dataset.dataset_id,
                    message=(
                        f"Verify every final {target_field} value against the "
                        "fresh Odoo choice codes before loading."
                    ),
                )
                for target_field in sorted(runtime_selection_fields)
            )
            if dataset.relationships:
                deferred.append(
                    DeferredRuntimeCheck(
                        code="TARGET_REFERENCE_COVERAGE_DEFERRED",
                        dataset_id=dataset.dataset_id,
                        message=(
                            "Target existence and uniqueness remain bound to "
                            "fresh preparation evidence, not mapping submission."
                        ),
                    )
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
                ) if not is_odoo_source else (
                    DeferredRuntimeCheck(
                        code="REQUIRED_ROW_VALUES",
                        dataset_id=dataset.dataset_id,
                        message="Verify required values on every staged row.",
                    ),
                )
            )

        for dataset_id in sorted(
            set(context.source_datasets).difference(seen_dataset_ids)
        ):
            source_dataset = context.source_datasets[dataset_id]
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

        _validate_dependencies(
            dependencies,
            required_on_create_dependencies,
            context.datasets_by_id,
            issues,
        )
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
