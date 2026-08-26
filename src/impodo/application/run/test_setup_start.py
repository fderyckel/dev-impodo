"""Create the durable roots required to begin guided Test setup."""

from __future__ import annotations

from datetime import UTC, datetime

from impodo.access import Actor, Capability
from impodo.data_version_sources import (
    DataVersionSourcePackage,
    SourcePackageOrigin,
    SourcePackageState,
)
from impodo.domain.data_version.models import DataVersionPurpose, DataVersionState
from impodo.domain.serialization import content_hash
from impodo.migration_foundation import (
    MigrationFoundationError,
    require_revision,
    require_uuid,
    required_text,
    utc_now,
)
from impodo.migration_run_planning import RecipeDependency
from impodo.migration_test import TestRunSetupBinding, TestRunSetupState
from impodo.workspace_state import SourceMode, WorkspaceStateNotFoundError
from .fresh_data_values import normalize_export_date


class TestRunSetupStartUseCase:
    """Own the restart-safe creation workflow for one draft Test setup."""

    def __init__(self, service) -> None:
        self._service = service

    def start(
        self,
        project_id: str,
        *,
        expected_workspace_revision: int,
        recipe_revisions: tuple[tuple[str, int], ...],
        dependencies: tuple[RecipeDependency, ...],
        label: str,
        export_as_of: str,
        operation_id: str,
        actor: Actor,
    ):
        """Create one draft Test delivery and its shared setup workspace."""

        project_id = require_uuid(project_id, "project_id")
        operation_id = require_uuid(operation_id, "operation_id")
        expected_workspace_revision = require_revision(
            expected_workspace_revision,
            "expected_workspace_revision",
        )
        clean_label = required_text(label, "label", maximum=200)
        clean_export_as_of = normalize_export_date(export_as_of)
        self._service.authorization.require(
            actor,
            Capability.MIGRATION_RUN_CREATE,
            project_id=project_id,
        )
        selections = self._service._selections(
            project_id,
            recipe_revisions,
            actor=actor,
        )
        replay = self._service._committed_setup(
            project_id,
            operation_id=operation_id,
            label=clean_label,
            export_as_of=clean_export_as_of,
            selections=selections,
            dependencies=dependencies,
            actor=actor,
        )
        if replay is not None:
            return replay

        authoring_versions = tuple(
            item
            for item in self._service.data_versions.list(project_id, actor=actor)
            if item.purpose is DataVersionPurpose.AUTHORING
            and item.state is DataVersionState.FROZEN
        )
        if not authoring_versions:
            raise MigrationFoundationError(
                "Save the Recipe from an accepted Authoring data version first"
            )
        parent = max(authoring_versions, key=lambda item: item.version_number)
        parent_package = self._service.source_packages.repository.get_source_package(
            parent.data_version_id
        )
        if (
            parent_package is None
            or parent_package.origin is not SourcePackageOrigin.FILE
        ):
            raise MigrationFoundationError(
                "Testing with a newer delivery currently requires a file-source Recipe"
            )

        data_version = self._service._data_version(
            project_id,
            expected_workspace_revision=expected_workspace_revision,
            parent_data_version_id=parent.data_version_id,
            label=clean_label,
            export_as_of=clean_export_as_of,
            operation_id=self._service._child_operation(operation_id, "test-data"),
            actor=actor,
        )
        package = self._service.source_packages.repository.get_source_package(
            data_version.data_version_id
        )
        if package is None:
            self._service.source_packages.replace_draft(
                DataVersionSourcePackage(
                    data_version_id=data_version.data_version_id,
                    project_id=project_id,
                    revision=1,
                    origin=SourcePackageOrigin.FILE,
                    state=SourcePackageState.DRAFT,
                    files=(),
                    catalogs=(),
                    configurations=(),
                    datasets=(),
                    updated_at=datetime.now(UTC),
                ),
                actor=actor,
                expected_package_revision=None,
            )
        project = self._service.projects.get(project_id, actor=actor)
        run = self._service._run(
            project_id,
            expected_workspace_revision=project.optimistic_revision,
            data_version_id=data_version.data_version_id,
            label=clean_label,
            operation_id=self._service._child_operation(operation_id, "test-run"),
            actor=actor,
        )
        project = self._service.projects.get(project_id, actor=actor)
        setup_workspace = self._service._workspace(
            project_id,
            expected_workspace_revision=project.optimistic_revision,
            data_version_id=data_version.data_version_id,
            migration_run_id=run.migration_run_id,
            label=f"{clean_label} data and Odoo target setup",
            operation_id=self._service._child_operation(
                operation_id,
                "test-setup-workspace",
            ),
            actor=actor,
        )
        try:
            self._service.workspace_states.repository.get(
                setup_workspace.workspace_id
            )
        except WorkspaceStateNotFoundError:
            project = self._service.projects.get(project_id, actor=actor)
            self._service.workspace_states.provision_migration_workspace(
                setup_workspace.workspace_id,
                actor=actor,
                name=setup_workspace.display_name,
                source_system=project.source_system_identity,
                source_mode=SourceMode.FILE,
                data_classification=project.data_classification.value,
                retention_days=project.retention_days,
            )
        binding = TestRunSetupBinding(
            test_run_setup_id=self._service._child_operation(
                operation_id,
                "test-binding",
            ),
            project_id=project_id,
            migration_run_id=run.migration_run_id,
            data_version_id=data_version.data_version_id,
            setup_workspace_id=setup_workspace.workspace_id,
            selected_revisions=selections,
            dependencies=dependencies,
            state=TestRunSetupState.SETUP,
            target_binding_id=None,
            created_at=utc_now(),
        )
        project = self._service.projects.get(project_id, actor=actor)
        stored = self._service.test_runs.bind_setup(
            binding,
            expected_workspace_revision=project.optimistic_revision,
            operation_id=self._service._child_operation(
                operation_id,
                "bind-test-setup",
            ),
            request_hash=content_hash(
                {
                    "binding": binding.to_dict(),
                    "export_as_of": clean_export_as_of,
                    "label": clean_label,
                }
            ),
            actor=actor,
        )
        return self._service._bundle(stored, actor=actor)
