"""Persist exact Project Production setup bindings and safe projections."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from impodo.domain.shared.access import Actor
from impodo.domain.project.foundation import (
    FaultInjector,
    MigrationConflictError,
    MigrationNotFoundError,
    MigrationOperationKind,
    MigrationOperationState,
    require_hash,
    require_uuid,
)
from impodo.domain.run.production import ProductionRunBinding
from .migration_foundation_repository import MigrationFoundationRepository


class ProductionRunRepository:
    """Own restart-safe Production setup and bounded run lookups."""

    def __init__(self, foundation: MigrationFoundationRepository) -> None:
        self.foundation = foundation
        self.database = foundation.database
        self.registry_path = foundation.registry_path

    def bind_setup(
        self,
        binding: ProductionRunBinding,
        *,
        expected_workspace_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> ProductionRunBinding:
        """Pin a setup-only Production run to the current selected plan."""

        operation_id = require_uuid(operation_id, "operation_id")
        intent = self.foundation._reserve_intent(
            operation_id=operation_id,
            project_id=binding.project_id,
            owner_kind="MIGRATION_RUN",
            owner_id=binding.migration_run_id,
            kind=MigrationOperationKind.PRODUCTION_RUN_SETUP,
            request_hash=require_hash(request_hash, "request_hash"),
            expected_revision=expected_workspace_revision,
            detail={"binding": binding.to_dict()},
            actor=actor,
        )
        stored = self._from_dict(dict(intent.detail["binding"]))
        if intent.state is MigrationOperationState.COMMITTED:
            return self.get(stored.migration_run_id)
        self.foundation._fault(fault, "INTENT_RESERVED")
        with self.database.connect(self.registry_path) as connection:
            connection.begin()
            try:
                existing = connection.execute(
                    "SELECT content_hash FROM production_run_binding "
                    "WHERE migration_run_id = ?",
                    [stored.migration_run_id],
                ).fetchone()
                if existing is None:
                    self.foundation._assert_workspace_revision(
                        connection,
                        stored.project_id,
                        expected_workspace_revision,
                    )
                    self._validate_setup(connection, stored)
                    self.foundation._assert_identity_available(
                        connection,
                        stored.production_run_binding_id,
                    )
                    connection.execute(
                        "UPDATE migration_run SET cutover_selection_id = ?, "
                        "optimistic_revision = optimistic_revision + 1, "
                        "updated_at = ? WHERE migration_run_id = ?",
                        [
                            stored.cutover_selection_id,
                            stored.created_at.isoformat(),
                            stored.migration_run_id,
                        ],
                    )
                    connection.execute(
                        "INSERT INTO migration_run_cutover_plan VALUES "
                        "(?, ?, ?, ?, ?)",
                        [
                            stored.migration_run_id,
                            stored.cutover_plan_id,
                            stored.cutover_plan_revision,
                            stored.plan_content_hash,
                            stored.created_at.isoformat(),
                        ],
                    )
                    connection.execute(
                        "INSERT INTO production_run_binding VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                        "?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        self._values(stored),
                    )
                    project_revision = self.foundation._advance_project(
                        connection,
                        stored.project_id,
                        expected_workspace_revision,
                        stored.created_at,
                    )
                    self.foundation._insert_event(
                        connection,
                        project_id=stored.project_id,
                        aggregate_kind="MIGRATION_RUN",
                        aggregate_id=stored.migration_run_id,
                        aggregate_revision=2,
                        event_type="PRODUCTION_RUN_SETUP_CREATED",
                        detail={
                            "cutover_plan_id": stored.cutover_plan_id,
                            "cutover_plan_revision": stored.cutover_plan_revision,
                            "cutover_selection_id": stored.cutover_selection_id,
                            "data_version_id": stored.data_version_id,
                            "project_revision": project_revision,
                            "setup_workspace_id": stored.setup_workspace_id,
                        },
                        actor=actor,
                        occurred_at=stored.created_at,
                    )
                elif str(existing[0]) != stored.content_hash:
                    raise MigrationConflictError(
                        "Production run setup identity already has different meaning"
                    )
                self.foundation._set_pending_stage(
                    connection,
                    operation_id,
                    "PRODUCTION_SETUP_BOUND",
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self.foundation._fault(fault, "REGISTRY_COMMITTED")
        self.foundation._finish_pending_intent(
            operation_id,
            stage="COMMITTED",
            result={"migration_run_id": stored.migration_run_id},
        )
        return self.get(stored.migration_run_id)

    def get(self, migration_run_id: str) -> ProductionRunBinding:
        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        with self.database.connect(self.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                "SELECT * FROM production_run_binding WHERE migration_run_id = ?",
                [migration_run_id],
            )
        if not rows:
            raise MigrationNotFoundError("Production run binding not found")
        return self._from_row(rows[0])

    def for_workspace(self, workspace_id: str) -> ProductionRunBinding | None:
        """Resolve one application or setup workspace with one registry query."""

        workspace_id = require_uuid(workspace_id, "workspace_id")
        with self.database.connect(self.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                "SELECT binding.* FROM migration_workspace workspace "
                "JOIN production_run_binding binding ON "
                "binding.migration_run_id = workspace.migration_run_id "
                "WHERE workspace.workspace_id = ?",
                [workspace_id],
            )
        return self._from_row(rows[0]) if rows else None

    def list_for_project(self, project_id: str) -> tuple[ProductionRunBinding, ...]:
        project_id = require_uuid(project_id, "project_id")
        with self.database.connect(self.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                "SELECT * FROM production_run_binding WHERE project_id = ? "
                "ORDER BY created_at DESC, migration_run_id DESC",
                [project_id],
            )
        return tuple(self._from_row(row) for row in rows)

    @staticmethod
    def _validate_setup(connection, binding: ProductionRunBinding) -> None:
        run = connection.execute(
            "SELECT project_id, data_version_id, purpose, state, "
            "target_binding_id, cutover_selection_id FROM migration_run "
            "WHERE migration_run_id = ?",
            [binding.migration_run_id],
        ).fetchone()
        if run != (
            binding.project_id,
            binding.data_version_id,
            "PRODUCTION",
            "DRAFT",
            None,
            None,
        ):
            raise MigrationConflictError(
                "Production setup does not match its fresh draft run"
            )
        data_version = connection.execute(
            "SELECT project_id, purpose, state FROM data_version "
            "WHERE data_version_id = ?",
            [binding.data_version_id],
        ).fetchone()
        if data_version != (binding.project_id, "PRODUCTION", "DRAFT"):
            raise MigrationConflictError(
                "Production setup requires one fresh draft Production DataVersion"
            )
        workspace = connection.execute(
            "SELECT project_id, data_version_id, migration_run_id, "
            "recipe_application_id FROM migration_workspace "
            "WHERE workspace_id = ?",
            [binding.setup_workspace_id],
        ).fetchone()
        if workspace != (
            binding.project_id,
            binding.data_version_id,
            binding.migration_run_id,
            None,
        ):
            raise MigrationConflictError(
                "Production setup workspace does not match its run"
            )
        selection = connection.execute(
            "SELECT project_id, cutover_plan_id, cutover_plan_revision, "
            "qualification_id FROM project_cutover_selection "
            "WHERE cutover_selection_id = ?",
            [binding.cutover_selection_id],
        ).fetchone()
        if selection != (
            binding.project_id,
            binding.cutover_plan_id,
            binding.cutover_plan_revision,
            binding.qualification_id,
        ):
            raise MigrationConflictError(
                "Production setup does not match the selected qualification"
            )
        qualification = connection.execute(
            "SELECT plan_content_hash, target_binding_hash, status FROM "
            "cutover_plan_qualification WHERE qualification_id = ?",
            [binding.qualification_id],
        ).fetchone()
        if qualification != (
            binding.plan_content_hash,
            binding.test_target_binding_hash,
            "TEST_QUALIFIED",
        ):
            raise MigrationConflictError(
                "Production setup does not match qualified Test evidence"
            )
        plan = connection.execute(
            "SELECT current_revision FROM cutover_plan WHERE cutover_plan_id = ?",
            [binding.cutover_plan_id],
        ).fetchone()
        if plan != (binding.cutover_plan_revision,):
            raise MigrationConflictError(
                "The selected CutoverPlan changed before Production setup"
            )

    @staticmethod
    def _values(binding: ProductionRunBinding) -> list[object]:
        return [
            binding.production_run_binding_id,
            binding.project_id,
            binding.migration_run_id,
            binding.data_version_id,
            binding.setup_workspace_id,
            binding.cutover_selection_id,
            binding.qualification_id,
            binding.cutover_plan_id,
            binding.cutover_plan_revision,
            binding.plan_content_hash,
            binding.test_target_binding_hash,
            binding.state.value,
            binding.target_binding_id,
            binding.read_credential_generation,
            binding.write_credential_generation,
            binding.write_principal_hash,
            binding.write_permission_hash,
            binding.write_context_hash,
            binding.parameter_values_hash,
            binding.control_values_hash,
            binding.activation_evidence_hash,
            binding.content_hash,
            binding.created_at.isoformat(),
            binding.activated_at.isoformat() if binding.activated_at else None,
            binding.contract_version,
        ]

    @classmethod
    def _from_row(cls, value: Mapping[str, object]) -> ProductionRunBinding:
        return ProductionRunBinding(
            production_run_binding_id=str(value["production_run_binding_id"]),
            project_id=str(value["project_id"]),
            migration_run_id=str(value["migration_run_id"]),
            data_version_id=str(value["data_version_id"]),
            setup_workspace_id=str(value["setup_workspace_id"]),
            cutover_selection_id=str(value["cutover_selection_id"]),
            qualification_id=str(value["qualification_id"]),
            cutover_plan_id=str(value["cutover_plan_id"]),
            cutover_plan_revision=int(value["cutover_plan_revision"]),
            plan_content_hash=str(value["plan_content_hash"]),
            test_target_binding_hash=str(value["test_target_binding_hash"]),
            state=str(value["state"]),
            target_binding_id=(
                str(value["target_binding_id"])
                if value.get("target_binding_id")
                else None
            ),
            read_credential_generation=(
                str(value["read_credential_generation"])
                if value.get("read_credential_generation")
                else None
            ),
            write_credential_generation=(
                str(value["write_credential_generation"])
                if value.get("write_credential_generation")
                else None
            ),
            write_principal_hash=cls._optional(value, "write_principal_hash"),
            write_permission_hash=cls._optional(value, "write_permission_hash"),
            write_context_hash=cls._optional(value, "write_context_hash"),
            parameter_values_hash=cls._optional(value, "parameter_values_hash"),
            control_values_hash=cls._optional(value, "control_values_hash"),
            activation_evidence_hash=cls._optional(
                value,
                "activation_evidence_hash",
            ),
            created_at=datetime.fromisoformat(str(value["created_at"])),
            activated_at=(
                datetime.fromisoformat(str(value["activated_at"]))
                if value.get("activated_at")
                else None
            ),
            contract_version=int(value["contract_version"]),
        )

    @classmethod
    def _from_dict(cls, value: Mapping[str, object]) -> ProductionRunBinding:
        return ProductionRunBinding(
            production_run_binding_id=str(value["production_run_binding_id"]),
            project_id=str(value["project_id"]),
            migration_run_id=str(value["migration_run_id"]),
            data_version_id=str(value["data_version_id"]),
            setup_workspace_id=str(value["setup_workspace_id"]),
            cutover_selection_id=str(value["cutover_selection_id"]),
            qualification_id=str(value["qualification_id"]),
            cutover_plan_id=str(value["cutover_plan_id"]),
            cutover_plan_revision=int(value["cutover_plan_revision"]),
            plan_content_hash=str(value["plan_content_hash"]),
            test_target_binding_hash=str(value["test_target_binding_hash"]),
            state=str(value["state"]),
            target_binding_id=cls._optional(value, "target_binding_id"),
            read_credential_generation=cls._optional(
                value,
                "read_credential_generation",
            ),
            write_credential_generation=cls._optional(
                value,
                "write_credential_generation",
            ),
            write_principal_hash=cls._optional(value, "write_principal_hash"),
            write_permission_hash=cls._optional(value, "write_permission_hash"),
            write_context_hash=cls._optional(value, "write_context_hash"),
            parameter_values_hash=cls._optional(value, "parameter_values_hash"),
            control_values_hash=cls._optional(value, "control_values_hash"),
            activation_evidence_hash=cls._optional(
                value,
                "activation_evidence_hash",
            ),
            created_at=datetime.fromisoformat(str(value["created_at"])),
            activated_at=(
                datetime.fromisoformat(str(value["activated_at"]))
                if value.get("activated_at")
                else None
            ),
            contract_version=int(value["contract_version"]),
        )

    @staticmethod
    def _optional(value: Mapping[str, object], key: str) -> str | None:
        item = value.get(key)
        return str(item) if item is not None and str(item) else None
