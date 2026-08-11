"""Orchestrate Stage D mapping drafts, validation, revisions, and submission.

Layer: application service.

The mapping router supplies complete dataset-centric definitions.
``MappingWorkspaceService`` binds them to the effective source selection and
captured/governed schema, delegates deterministic meaning checks to
``MappingSemanticValidator``, and persists through focused repository ports.
Working drafts are recoverable but unchecked; revisions and submissions are
immutable evidence.

See ``docs/architecture/python-code-map.md``,
``docs/contracts/02-workspace.md``, and ``tests/test_workspace.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Protocol
from uuid import uuid4

from ..access import Actor, AuthorizationPolicy, Capability
from ..domain.schema.governance import SchemaGovernance
from ..domain.mapping.contracts import (
    DatasetMapping,
    MappingDefinition,
)
from ..domain.mapping.artifacts import (
    MappingRevision,
    MappingSubmission,
)
from ..domain.mapping.canonicalization import canonicalize_mapping_definition
from ..domain.mapping.validation.evidence import (
    MappingValidationResult,
    MappingValidationStatus,
    mapping_issue_fingerprint,
)
from ..domain.mapping.validation.validator import MappingSemanticValidator
from ..domain.staging.transformation_impact import TransformationRuleReview
from ..workspace_contracts import (
    MappingWorkingDraft,
    OdooSchemaCatalog,
    SchemaOrigin,
    SourceSelection,
)
from ..workspace_errors import WorkspaceError


class MappingSourceRepository(Protocol):
    """Provide the physical plus derived dataset selection visible to mapping."""

    def get_mapping_source_selection(
        self,
        project_id: str,
    ) -> SourceSelection | None:
        """Return the effective physical-plus-derived selection visible to mapping."""
        ...


class MappingSchemaRepository(Protocol):
    """Provide the captured schema and optional exact key governance."""

    def get_odoo_schema_catalog(
        self,
        project_id: str,
    ) -> OdooSchemaCatalog | None:
        """Return the current target-bound detailed schema."""
        ...

    def get_schema_governance(
        self,
        project_id: str,
    ) -> SchemaGovernance | None:
        """Return current confirmed business-key governance, when available."""
        ...


class MappingWorkspaceRepository(Protocol):
    """Persist recoverable drafts and immutable mapping evidence."""

    def get_mapping_working_draft(
        self,
        project_id: str,
    ) -> MappingWorkingDraft | None:
        """Return recoverable unchecked editor state, if one exists."""
        ...
    def save_mapping_working_draft(
        self,
        project_id: str,
        draft: MappingWorkingDraft,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> None:
        """Replace editor state only at the expected optimistic draft version."""
        ...

    def get_mapping_revision(
        self,
        project_id: str,
        version: int | None = None,
    ) -> MappingRevision | None:
        """Return the current or requested immutable mapping revision."""
        ...

    def get_mapping_validation(
        self,
        project_id: str,
        version: int,
    ) -> MappingValidationResult | None:
        """Return the stored validation for one checked mapping revision."""
        ...

    def get_mapping_submission(
        self,
        project_id: str,
        version: int | None = None,
    ) -> MappingSubmission | None:
        """Return the newest submission globally or for one revision."""
        ...

    def list_mapping_revisions(
        self,
        project_id: str,
    ) -> tuple[MappingRevision, ...]:
        """Return the complete immutable revision history in version order."""
        ...

    def save_mapping_revision(
        self,
        project_id: str,
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
        project_id: str,
        version: int,
        validation: MappingValidationResult,
        *,
        actor: Actor,
    ) -> None:
        """Append deterministic revalidation for one exact stored revision."""
        ...

    def save_mapping_submission(
        self,
        project_id: str,
        submission: MappingSubmission,
        *,
        actor: Actor,
    ) -> None:
        """Append exact submission evidence after repository-side gate checks."""
        ...


class MappingTransformationImpactRepository(Protocol):
    """Read separate full-row rule-review evidence for submission gates."""

    def get_transformation_rule_review(
        self,
        project_id: str,
        *,
        mapping_content_hash: str,
        source_selection_hash: str,
        schema_hash: str,
    ) -> TransformationRuleReview | None: ...


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
        transformation_impacts: MappingTransformationImpactRepository | None = None,
    ) -> None:
        self.sources = sources
        self.schemas = schemas
        self.mappings = mappings
        self.authorization = authorization
        self.transformation_impacts = transformation_impacts
        self.validator = MappingSemanticValidator()

    def save_working_draft(
        self,
        project_id: str,
        *,
        datasets: Iterable[DatasetMapping],
        expected_version: int | None,
        actor: Actor,
    ) -> MappingWorkingDraft:
        """Persist incomplete browser work without semantic validation."""

        self.authorization.require(
            actor,
            Capability.MAPPING_EDIT,
            project_id=project_id,
        )
        selection = self.sources.get_mapping_source_selection(project_id)
        schema = self.schemas.get_odoo_schema_catalog(project_id)
        governance = self.schemas.get_schema_governance(project_id)
        if selection is None or schema is None:
            raise WorkspaceError(
                "Freeze datasets and capture Odoo schema first"
            )
        current = self.mappings.get_mapping_revision(project_id)
        existing = self.mappings.get_mapping_working_draft(project_id)
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
            project_id=project_id,
            base_mapping_version=current.version if current else None,
            definition=definition,
            updated_at=datetime.now(timezone.utc),
            updated_by=actor.identity.display_name,
        )
        self.mappings.save_mapping_working_draft(
            project_id,
            draft,
            expected_version=expected_version,
            actor=actor,
        )
        return draft

    def check_definition(
        self,
        project_id: str,
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
            project_id=project_id,
        )
        selection = self.sources.get_mapping_source_selection(project_id)
        schema = self.schemas.get_odoo_schema_catalog(project_id)
        governance = self.schemas.get_schema_governance(project_id)
        if selection is None or schema is None:
            raise WorkspaceError(
                "Freeze datasets and capture Odoo schema first"
            )
        current = self.mappings.get_mapping_revision(project_id)
        actual_parent = current.version if current else None
        if expected_parent_version != actual_parent:
            raise WorkspaceError(
                "The mapping was modified by another request; reload it"
            )
        working_draft = self.mappings.get_mapping_working_draft(project_id)
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
        validation = self.validator.validate(
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
                    project_id,
                    datasets=definition.datasets,
                    expected_version=working_draft.version,
                    actor=actor,
                )
            self.mappings.save_mapping_validation(
                project_id,
                current.version,
                validation,
                actor=actor,
            )
            return current, validation
        historical_versions = self.mappings.list_mapping_revisions(project_id)
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
            project_id=project_id,
            base_mapping_version=revision.version,
            definition=definition,
            updated_at=revision.created_at,
            updated_by=actor.identity.display_name,
        )
        self.mappings.save_mapping_revision(
            project_id,
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
        project_id: str,
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
            project_id=project_id,
        )
        selection = self.sources.get_mapping_source_selection(project_id)
        schema = self.schemas.get_odoo_schema_catalog(project_id)
        governance = self.schemas.get_schema_governance(project_id)
        revision = self.mappings.get_mapping_revision(project_id)
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
        working_draft = self.mappings.get_mapping_working_draft(project_id)
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
            project_id,
            revision.version,
        )
        if (
            validation is None
            or validation.mapping_content_hash
            != revision.definition.content_hash
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
        text_cleanup_configured = any(
            field.transform.configured_text_steps
            for dataset in revision.definition.datasets
            for field in dataset.fields
        )
        if text_cleanup_configured and self.transformation_impacts is not None:
            review = self.transformation_impacts.get_transformation_rule_review(
                project_id,
                mapping_content_hash=revision.definition.content_hash,
                source_selection_hash=revision.definition.source_selection_hash,
                schema_hash=revision.definition.schema_hash,
            )
            if review is None:
                raise WorkspaceError(
                    "Preview the current rule effects before confirming field matches"
                )
            if review.unacknowledged_rule_impacts:
                raise WorkspaceError(
                    "Review every cleanup step that changed no values before confirming"
                )
        existing = self.mappings.get_mapping_submission(
            project_id,
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
            project_id,
            submission,
            actor=actor,
        )
        return submission

    def validate_current(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> MappingValidationResult:
        """Revalidate the current exact revision against current evidence."""

        self.authorization.require(
            actor,
            Capability.MAPPING_EDIT,
            project_id=project_id,
        )
        revision = self.mappings.get_mapping_revision(project_id)
        selection = self.sources.get_mapping_source_selection(project_id)
        schema = self.schemas.get_odoo_schema_catalog(project_id)
        governance = self.schemas.get_schema_governance(project_id)
        if revision is None or selection is None or schema is None:
            raise WorkspaceError(
                "Save a mapping revision before validating"
            )
        validation = self.validator.validate(
            revision.definition,
            selection,
            schema,
            governance,
        )
        self.mappings.save_mapping_validation(
            project_id,
            revision.version,
            validation,
            actor=actor,
        )
        return validation
