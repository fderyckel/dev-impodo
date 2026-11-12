"""Keep reusable Odoo capture choices separate from immutable saved data.

This service owns project authorization, configuration revisions, complete
version publication, and fresh restore admission. Remote capture runs between
bounded local phases; it never borrows an old execution journal.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4, uuid5

from impodo.application.odoo_capture_publication_service import (
    OdooCapturePublicationService,
)
from impodo.application.odoo_provenance_service import OdooProvenanceService
from impodo.application.odoo_saved_data_ports import SavedOdooDataRepository
from impodo.application.odoo_source_capture_service import OdooSourceCapturePort
from impodo.application.schema_workspace_service import SchemaWorkspaceService
from impodo.application.shared.local_operations import (
    LocalOperation,
    direct_local_operation,
)
from impodo.application.source_workspace_service import SourceWorkspaceService
from impodo.application.workspace.access import WorkspaceAccessService
from impodo.application.workspace_data_version_source_service import (
    WorkspaceDataVersionSourceService,
)
from impodo.domain.odoo.compatibility import OdooOperation, assess_odoo_operation
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo.contracts import MetadataSnapshot
from impodo.domain.odoo_capture import OdooCaptureSelection
from impodo.domain.odoo_relationship_scope import OdooRelationshipScope
from impodo.domain.odoo_saved_data import (
    SavedOdooDataDefinition,
    SavedOdooDataVersion,
    SavedOdooRestoreBinding,
)
from impodo.domain.odoo_source_capture import is_odoo_capture_value_field
from impodo.domain.project.foundation import MigrationConflictError, require_uuid
from impodo.domain.serialization import canonical_json
from impodo.domain.shared.access import Actor, AuthorizationPolicy, Capability
from impodo.domain.shared.models import OdooReadIdentity
from impodo.domain.workspace.contracts import OdooSchemaCatalog, SourceSelection
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceState,
    WorkspaceStateService,
)


class SavedOdooEvidenceReader(Protocol):
    """Read the immutable evidence referenced by a saved version."""

    def get_source_selection(self, workspace_id: str) -> SourceSelection | None: ...

    def get_odoo_schema_catalog(
        self, workspace_id: str
    ) -> OdooSchemaCatalog | None: ...

    def get_current_odoo_capture_selections(
        self, workspace_id: str
    ) -> tuple[OdooCaptureSelection, ...]: ...


class SavedOdooWorkspaceFactory(Protocol):
    """Provision independent capture or restore workspaces."""

    def prepare_refresh(
        self, source_workspace_id: str, *, actor: Actor, operation_id: str
    ) -> WorkspaceState: ...

    def prepare_restore(
        self,
        source_workspace_id: str,
        *,
        actor: Actor,
        operation_id: str,
        expected_source_selection_hash: str | None = None,
        expected_manifest_hashes: tuple[tuple[str, str], ...] = (),
    ) -> WorkspaceState: ...


@dataclass(frozen=True, slots=True)
class SavedOdooCaptureAccess:
    """Hold one transient authenticated source read; never persist credentials."""

    metadata: MetadataSnapshot
    read_identity: OdooReadIdentity | None
    credential_binding_hash: str
    gateway: OdooSourceCapturePort


SavedOdooAccessFactory = Callable[[WorkspaceState, str], SavedOdooCaptureAccess]


class SavedOdooSourceCompanyWriter(Protocol):
    """Persist the source context independently from destination choices."""

    def set_source_company(self, workspace_id: str, selection: OdooCompanySelection, *, actor: Actor, expected_revision: int) -> None: ...


class SavedOdooDataService:
    """Authorize the two-level saved-data lifecycle without duplicating rows."""

    def __init__(
        self,
        *,
        repository: SavedOdooDataRepository,
        access: WorkspaceAccessService,
        authorization: AuthorizationPolicy,
        states: WorkspaceStateService,
        evidence: SavedOdooEvidenceReader,
        workspaces: SavedOdooWorkspaceFactory,
        sources: SourceWorkspaceService,
        schemas: SchemaWorkspaceService,
        provenance: OdooProvenanceService,
        publication: OdooCapturePublicationService,
        projections: WorkspaceDataVersionSourceService,
        source_companies: SavedOdooSourceCompanyWriter,
        local_operation: LocalOperation = direct_local_operation,
    ) -> None:
        self.repository = repository
        self.access = access
        self.authorization = authorization
        self.states = states
        self.evidence = evidence
        self.workspaces = workspaces
        self.sources = sources
        self.schemas = schemas
        self.provenance = provenance
        self.publication = publication
        self.projections = projections
        self.source_companies = source_companies
        self.local_operation = local_operation

    def configure(
        self, workspace_id: str, *, actor: Actor, label: str,
        source_company: OdooCompanySelection, all_fields: bool,
        operation_id: str, access_factory: SavedOdooAccessFactory,
    ) -> SavedOdooDataDefinition:
        """Check capture choices in the explicit company without downloading rows."""

        require_uuid(operation_id, "operation_id")
        def prepare():
            owner = self.access.require(actor, Capability.SOURCE_CONFIGURE, workspace_id=workspace_id)
            state, schema, selections = self._configuration(workspace_id)
            scope = self.states.repository.get_current_odoo_relationship_scope(workspace_id)
            if self.evidence.get_source_selection(workspace_id) is not None:
                fresh = self.workspaces.prepare_refresh(workspace_id, actor=actor, operation_id=str(uuid5(UUID(operation_id), "configuration-workspace")))
            else:
                fresh = state
            self.source_companies.set_source_company(fresh.workspace_id, source_company, actor=actor, expected_revision=fresh.revision)
            fresh = self.states.repository.get(fresh.workspace_id)
            now = datetime.now(UTC)
            major = assess_odoo_operation(schema.odoo_version, OdooOperation.CAPTURE_SOURCE).version.major
            if major not in {19, 20}:
                raise WorkspaceError("Choose a supported Odoo source")
            candidate = SavedOdooDataDefinition(
                saved_data_id=str(uuid5(UUID(operation_id), "saved-data")), project_id=owner.project_id,
                label=label, revision=1, source_workspace_id=fresh.workspace_id,
                source_target_hash=schema.connection_target_hash, source_company_hash=source_company.binding_hash,
                odoo_major=major, config_json=canonical_json({"all_fields": all_fields}), latest_version=None,
                created_at=now, updated_at=now,
            )
            return candidate, fresh, selections, scope
        candidate, fresh, selections, scope = self.local_operation(prepare)
        live = access_factory(fresh, workspace_id)
        self.local_operation(lambda: self._seed_refresh(candidate, fresh, selections, scope, live, actor))
        return self.local_operation(lambda: self.create(fresh.workspace_id, actor=actor, label=label, all_fields=all_fields, saved_data_id=candidate.saved_data_id))

    def create(
        self,
        workspace_id: str,
        *,
        actor: Actor,
        label: str,
        all_fields: bool = False,
        saved_data_id: str | None = None,
    ) -> SavedOdooDataDefinition:
        """Save reviewed source choices before or after their first capture."""

        owner = self.access.require(
            actor, Capability.SOURCE_CONFIGURE, workspace_id=workspace_id
        )
        state, schema, selections = self._configuration(workspace_id)
        major = assess_odoo_operation(
            schema.odoo_version, OdooOperation.CAPTURE_SOURCE
        ).version.major
        if major not in {19, 20}:
            raise WorkspaceError("Choose a supported Odoo 19 or Odoo 20 source")
        if state.odoo_company is None:
            raise WorkspaceError("Choose the source company before saving Odoo data")
        now = datetime.now(UTC)
        definition = SavedOdooDataDefinition(
            saved_data_id=saved_data_id or str(uuid4()),
            project_id=owner.project_id,
            label=label,
            revision=1,
            source_workspace_id=workspace_id,
            source_target_hash=schema.connection_target_hash,
            source_company_hash=state.odoo_company.binding_hash,
            odoo_major=major,
            config_json=self._config_json(schema, selections, all_fields=all_fields),
            latest_version=None,
            created_at=now,
            updated_at=now,
        )
        return self.repository.create_definition(definition)

    def revise(
        self,
        project_id: str,
        saved_data_id: str,
        workspace_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        label: str,
        all_fields: bool,
    ) -> SavedOdooDataDefinition:
        """Append capture meaning without modifying any saved data version."""

        current = self.get(project_id, saved_data_id, actor=actor)
        owner = self.access.require(
            actor, Capability.SOURCE_CONFIGURE, workspace_id=workspace_id
        )
        if owner.project_id != current.project_id:
            raise WorkspaceError("The capture configuration belongs to another project")
        state, schema, selections = self._configuration(workspace_id)
        if state.odoo_company is None or (
            state.odoo_company.binding_hash != current.source_company_hash
            or schema.connection_target_hash != current.source_target_hash
        ):
            raise WorkspaceError(
                "Save a separate configuration for another database or company"
            )
        return self.repository.save_definition(
            replace(
                current,
                label=label,
                source_workspace_id=workspace_id,
                config_json=self._config_json(
                    schema, selections, all_fields=all_fields
                ),
                updated_at=datetime.now(UTC),
            ),
            expected_revision=expected_revision,
        )

    def get(
        self, project_id: str, saved_data_id: str, *, actor: Actor
    ) -> SavedOdooDataDefinition:
        """Authorize the project before reading its saved configuration."""

        self.authorization.require(
            actor, Capability.PROJECT_VIEW, project_id=project_id
        )
        return self.repository.get_definition(project_id, saved_data_id)

    def retain_current(
        self,
        project_id: str,
        saved_data_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        operation_id: str,
    ) -> tuple[SavedOdooDataDefinition, SavedOdooDataVersion]:
        """Retain an already complete source capture without reading Odoo again."""

        definition = self.get(project_id, saved_data_id, actor=actor)
        self.access.require(
            actor,
            Capability.SOURCE_CAPTURE,
            workspace_id=definition.source_workspace_id,
        )
        if definition.revision != expected_revision:
            raise MigrationConflictError(
                "The saved configuration changed; reload before continuing"
            )
        return self._append(
            definition, definition.source_workspace_id, operation_id, actor
        )

    def refresh(
        self,
        project_id: str,
        saved_data_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        operation_id: str,
        access_factory: SavedOdooAccessFactory,
        progress: Callable[[int, str], None] | None = None,
    ) -> SavedOdooDataVersion:
        """Recapture all configured models between bounded local phases."""

        require_uuid(operation_id, "operation_id")
        previous = self.local_operation(lambda: self._completed_operation(project_id, saved_data_id, operation_id, actor))
        if previous is not None:
            return previous

        def prepare():
            definition = self.get(project_id, saved_data_id, actor=actor)
            if definition.revision != expected_revision:
                raise MigrationConflictError(
                    "The saved configuration changed; reload before updating"
                )
            self.access.require(
                actor,
                Capability.SOURCE_CAPTURE,
                workspace_id=definition.source_workspace_id,
            )
            state, schema, selections = self._configuration(
                definition.source_workspace_id
            )
            if (
                state.odoo_company is None
                or state.odoo_company.binding_hash != definition.source_company_hash
            ):
                raise WorkspaceError(
                    "The source company changed; review the capture configuration"
                )
            if schema.connection_target_hash != definition.source_target_hash:
                raise WorkspaceError(
                    "The source database changed; save a separate configuration"
                )
            fresh = (
                self.workspaces.prepare_refresh(state.workspace_id, actor=actor, operation_id=str(uuid5(UUID(operation_id), "capture-workspace")))
                if self.evidence.get_source_selection(state.workspace_id) is not None else state
            )
            scope = self.states.repository.get_current_odoo_relationship_scope(
                state.workspace_id
            )
            return definition, fresh, selections, scope

        definition, fresh, selections, scope = self.local_operation(prepare)
        if progress:
            progress(5, "Checking the source database and company")
        live = access_factory(fresh, definition.source_workspace_id)
        self.local_operation(
            lambda: self._seed_refresh(
                definition, fresh, selections, scope, live, actor
            )
        )
        if progress:
            progress(15, "Downloading all configured models and related records")
        publication = self.publication.publish(
            fresh.workspace_id, live.gateway, actor=actor
        )

        def finish():
            self.projections.accept_odoo_capture(
                fresh.workspace_id,
                publication.source_selection,
                publication.source_snapshots,
                publication.manifests,
                actor=actor,
            )
            return self._append(definition, fresh.workspace_id, operation_id, actor)[1]

        version = self.local_operation(finish)
        if progress:
            progress(100, "Saved data updated")
        return version

    def prepare_restore(
        self,
        project_id: str,
        saved_data_id: str,
        version_number: int,
        *,
        actor: Actor,
        operation_id: str,
    ) -> WorkspaceState:
        """Bind one retained version to a fresh run without contacting its source."""

        self.get(project_id, saved_data_id, actor=actor)
        self.authorization.require(
            actor, Capability.EXPORT_PLAN_EXECUTE, project_id=project_id
        )
        version = self.repository.get_version(project_id, saved_data_id, version_number)
        state = self.workspaces.prepare_restore(
            version.workspace_id,
            actor=actor,
            operation_id=operation_id,
            expected_source_selection_hash=version.source_selection_hash,
            expected_manifest_hashes=version.manifest_hashes,
        )
        self.repository.bind_restore(
            SavedOdooRestoreBinding(
                project_id=project_id,
                workspace_id=state.workspace_id,
                saved_data_id=saved_data_id,
                version_number=version_number,
                source_workspace_id=version.workspace_id,
                data_version_id=version.data_version_id,
                generation_id=str(uuid5(UUID(operation_id), "restore-generation")),
                created_at=state.created_at,
            )
        )
        return state

    def _completed_operation(self, project_id: str, saved_data_id: str, operation_id: str, actor: Actor) -> SavedOdooDataVersion | None:
        self.get(project_id, saved_data_id, actor=actor)
        return self.repository.get_version_for_operation(project_id, saved_data_id, operation_id)

    def _configuration(self, workspace_id: str):
        state = self.states.repository.get(workspace_id)
        schema = self.evidence.get_odoo_schema_catalog(workspace_id)
        selections = self.evidence.get_current_odoo_capture_selections(workspace_id)
        if state.source_mode is not SourceMode.ODOO or schema is None or not selections:
            raise WorkspaceError(
                "Choose the Odoo models, fields, and related data before saving the configuration"
            )
        if {item.model for item in selections} != {item.name for item in schema.models}:
            raise WorkspaceError("Save a capture plan for every selected Odoo model")
        return state, schema, selections

    @staticmethod
    def _config_json(
        schema: OdooSchemaCatalog,
        selections: tuple[OdooCaptureSelection, ...],
        *,
        all_fields: bool,
    ) -> str:
        return canonical_json(
            {
                "contract_version": 1,
                "all_fields": all_fields,
                "schema_hash": schema.content_hash,
                "models": [
                    {
                        "model": item.model,
                        "field_names": item.field_names,
                        "selection_hash": item.content_hash,
                    }
                    for item in selections
                ],
            }
        )

    def _append(
        self,
        definition: SavedOdooDataDefinition,
        workspace_id: str,
        operation_id: str,
        actor: Actor,
    ):
        owner = self.access.require(
            actor, Capability.SOURCE_CAPTURE, workspace_id=workspace_id
        )
        selection = self.evidence.get_source_selection(workspace_id)
        manifests = self.provenance.current_manifests(workspace_id, actor=actor)
        if (
            owner.project_id != definition.project_id
            or selection is None
            or not manifests
        ):
            raise WorkspaceError("The complete captured data is not available")
        if {item.dataset_id for item in manifests} != {
            item.dataset_id for item in selection.datasets
        }:
            raise WorkspaceError(
                "The captured models are incomplete; the previous saved version remains available"
            )
        version = SavedOdooDataVersion(
            saved_data_id=definition.saved_data_id,
            project_id=definition.project_id,
            version_number=(definition.latest_version or 0) + 1,
            configuration_revision=definition.configuration_revision,
            workspace_id=workspace_id,
            data_version_id=selection.data_version_id,
            source_selection_hash=selection.content_hash,
            manifest_hashes=tuple(
                sorted((item.model, item.content_hash) for item in manifests)
            ),
            row_counts=tuple(
                sorted((item.model, item.row_count) for item in manifests)
            ),
            captured_at=max(item.capture_finished_at for item in manifests),
            operation_id=operation_id,
        )
        if (
            definition.latest_version is not None
            and sum(count for _, count in version.row_counts) == 0
        ):
            raise WorkspaceError(
                "The refreshed source is empty. Review its database and company before replacing the preferred saved version"
            )
        return self.repository.append_version(
            version, expected_revision=definition.revision
        )

    def _seed_refresh(
        self,
        definition: SavedOdooDataDefinition,
        fresh: WorkspaceState,
        selections: tuple[OdooCaptureSelection, ...],
        scope: OdooRelationshipScope | None,
        live: SavedOdooCaptureAccess,
        actor: Actor,
    ) -> None:
        schema = self.schemas.prepare_capture(
            fresh,
            live.metadata,
            read_credential_binding_hash=live.credential_binding_hash,
            read_identity=live.read_identity,
            actor=actor,
        )
        if (
            assess_odoo_operation(
                schema.odoo_version, OdooOperation.CAPTURE_SOURCE
            ).version.major
            != definition.odoo_major
        ):
            raise WorkspaceError(
                "The source Odoo major changed; save a separate configuration"
            )
        by_model = {model.name: model for model in schema.models}
        owner = self.access.require(
            actor, Capability.SOURCE_SELECT, workspace_id=fresh.workspace_id
        )
        all_fields = json.loads(definition.config_json).get("all_fields") is True
        rebuilt = []
        for old in selections:
            model = by_model.get(old.model)
            if model is None:
                raise WorkspaceError(
                    f"The source model {old.model} is no longer available"
                )
            fields = (
                tuple(
                    sorted(
                        field.name
                        for field in model.fields
                        if field.name not in {"id", "write_date"}
                        and is_odoo_capture_value_field(field)
                    )
                )
                if all_fields
                else old.field_names
            )
            clauses = old.filter_clauses
            if old.protected_filter_artifact_hash is not None:
                if self.sources.capture_filters is None:
                    raise WorkspaceError("The saved source filter is unavailable")
                clauses = self.sources.capture_filters.read(definition.project_id, old)
            selection_id = str(uuid4())
            filter_hash = None
            if clauses and self.sources.capture_filters is not None:
                filter_hash = self.sources.capture_filters.put(
                    definition.project_id,
                    selection_id=selection_id,
                    version=1,
                    data_version_id=owner.data_version_id,
                    clauses=clauses,
                )
            rebuilt.append(
                OdooCaptureSelection.create(
                    selection_id=selection_id,
                    version=1,
                    data_version_id=owner.data_version_id,
                    dataset_name=old.dataset_name,
                    model=old.model,
                    field_names=fields,
                    filter_clauses=() if filter_hash else clauses,
                    protected_filter_artifact_hash=filter_hash,
                    capture_role=old.capture_role,
                    filter_policy=old.filter_policy,
                    max_rows=old.max_rows,
                    page_size=old.page_size,
                    policy_hash=schema.policy_hash,
                    connection_target_hash=schema.connection_target_hash,
                    schema_scope_hash=schema.content_hash,
                    read_principal_hash=schema.read_principal_hash,
                    read_permission_hash=schema.read_permission_hash,
                    context_hash=schema.read_context_hash,
                    created_at=datetime.now(UTC),
                    created_by=actor.identity.display_name,
                )
            )
        self.states.update_schema_scope(
            fresh.workspace_id,
            actor=actor,
            expected_revision=fresh.revision,
            permitted_models=tuple(by_model),
            relationship_decisions=scope.decisions if scope else (),
            relationship_root_models=scope.root_models if scope else tuple(by_model),
            relationship_explicit_actions=scope.explicit_actions_by_model
            if scope
            else (),
            schema_catalog=schema,
            capture_selections=rebuilt,
        )
