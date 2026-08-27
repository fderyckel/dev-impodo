"""Orchestrate Stage D mapping drafts, validation, revisions, and submission.

Layer: application service.

The mapping router supplies complete dataset-centric definitions.
``MappingWorkspaceService`` binds them to the effective source selection and
captured/governed schema, delegates deterministic meaning checks to
``MappingSemanticValidator``, and persists through focused repository ports.
Working drafts are recoverable but unchecked; revisions and submissions are
immutable evidence.

See ``docs/architecture/python-code-map.md``,
``docs/developer/contracts/evidence-lifecycle.md``, and
``tests/integration/duckdb/test_workspace.py``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Iterable, Protocol
from uuid import uuid4

from impodo.domain.shared.access import Actor, AuthorizationPolicy, Capability
from impodo.domain.schema.governance import SchemaGovernance
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    MappingDefinition,
    MappingTargetMode,
    ResolverOrigin,
    TargetFieldDisposition,
    TargetFieldHandling,
)
from impodo.domain.mapping.create_field_policy import (
    CreateFieldCoverage,
    evaluate_create_field,
    supports_create_default_capture,
)
from impodo.domain.mapping.artifacts import (
    MappingRevision,
    MappingSubmission,
)
from impodo.domain.mapping.canonicalization import canonicalize_mapping_definition
from impodo.domain.mapping.validation.evidence import (
    MappingValidationResult,
    MappingValidationStatus,
    mapping_issue_fingerprint,
)
from impodo.domain.mapping.validation.validator import MappingSemanticValidator
from impodo.domain.workspace.reference_keys import REFERENCE_POLICY_HASH, standard_reference_key
from impodo.domain.workspace.supporting_lookups import (
    SupportingLookupSnapshot,
    supporting_lookup_key,
)
from .categorical_coverage import CategoricalCoverageService
from impodo.domain.workspace.contracts import (
    MappingWorkingDraft,
    OdooSchemaCatalog,
    SchemaOrigin,
    SourceSelection,
)
from impodo.domain.workspace.errors import WorkspaceError


class MappingSourceRepository(Protocol):
    """Provide the physical plus derived dataset selection visible to mapping."""

    def get_mapping_source_selection(
        self,
        workspace_id: str,
    ) -> SourceSelection | None:
        """Return the effective physical-plus-derived selection visible to mapping."""
        ...


class MappingSchemaRepository(Protocol):
    """Provide the captured schema and optional exact key governance."""

    def get_odoo_schema_catalog(
        self,
        workspace_id: str,
    ) -> OdooSchemaCatalog | None:
        """Return the current target-bound detailed schema."""
        ...

    def get_schema_governance(
        self,
        workspace_id: str,
    ) -> SchemaGovernance | None:
        """Return current confirmed business-key governance, when available."""
        ...


class MappingWorkspaceRepository(Protocol):
    """Persist recoverable drafts and immutable mapping evidence."""

    def get_mapping_working_draft(
        self,
        workspace_id: str,
    ) -> MappingWorkingDraft | None:
        """Return recoverable unchecked editor state, if one exists."""
        ...
    def save_mapping_working_draft(
        self,
        workspace_id: str,
        draft: MappingWorkingDraft,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> None:
        """Replace editor state only at the expected optimistic draft version."""
        ...

    def get_mapping_revision(
        self,
        workspace_id: str,
        version: int | None = None,
    ) -> MappingRevision | None:
        """Return the current or requested immutable mapping revision."""
        ...

    def get_mapping_validation(
        self,
        workspace_id: str,
        version: int,
    ) -> MappingValidationResult | None:
        """Return the stored validation for one checked mapping revision."""
        ...

    def get_mapping_submission(
        self,
        workspace_id: str,
        version: int | None = None,
    ) -> MappingSubmission | None:
        """Return the newest submission globally or for one revision."""
        ...

    def list_mapping_revisions(
        self,
        workspace_id: str,
    ) -> tuple[MappingRevision, ...]:
        """Return the complete immutable revision history in version order."""
        ...

    def save_mapping_revision(
        self,
        workspace_id: str,
        revision: MappingRevision,
        *,
        validation: MappingValidationResult,
        expected_parent_version: int | None,
        expected_working_draft_version: int | None,
        checked_draft: MappingWorkingDraft,
        actor: Actor,
    ) -> None:
        """Promote one expected draft state to a checked revision."""
        ...

    def save_mapping_validation(
        self,
        workspace_id: str,
        version: int,
        validation: MappingValidationResult,
        *,
        actor: Actor,
    ) -> None:
        """Append deterministic revalidation for one exact stored revision."""
        ...

    def save_mapping_submission(
        self,
        workspace_id: str,
        submission: MappingSubmission,
        *,
        actor: Actor,
    ) -> None:
        """Append exact submission evidence after repository-side gate checks."""
        ...


class MappingSupportingLookupRepository(Protocol):
    """Return current bounded reference evidence used by Stage 3 validation."""

    def get_current(
        self,
        workspace_id: str,
        lookup_key: str,
    ) -> SupportingLookupSnapshot | None: ...


class MappingWorkspaceService:
    """Own Stage D concurrency, evidence binding, and submission gates.

    The service keeps recoverable editor progress separate from semantic
    revisions. Submission additionally requires a verified schema, a non-
    invalid validation result, and acknowledgement of the exact current
    warning fingerprints.
    """

    def __init__(
        self,
        sources: MappingSourceRepository,
        schemas: MappingSchemaRepository,
        mappings: MappingWorkspaceRepository,
        authorization: AuthorizationPolicy,
        categorical_coverage: CategoricalCoverageService,
        supporting_lookups: MappingSupportingLookupRepository | None = None,
    ) -> None:
        self.sources = sources
        self.schemas = schemas
        self.mappings = mappings
        self.authorization = authorization
        self.categorical_coverage = categorical_coverage
        self.supporting_lookups = supporting_lookups
        self.validator = MappingSemanticValidator()

    def save_working_draft(
        self,
        workspace_id: str,
        *,
        datasets: Iterable[DatasetMapping],
        expected_version: int | None,
        actor: Actor,
    ) -> MappingWorkingDraft:
        """Persist incomplete browser work without semantic validation."""

        self.authorization.require(
            actor,
            Capability.MAPPING_EDIT,
            workspace_id=workspace_id,
        )
        selection = self.sources.get_mapping_source_selection(workspace_id)
        schema = self.schemas.get_odoo_schema_catalog(workspace_id)
        governance = self.schemas.get_schema_governance(workspace_id)
        if selection is None or schema is None:
            raise WorkspaceError(
                "Freeze datasets and capture Odoo schema first"
            )
        current = self.mappings.get_mapping_revision(workspace_id)
        existing = self.mappings.get_mapping_working_draft(workspace_id)
        actual_version = existing.version if existing else None
        if expected_version != actual_version:
            raise WorkspaceError(
                "The working draft was modified by another request; reload it"
            )
        if existing is not None:
            mapping_id = existing.mapping_id
        elif current is not None:
            mapping_id = current.mapping_id
        else:
            mapping_id = str(uuid4())
        definition = canonicalize_mapping_definition(
            MappingDefinition(
                mapping_id=mapping_id,
                source_selection_hash=selection.content_hash,
                schema_hash=(
                    governance.content_hash
                    if governance is not None
                    else schema.content_hash
                ),
                datasets=tuple(datasets),
            )
        )
        draft = MappingWorkingDraft(
            mapping_id=mapping_id,
            version=(actual_version or 0) + 1,
            workspace_id=workspace_id,
            base_mapping_version=current.version if current else None,
            definition=definition,
            updated_at=datetime.now(timezone.utc),
            updated_by=actor.identity.display_name,
        )
        self.mappings.save_mapping_working_draft(
            workspace_id,
            draft,
            expected_version=expected_version,
            actor=actor,
        )
        return draft

    def remove_readonly_field_mappings(
        self,
        workspace_id: str,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> tuple[MappingWorkingDraft, int]:
        """Remove write mappings for fields the captured schema marks readonly.

        Check-only mappings are intentionally retained: they do not prepare a
        value for Odoo and remain an explicit advanced review choice.
        """

        self.authorization.require(
            actor,
            Capability.MAPPING_EDIT,
            workspace_id=workspace_id,
        )
        schema = self.schemas.get_odoo_schema_catalog(workspace_id)
        existing = self.mappings.get_mapping_working_draft(workspace_id)
        if schema is None or existing is None:
            raise WorkspaceError(
                "Load the current matching draft and Odoo fields first"
            )
        if expected_version != existing.version:
            raise WorkspaceError(
                "The working draft was modified by another request; reload it"
            )

        readonly_by_model = {
            model.name: {
                field.name for field in model.fields if field.readonly
            }
            for model in schema.models
        }
        removed_count = 0
        cleaned_datasets: list[DatasetMapping] = []
        for dataset in existing.definition.datasets:
            readonly_fields = readonly_by_model.get(dataset.target_model, set())
            kept_fields = tuple(
                field
                for field in dataset.fields
                if field.validate_only or field.target_field not in readonly_fields
            )
            kept_relationships = tuple(
                relationship
                for relationship in dataset.relationships
                if (
                    relationship.validate_only
                    or relationship.target_field not in readonly_fields
                )
            )
            removed_count += len(dataset.fields) - len(kept_fields)
            removed_count += len(dataset.relationships) - len(kept_relationships)
            cleaned_datasets.append(
                replace(
                    dataset,
                    fields=kept_fields,
                    relationships=kept_relationships,
                    approved_write_fields=tuple(
                        target
                        for target in dataset.approved_write_fields
                        if target not in readonly_fields
                    ),
                )
            )
        if not removed_count:
            raise WorkspaceError(
                "No Odoo-managed field matches need to be removed"
            )
        draft = self.save_working_draft(
            workspace_id,
            datasets=cleaned_datasets,
            expected_version=expected_version,
            actor=actor,
        )
        return draft, removed_count

    def set_target_field_disposition(
        self,
        workspace_id: str,
        *,
        dataset_id: str,
        target_field: str,
        handling: TargetFieldHandling | None,
        expected_version: int | None,
        actor: Actor,
    ) -> MappingWorkingDraft:
        """Save or clear one explicit decision to leave an Odoo field unset."""

        self.authorization.require(
            actor,
            Capability.MAPPING_EDIT,
            workspace_id=workspace_id,
        )
        schema = self.schemas.get_odoo_schema_catalog(workspace_id)
        existing = self.mappings.get_mapping_working_draft(workspace_id)
        if schema is None or existing is None:
            raise WorkspaceError(
                "Load the current matching draft and Odoo fields first"
            )
        if expected_version != existing.version:
            raise WorkspaceError(
                "The working draft was modified by another request; reload it"
            )
        dataset = next(
            (
                item
                for item in existing.definition.datasets
                if item.dataset_id == dataset_id
            ),
            None,
        )
        if dataset is None:
            raise WorkspaceError("The matching table is no longer current")
        model = next(
            (item for item in schema.models if item.name == dataset.target_model),
            None,
        )
        metadata = next(
            (
                item
                for item in (model.fields if model is not None else ())
                if item.name == target_field
            ),
            None,
        )
        if metadata is None or metadata.readonly or not metadata.required:
            raise WorkspaceError(
                "This Odoo field does not need a required-field decision"
            )
        supplied_targets = {
            field.target_field for field in dataset.fields
        } | {
            relationship.target_field
            for relationship in dataset.relationships
        } | {
            target
            for component in (*dataset.target_identity, *dataset.target_scope)
            for target in component.target_fields
        }
        if handling is not None and target_field in supplied_targets:
            raise WorkspaceError(
                "Remove the current field match before leaving this field to Odoo"
            )
        if (
            handling is TargetFieldHandling.ODOO_MANAGED
            and metadata.type not in {"one2many", "many2many"}
            and metadata.computed is not True
            and metadata.related is not True
        ):
            raise WorkspaceError(
                "The captured Odoo details do not identify this field as Odoo-managed"
            )
        if handling is TargetFieldHandling.ODOO_DEFAULT:
            assessment = evaluate_create_field(
                metadata,
                provided=False,
                handling=handling,
            )
            if assessment.coverage is not CreateFieldCoverage.DEFAULT_CONFIRMED:
                raise WorkspaceError(
                    "Odoo did not provide a verified create default for this field"
                )

        dispositions = {
            item.target_field: item
            for item in dataset.target_field_dispositions
        }
        if handling is None:
            if target_field not in dispositions:
                raise WorkspaceError("This Odoo-field decision is already cleared")
            dispositions.pop(target_field)
        else:
            dispositions[target_field] = TargetFieldDisposition(
                target_field=target_field,
                handling=handling,
            )
        updated_datasets = tuple(
            replace(
                item,
                target_field_dispositions=tuple(
                    dispositions[target]
                    for target in sorted(dispositions)
                ),
            )
            if item.dataset_id == dataset_id
            else item
            for item in existing.definition.datasets
        )
        return self.save_working_draft(
            workspace_id,
            datasets=updated_datasets,
            expected_version=expected_version,
            actor=actor,
        )

    def confirm_available_odoo_defaults(
        self,
        workspace_id: str,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> tuple[MappingWorkingDraft, int]:
        """Confirm every currently uncovered verified create default together."""

        self.authorization.require(
            actor,
            Capability.MAPPING_EDIT,
            workspace_id=workspace_id,
        )
        schema = self.schemas.get_odoo_schema_catalog(workspace_id)
        existing = self.mappings.get_mapping_working_draft(workspace_id)
        if schema is None or existing is None:
            raise WorkspaceError(
                "Load the current matching draft and Odoo fields first"
            )
        if expected_version != existing.version:
            raise WorkspaceError(
                "The working draft was modified by another request; reload it"
            )
        models = {model.name: model for model in schema.models}
        updated: list[DatasetMapping] = []
        confirmed_count = 0
        for dataset in existing.definition.datasets:
            if dataset.mode in {
                MappingTargetMode.REFERENCE,
                MappingTargetMode.ODOO_PINNED_UPDATE,
            }:
                updated.append(dataset)
                continue
            supplied = {
                item.target_field
                for item in (*dataset.fields, *dataset.relationships)
            }
            supplied.update(
                target
                for component in (*dataset.target_identity, *dataset.target_scope)
                for target in component.target_fields
            )
            dispositions = {
                item.target_field: item
                for item in dataset.target_field_dispositions
            }
            model = models.get(dataset.target_model)
            for field in model.fields if model is not None else ():
                if field.name in dispositions:
                    continue
                assessment = evaluate_create_field(
                    field,
                    provided=field.name in supplied,
                    handling=None,
                )
                if assessment.coverage is not CreateFieldCoverage.DEFAULT_AVAILABLE:
                    continue
                dispositions[field.name] = TargetFieldDisposition(
                    target_field=field.name,
                    handling=TargetFieldHandling.ODOO_DEFAULT,
                )
                confirmed_count += 1
            updated.append(
                replace(
                    dataset,
                    target_field_dispositions=tuple(
                        dispositions[name] for name in sorted(dispositions)
                    ),
                )
            )
        if not confirmed_count:
            raise WorkspaceError("No verified Odoo defaults are waiting for review")
        draft = self.save_working_draft(
            workspace_id,
            datasets=updated,
            expected_version=expected_version,
            actor=actor,
        )
        return draft, confirmed_count

    def default_recovery_fields(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Return checked required scalar blockers eligible for ``default_get``."""

        self.authorization.require(
            actor,
            Capability.MAPPING_EDIT,
            workspace_id=workspace_id,
        )
        schema = self.schemas.get_odoo_schema_catalog(workspace_id)
        revision = self.mappings.get_mapping_revision(workspace_id)
        working = self.mappings.get_mapping_working_draft(workspace_id)
        if schema is None or revision is None or working is None:
            raise WorkspaceError(
                "Check the current field matches before asking Odoo to decide"
            )
        if working.content_hash != revision.definition.content_hash:
            raise WorkspaceError(
                "Check the saved field changes before asking Odoo to decide"
            )
        validation = self.mappings.get_mapping_validation(
            workspace_id,
            revision.version,
        )
        if validation is None:
            raise WorkspaceError(
                "Check the current field matches before asking Odoo to decide"
            )
        fields_by_model = {
            model.name: {field.name: field for field in model.fields}
            for model in schema.models
        }
        grouped: dict[str, set[str]] = {}
        for issue in validation.issues:
            if (
                issue.severity != "error"
                or issue.code != "MAPPING_REQUIRED_FIELD_UNMAPPED"
                or not issue.target_model
                or not issue.target_field
            ):
                continue
            field = fields_by_model.get(issue.target_model, {}).get(
                issue.target_field
            )
            if field is not None and supports_create_default_capture(field):
                grouped.setdefault(issue.target_model, set()).add(field.name)
        if not grouped:
            raise WorkspaceError(
                "No required scalar fields are waiting for an Odoo default check"
            )
        return tuple(
            (model_name, tuple(sorted(field_names)))
            for model_name, field_names in sorted(grouped.items())
        )

    def check_definition(
        self,
        workspace_id: str,
        *,
        datasets: Iterable[DatasetMapping],
        expected_parent_version: int | None,
        expected_working_draft_version: int | None,
        actor: Actor,
    ) -> tuple[MappingRevision, MappingValidationResult]:
        """Check one editor state and persist only new semantic content."""

        self.authorization.require(
            actor,
            Capability.MAPPING_EDIT,
            workspace_id=workspace_id,
        )
        selection = self.sources.get_mapping_source_selection(workspace_id)
        schema = self.schemas.get_odoo_schema_catalog(workspace_id)
        governance = self.schemas.get_schema_governance(workspace_id)
        if selection is None or schema is None:
            raise WorkspaceError(
                "Freeze datasets and capture Odoo schema first"
            )
        current = self.mappings.get_mapping_revision(workspace_id)
        actual_parent = current.version if current else None
        if expected_parent_version != actual_parent:
            raise WorkspaceError(
                "The mapping was modified by another request; reload it"
            )
        working_draft = self.mappings.get_mapping_working_draft(workspace_id)
        actual_working_version = (
            working_draft.version if working_draft is not None else None
        )
        if expected_working_draft_version != actual_working_version:
            raise WorkspaceError(
                "The working draft was modified by another request; reload it"
            )
        expected_schema_hash = (
            governance.content_hash
            if governance is not None
            else schema.content_hash
        )
        compatible_working_draft = (
            working_draft
            if working_draft is not None
            and working_draft.definition.source_selection_hash
            == selection.content_hash
            and working_draft.definition.schema_hash == expected_schema_hash
            else None
        )
        mapping_id = (
            current.mapping_id
            if current is not None
            else (
                compatible_working_draft.mapping_id
                if compatible_working_draft is not None
                else str(uuid4())
            )
        )
        definition = canonicalize_mapping_definition(
            MappingDefinition(
                mapping_id=mapping_id,
                source_selection_hash=selection.content_hash,
                schema_hash=expected_schema_hash,
                datasets=tuple(datasets),
            )
        )
        validation = self._validate_definition(
            workspace_id,
            definition,
            selection,
            schema,
            governance,
        )
        if (
            current is not None
            and current.definition.content_hash == definition.content_hash
        ):
            if (
                working_draft is not None
                and working_draft.content_hash != definition.content_hash
            ):
                self.save_working_draft(
                    workspace_id,
                    datasets=definition.datasets,
                    expected_version=working_draft.version,
                    actor=actor,
                )
            self.mappings.save_mapping_validation(
                workspace_id,
                current.version,
                validation,
                actor=actor,
            )
            return current, validation
        historical_versions = self.mappings.list_mapping_revisions(workspace_id)
        revision = MappingRevision(
            mapping_id=mapping_id,
            version=(
                max((item.version for item in historical_versions), default=0)
                + 1
            ),
            parent_version=actual_parent,
            definition=definition,
            created_at=datetime.now(timezone.utc),
            created_by=actor.identity.display_name,
        )
        checked_draft = MappingWorkingDraft(
            mapping_id=mapping_id,
            version=(actual_working_version or 0) + 1,
            workspace_id=workspace_id,
            base_mapping_version=revision.version,
            definition=definition,
            updated_at=revision.created_at,
            updated_by=actor.identity.display_name,
        )
        self.mappings.save_mapping_revision(
            workspace_id,
            revision,
            validation=validation,
            expected_parent_version=expected_parent_version,
            expected_working_draft_version=expected_working_draft_version,
            checked_draft=checked_draft,
            actor=actor,
        )
        return revision, validation

    def submit_current(
        self,
        workspace_id: str,
        *,
        datasets: Iterable[DatasetMapping],
        expected_version: int | None,
        expected_working_draft_version: int | None,
        warning_acknowledgements: Iterable[str] = (),
        actor: Actor,
    ) -> MappingSubmission:
        """Confirm the exact current checked revision without rewriting it."""

        self.authorization.require(
            actor,
            Capability.MAPPING_SUBMIT,
            workspace_id=workspace_id,
        )
        selection = self.sources.get_mapping_source_selection(workspace_id)
        schema = self.schemas.get_odoo_schema_catalog(workspace_id)
        governance = self.schemas.get_schema_governance(workspace_id)
        revision = self.mappings.get_mapping_revision(workspace_id)
        if selection is None or schema is None:
            raise WorkspaceError(
                "Freeze datasets and capture Odoo schema first"
            )
        if schema.origin is SchemaOrigin.LOCAL_MANUAL:
            raise WorkspaceError(
                "Capture the live Odoo schema before confirming field matches; "
                "the current local schema is unverified"
            )
        if revision is None:
            raise WorkspaceError(
                "Check the field matches before confirming them"
            )
        if expected_version != revision.version:
            raise WorkspaceError(
                "The mapping was modified by another request; reload it"
            )
        working_draft = self.mappings.get_mapping_working_draft(workspace_id)
        actual_working_version = (
            working_draft.version if working_draft is not None else None
        )
        if expected_working_draft_version != actual_working_version:
            raise WorkspaceError(
                "The working draft was modified by another request; reload it"
            )
        expected_schema_hash = (
            governance.content_hash
            if governance is not None
            else schema.content_hash
        )
        candidate = canonicalize_mapping_definition(
            MappingDefinition(
                mapping_id=revision.mapping_id,
                source_selection_hash=selection.content_hash,
                schema_hash=expected_schema_hash,
                datasets=tuple(datasets),
            )
        )
        if candidate.content_hash != revision.definition.content_hash:
            raise WorkspaceError(
                "These field matches changed after they were checked. "
                "Check matches again before confirming."
            )
        if (
            working_draft is not None
            and working_draft.definition.source_selection_hash
            == selection.content_hash
            and working_draft.definition.schema_hash == expected_schema_hash
            and working_draft.content_hash != revision.definition.content_hash
        ):
            raise WorkspaceError(
                "Saved changes still need checking before confirmation"
            )
        validation = self.mappings.get_mapping_validation(
            workspace_id,
            revision.version,
        )
        if (
            validation is None
            or validation.mapping_content_hash
            != revision.definition.content_hash
            or validation.reference_policy_hash != REFERENCE_POLICY_HASH
        ):
            raise WorkspaceError(
                "Check the current field matches before confirming them"
            )
        if validation.status is MappingValidationStatus.INVALID:
            first = next(
                item
                for item in validation.issues
                if item.severity == "error"
            )
            raise WorkspaceError(
                f"Mapping cannot be submitted: {first.message}"
            )
        warning_fingerprints = tuple(
            sorted(
                mapping_issue_fingerprint(item)
                for item in validation.issues
                if item.severity == "warning"
            )
        )
        acknowledgements = frozenset(warning_acknowledgements)
        if set(warning_fingerprints).difference(acknowledgements):
            raise WorkspaceError(
                "Acknowledge every current validation warning before "
                "confirming"
            )
        existing = self.mappings.get_mapping_submission(
            workspace_id,
            revision.version,
        )
        if (
            existing is not None
            and existing.mapping_content_hash
            == revision.definition.content_hash
            and existing.validation_hash == validation.validation_hash
            and existing.warning_acknowledgements == warning_fingerprints
        ):
            return existing
        submission = MappingSubmission(
            submission_id=str(uuid4()),
            mapping_id=revision.mapping_id,
            version=revision.version,
            mapping_content_hash=revision.definition.content_hash,
            validation_hash=validation.validation_hash,
            warning_acknowledgements=warning_fingerprints,
            submitted_at=datetime.now(timezone.utc),
            submitted_by=actor.identity.display_name,
        )
        self.mappings.save_mapping_submission(
            workspace_id,
            submission,
            actor=actor,
        )
        return submission

    def validate_current(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> MappingValidationResult:
        """Revalidate the current exact revision against current evidence."""

        self.authorization.require(
            actor,
            Capability.MAPPING_EDIT,
            workspace_id=workspace_id,
        )
        revision = self.mappings.get_mapping_revision(workspace_id)
        selection = self.sources.get_mapping_source_selection(workspace_id)
        schema = self.schemas.get_odoo_schema_catalog(workspace_id)
        governance = self.schemas.get_schema_governance(workspace_id)
        if revision is None or selection is None or schema is None:
            raise WorkspaceError(
                "Save a mapping revision before validating"
            )
        validation = self._validate_definition(
            workspace_id,
            revision.definition,
            selection,
            schema,
            governance,
        )
        self.mappings.save_mapping_validation(
            workspace_id,
            revision.version,
            validation,
            actor=actor,
        )
        return validation

    def _validate_definition(
        self,
        workspace_id: str,
        definition: MappingDefinition,
        selection: SourceSelection,
        schema: OdooSchemaCatalog,
        governance: SchemaGovernance | None,
    ) -> MappingValidationResult:
        """Combine pure semantic validation with workspace-local scan evidence."""

        validation = self.validator.validate(
            definition,
            selection,
            schema,
            governance,
            self._current_supporting_references(
                workspace_id,
                definition,
                schema,
            ),
        )
        collected = self.categorical_coverage.collect(
            workspace_id,
            definition,
            selection,
            schema,
        )
        issues = tuple(
            sorted(
                (*validation.issues, *collected.issues),
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
            if any(item.severity == "error" for item in issues)
            else (
                MappingValidationStatus.VALID_WITH_WARNINGS
                if issues
                else MappingValidationStatus.VALID
            )
        )
        return replace(
            validation,
            status=status,
            issues=issues,
            categorical_coverage=collected.evidence,
        )

    def _current_supporting_references(
        self,
        workspace_id: str,
        definition: MappingDefinition,
        schema: OdooSchemaCatalog,
    ) -> tuple[SupportingLookupSnapshot, ...]:
        """Load each distinct related-model lookup once for semantic checks."""

        if self.supporting_lookups is None:
            return ()
        primary_models = {item.name for item in schema.models}
        lookup_keys: set[str] = set()
        resolvers = []
        for dataset in definition.datasets:
            resolvers.extend(
                component.resolver
                for component in (
                    *dataset.target_identity,
                    *dataset.target_scope,
                )
                if component.resolver is not None
            )
            resolvers.extend(
                relationship.resolver
                for relationship in dataset.relationships
            )
        for resolver in resolvers:
            if (
                resolver.origin
                not in {
                    ResolverOrigin.TARGET_CATALOG,
                    ResolverOrigin.TARGET_THEN_DATASET,
                }
                or not resolver.model
                or resolver.model in primary_models
            ):
                continue
            key_fields = tuple(
                item.target_field for item in resolver.key_mappings
            )
            scope_fields = tuple(
                item.target_field for item in resolver.scope_mappings
            )
            if not key_fields:
                continue
            standard = standard_reference_key(resolver.model)
            display_field = (
                standard.display_field
                if standard is not None
                and standard.key_fields == key_fields
                and standard.scope_fields == scope_fields
                else key_fields[0]
            )
            lookup_key = supporting_lookup_key(
                relation_model=resolver.model,
                key_fields=key_fields,
                scope_fields=scope_fields,
                display_field=display_field,
            )
            lookup_keys.add(lookup_key)

        snapshots = []
        for lookup_key in sorted(lookup_keys):
            snapshot = self.supporting_lookups.get_current(
                workspace_id,
                lookup_key,
            )
            if (
                snapshot is not None
                and snapshot.target_hash == schema.connection_target_hash
                and snapshot.read_credential_binding_hash
                == schema.read_credential_binding_hash
                and snapshot.read_principal_hash == schema.read_principal_hash
                and snapshot.read_context_hash == schema.read_context_hash
                and snapshot.reference_policy_hash == REFERENCE_POLICY_HASH
            ):
                snapshots.append(snapshot)
        return tuple(snapshots)
