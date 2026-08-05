"""Application service for governed mapping drafts and revisions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Protocol
from uuid import uuid4

from ..access import Actor, AuthorizationPolicy, Capability
from ..mapping_semantics import (
    DatasetMapping,
    MappingCompiler,
    MappingDefinition,
    MappingRevision,
    MappingSemanticValidator,
    MappingSubmission,
    MappingValidationResult,
    MappingValidationStatus,
    SchemaGovernance,
    mapping_issue_fingerprint,
)
from ..workspace_contracts import (
    MappingWorkingDraft,
    OdooSchemaCatalog,
    SchemaOrigin,
    SourceSelection,
)
from ..workspace_errors import WorkspaceError


class MappingWorkspaceRepository(Protocol):
    def get_mapping_source_selection(
        self,
        project_id: str,
    ) -> SourceSelection | None: ...

    def get_odoo_schema_catalog(
        self,
        project_id: str,
    ) -> OdooSchemaCatalog | None: ...

    def get_schema_governance(
        self,
        project_id: str,
    ) -> SchemaGovernance | None: ...

    def get_mapping_working_draft(
        self,
        project_id: str,
    ) -> MappingWorkingDraft | None: ...

    def save_mapping_working_draft(
        self,
        project_id: str,
        draft: MappingWorkingDraft,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> None: ...

    def get_mapping_revision(
        self,
        project_id: str,
        version: int | None = None,
    ) -> MappingRevision | None: ...

    def list_mapping_revisions(
        self,
        project_id: str,
    ) -> tuple[MappingRevision, ...]: ...

    def save_mapping_revision(
        self,
        project_id: str,
        revision: MappingRevision,
        *,
        validation: MappingValidationResult,
        expected_parent_version: int | None,
        actor: Actor,
    ) -> None: ...

    def save_mapping_validation(
        self,
        project_id: str,
        version: int,
        validation: MappingValidationResult,
        *,
        actor: Actor,
    ) -> None: ...

    def save_mapping_submission(
        self,
        project_id: str,
        submission: MappingSubmission,
        *,
        actor: Actor,
    ) -> None: ...


class MappingWorkspaceService:
    def __init__(
        self,
        repository: MappingWorkspaceRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization
        self.compiler = MappingCompiler()
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
        selection = self.repository.get_mapping_source_selection(project_id)
        schema = self.repository.get_odoo_schema_catalog(project_id)
        governance = self.repository.get_schema_governance(project_id)
        if selection is None or schema is None:
            raise WorkspaceError(
                "Freeze datasets and capture Odoo schema first"
            )
        current = self.repository.get_mapping_revision(project_id)
        existing = self.repository.get_mapping_working_draft(project_id)
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
        definition = MappingDefinition(
            mapping_id=mapping_id,
            source_selection_hash=selection.content_hash,
            schema_hash=(
                governance.content_hash
                if governance is not None
                else schema.content_hash
            ),
            datasets=tuple(datasets),
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
        self.repository.save_mapping_working_draft(
            project_id,
            draft,
            expected_version=expected_version,
            actor=actor,
        )
        return draft

    def save_definition(
        self,
        project_id: str,
        *,
        datasets: Iterable[DatasetMapping],
        expected_parent_version: int | None,
        submit: bool,
        warning_acknowledgements: Iterable[str] = (),
        actor: Actor,
    ) -> tuple[
        MappingRevision,
        MappingValidationResult,
        MappingSubmission | None,
    ]:
        """Save and validate one immutable dataset-centric mapping revision."""

        capability = (
            Capability.MAPPING_SUBMIT if submit else Capability.MAPPING_EDIT
        )
        self.authorization.require(
            actor,
            capability,
            project_id=project_id,
        )
        selection = self.repository.get_mapping_source_selection(project_id)
        schema = self.repository.get_odoo_schema_catalog(project_id)
        governance = self.repository.get_schema_governance(project_id)
        if selection is None or schema is None:
            raise WorkspaceError(
                "Freeze datasets and capture Odoo schema first"
            )
        if submit and schema.origin is SchemaOrigin.LOCAL_MANUAL:
            raise WorkspaceError(
                "Capture the live Odoo schema before submitting a mapping; "
                "the current local schema is unverified"
            )
        current = self.repository.get_mapping_revision(project_id)
        actual_parent = current.version if current else None
        if expected_parent_version != actual_parent:
            raise WorkspaceError(
                "The mapping was modified by another request; reload it"
            )
        working_draft = self.repository.get_mapping_working_draft(project_id)
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
        definition = self.compiler.compile(
            MappingDefinition(
                mapping_id=mapping_id,
                source_selection_hash=selection.content_hash,
                schema_hash=expected_schema_hash,
                datasets=tuple(datasets),
            )
        ).definition
        validation = self.validator.validate(
            definition,
            selection,
            schema,
            governance,
        )
        warning_fingerprints = {
            mapping_issue_fingerprint(item)
            for item in validation.issues
            if item.severity == "warning"
        }
        acknowledgements = frozenset(warning_acknowledgements)
        historical_versions = self.repository.list_mapping_revisions(project_id)
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
        self.repository.save_mapping_revision(
            project_id,
            revision,
            validation=validation,
            expected_parent_version=expected_parent_version,
            actor=actor,
        )
        if submit and validation.status is MappingValidationStatus.INVALID:
            first = next(
                item
                for item in validation.issues
                if item.severity == "error"
            )
            raise WorkspaceError(
                f"Mapping cannot be submitted: {first.message}"
            )
        if submit:
            missing = warning_fingerprints.difference(acknowledgements)
            if missing:
                raise WorkspaceError(
                    "Acknowledge every current validation warning before "
                    "submitting"
                )
        submission = None
        if submit:
            submission = MappingSubmission(
                submission_id=str(uuid4()),
                mapping_id=mapping_id,
                version=revision.version,
                mapping_content_hash=definition.content_hash,
                validation_hash=validation.validation_hash,
                warning_acknowledgements=tuple(
                    sorted(warning_fingerprints)
                ),
                submitted_at=datetime.now(timezone.utc),
                submitted_by=actor.identity.display_name,
            )
            self.repository.save_mapping_submission(
                project_id,
                submission,
                actor=actor,
            )
        return revision, validation, submission

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
        revision = self.repository.get_mapping_revision(project_id)
        selection = self.repository.get_mapping_source_selection(project_id)
        schema = self.repository.get_odoo_schema_catalog(project_id)
        governance = self.repository.get_schema_governance(project_id)
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
        self.repository.save_mapping_validation(
            project_id,
            revision.version,
            validation,
            actor=actor,
        )
        return validation
