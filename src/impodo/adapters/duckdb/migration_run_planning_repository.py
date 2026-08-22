"""Persist one restart-safe integrated run and its isolated applications."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import json
from typing import Mapping

from ...access import Actor
from ...domain.serialization import canonical_json, content_hash
from ...domain.coverage import ReferenceBundle
from ...migration_foundation import (
    FaultInjector,
    MigrationConflictError,
    MigrationNotFoundError,
    MigrationOperationKind,
    MigrationOperationState,
    require_hash,
    require_uuid,
    utc_now,
)
from ...migration_run_planning import (
    IntegratedRunBundle,
    IntegratedRunProgress,
    MigrationRunPlanIssue,
    MigrationRunPlanIssueLevel,
    MigrationRunRequirementPlan,
    OdooModelRequirement,
    PlannedRecipeApplication,
    RecipeApplicationStatus,
    RecipeDependency,
    ReferenceRequirement,
    RecipeRevisionSelection,
    RunRecipeApplication,
    RunTargetBinding,
)
from ...migration_runs import MigrationRun, MigrationRunPurpose
from ...migration_workspaces import MigrationWorkspace
from ...workspace_contracts import OdooSchemaCatalog
from .migration_foundation_repository import MigrationFoundationRepository


class MigrationRunPlanningRepository:
    """Own the bounded M4 registry projections and cross-store recovery."""

    def __init__(self, foundation: MigrationFoundationRepository) -> None:
        self.foundation = foundation
        self.database = foundation.database
        self.registry_path = foundation.registry_path

    def provision_integrated_run(
        self,
        *,
        run: MigrationRun,
        target_binding: RunTargetBinding,
        requirement_plan: MigrationRunRequirementPlan,
        applications: tuple[PlannedRecipeApplication, ...],
        target_schema: OdooSchemaCatalog,
        reference_bundle: ReferenceBundle | None,
        expected_project_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> IntegratedRunBundle:
        """Create one run, target, plan, and all applications atomically."""

        operation_id = require_uuid(operation_id, "operation_id")
        detail = {
            "applications": [self._planned_dict(item) for item in applications],
            "requirement_plan": requirement_plan.to_dict(),
            "run": self.foundation._run_dict(run),
            "target_binding": target_binding.to_dict(),
            "target_schema_json": target_schema.to_json(),
            "reference_bundle": (
                reference_bundle.to_portable_dict()
                if reference_bundle is not None
                else None
            ),
        }
        intent = self.foundation._reserve_intent(
            operation_id=operation_id,
            project_id=run.project_id,
            owner_kind="MIGRATION_RUN",
            owner_id=run.migration_run_id,
            kind=MigrationOperationKind.MIGRATION_RUN_PLAN,
            request_hash=require_hash(request_hash, "request_hash"),
            expected_revision=expected_project_revision,
            detail=detail,
            actor=actor,
        )
        stored = self._stored_plan(intent.detail)
        if intent.state is MigrationOperationState.COMMITTED:
            return self.get_bundle(intent.owner_id)

        self.foundation._fault(fault, "INTENT_RESERVED")
        self._insert_registry(
            run=stored[0],
            target_binding=stored[1],
            requirement_plan=stored[2],
            applications=stored[3],
            target_schema=stored[4],
            reference_bundle=stored[5],
            expected_project_revision=int(intent.expected_revision or 0),
            operation_id=operation_id,
            actor=actor,
        )
        self.foundation._fault(fault, "REGISTRY_COMMITTED")
        for item in stored[3]:
            self.database.create_workspace_store(item.workspace)
        self.foundation._fault(fault, "STORES_CREATED")
        with self.database.connect(self.registry_path) as connection:
            self.foundation._set_pending_stage(
                connection,
                operation_id,
                "APPLICATION_STORES_CREATED",
            )
        return self.get_bundle(stored[0].migration_run_id)

    def commit_provisioning(self, operation_id: str) -> IntegratedRunBundle:
        """Commit an integrated run only after every compiler attempt is stored."""

        operation_id = require_uuid(operation_id, "operation_id")
        intent = self.foundation.get_operation_intent(operation_id)
        if (
            intent.kind is not MigrationOperationKind.MIGRATION_RUN_PLAN
            or intent.owner_kind != "MIGRATION_RUN"
        ):
            raise MigrationConflictError(
                "Operation identity does not belong to integrated run planning"
            )
        if intent.state is not MigrationOperationState.COMMITTED:
            self.foundation._finish_pending_intent(
                operation_id,
                stage="APPLICATIONS_MATERIALIZED",
                result={"migration_run_id": intent.owner_id},
            )
        return self.get_bundle(intent.owner_id)

    def get_target_binding(self, migration_run_id: str) -> RunTargetBinding:
        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        with self.database.connect(self.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                "SELECT * FROM target_binding WHERE migration_run_id = ?",
                [migration_run_id],
            )
        if not rows:
            self.foundation.get_migration_run(migration_run_id)
            raise MigrationNotFoundError("MigrationRun target binding not found")
        return self._target_from_row(rows[0])

    def get_requirement_plan(
        self,
        migration_run_id: str,
    ) -> MigrationRunRequirementPlan:
        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        with self.database.connect(self.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                "SELECT * FROM migration_run_requirement_plan "
                "WHERE migration_run_id = ?",
                [migration_run_id],
            )
        if not rows:
            self.foundation.get_migration_run(migration_run_id)
            raise MigrationNotFoundError("MigrationRun requirement plan not found")
        return self._plan_from_row(rows[0])

    def get_run_target_schema(self, migration_run_id: str) -> OdooSchemaCatalog:
        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        with self.database.connect(self.registry_path) as connection:
            row = connection.execute(
                "SELECT schema_hash, schema_json FROM migration_run_target_schema "
                "WHERE migration_run_id = ?",
                [migration_run_id],
            ).fetchone()
        if row is None:
            self.foundation.get_migration_run(migration_run_id)
            raise MigrationNotFoundError("MigrationRun target schema not found")
        schema = OdooSchemaCatalog.from_json(str(row[1]))
        if schema.content_hash != str(row[0]):
            raise MigrationConflictError("MigrationRun target schema is inconsistent")
        return schema

    def get_run_reference_bundle(
        self,
        migration_run_id: str,
    ) -> ReferenceBundle | None:
        """Read the immutable reference evidence captured once for a run."""

        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        with self.database.connect(self.registry_path) as connection:
            row = connection.execute(
                "SELECT bundle_hash, bundle_json "
                "FROM migration_run_reference_bundle "
                "WHERE migration_run_id = ?",
                [migration_run_id],
            ).fetchone()
        if row is None:
            self.foundation.get_migration_run(migration_run_id)
            return None
        bundle = ReferenceBundle.from_dict(json.loads(str(row[1])))
        if bundle.content_hash != str(row[0]):
            raise MigrationConflictError(
                "MigrationRun reference bundle is inconsistent"
            )
        return bundle

    def get_workspace_reference_bundle(
        self,
        workspace_id: str,
    ) -> ReferenceBundle | None:
        """Project only the run references selected by one application."""

        workspace_id = require_uuid(workspace_id, "workspace_id")
        with self.database.connect(self.registry_path) as connection:
            row = connection.execute(
                """
                SELECT a.application_id, a.migration_run_id
                  FROM migration_workspace w
                  JOIN recipe_application a
                    ON a.application_id = w.recipe_application_id
                 WHERE w.workspace_id = ?
                """,
                [workspace_id],
            ).fetchone()
            if row is None:
                return None
            requirements = self.foundation._rows(
                connection,
                "SELECT * FROM recipe_application_reference_requirement "
                "WHERE application_id = ? ORDER BY name",
                [str(row[0])],
            )
        run_bundle = self.get_run_reference_bundle(str(row[1]))
        available = {
            item.name: item for item in (run_bundle.datasets if run_bundle else ())
        }
        datasets = tuple(
            available[str(requirement["name"])]
            for requirement in requirements
            if str(requirement["name"]) in available
        )
        return ReferenceBundle(project_id=workspace_id, datasets=datasets)

    def is_application_workspace(self, workspace_id: str) -> bool:
        """Check the registry linkage without opening the workspace store."""

        workspace_id = require_uuid(workspace_id, "workspace_id")
        with self.database.connect(self.registry_path) as connection:
            row = connection.execute(
                "SELECT recipe_application_id FROM migration_workspace "
                "WHERE workspace_id = ?",
                [workspace_id],
            ).fetchone()
        return row is not None and row[0] is not None

    def get_workspace_target_schema(
        self,
        workspace_id: str,
    ) -> OdooSchemaCatalog | None:
        """Return only the run-level models required by this application."""

        workspace_id = require_uuid(workspace_id, "workspace_id")
        with self.database.connect(self.registry_path) as connection:
            row = connection.execute(
                """
                SELECT a.application_id, a.migration_run_id
                  FROM migration_workspace w
                  JOIN recipe_application a
                    ON a.application_id = w.recipe_application_id
                 WHERE w.workspace_id = ?
                """,
                [workspace_id],
            ).fetchone()
            if row is None:
                return None
            requirements = self.foundation._rows(
                connection,
                "SELECT * FROM recipe_application_requirement "
                "WHERE application_id = ? ORDER BY model",
                [str(row[0])],
            )
        schema = self.get_run_target_schema(str(row[1]))
        required_models = {str(item["model"]) for item in requirements}
        models = tuple(item for item in schema.models if item.name in required_models)
        projection_hash = content_hash(
            {
                "application_id": str(row[0]),
                "requirements": [
                    {
                        "fields": json.loads(str(item["fields_json"])),
                        "model": str(item["model"]),
                    }
                    for item in requirements
                ],
                "run_schema_hash": schema.content_hash,
                "workspace_id": workspace_id,
            }
        )
        return replace(
            schema,
            project_id=workspace_id,
            models=models,
            content_hash=projection_hash,
        )

    def list_applications(
        self,
        migration_run_id: str,
    ) -> tuple[RunRecipeApplication, ...]:
        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        with self.database.connect(self.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                "SELECT * FROM recipe_application WHERE migration_run_id = ? "
                "ORDER BY created_at, application_id",
                [migration_run_id],
            )
        return tuple(self._application_from_row(item) for item in rows)

    def list_issues(
        self,
        application_id: str,
    ) -> tuple[MigrationRunPlanIssue, ...]:
        application_id = require_uuid(application_id, "application_id")
        with self.database.connect(self.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                "SELECT * FROM recipe_application_issue "
                "WHERE application_id = ? ORDER BY ordinal",
                [application_id],
            )
            if not rows and connection.execute(
                "SELECT 1 FROM recipe_application WHERE application_id = ?",
                [application_id],
            ).fetchone() is None:
                self.foundation._raise_missing_identity(connection, application_id)
        return tuple(self._issue_from_row(item) for item in rows)

    def list_run_issues(
        self,
        migration_run_id: str,
    ) -> Mapping[str, tuple[MigrationRunPlanIssue, ...]]:
        """Read every application issue for one run in one registry query."""

        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        applications = self.list_applications(migration_run_id)
        by_application: dict[str, list[MigrationRunPlanIssue]] = {
            item.application_id: [] for item in applications
        }
        if not by_application:
            return {}
        with self.database.connect(self.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                """
                SELECT issue.*
                  FROM recipe_application_issue issue
                  JOIN recipe_application application
                    ON application.application_id = issue.application_id
                 WHERE application.migration_run_id = ?
                 ORDER BY issue.application_id, issue.ordinal
                """,
                [migration_run_id],
            )
        for row in rows:
            by_application[str(row["application_id"])].append(
                self._issue_from_row(row)
            )
        return {
            application_id: tuple(issues)
            for application_id, issues in by_application.items()
        }

    def application_dataset_ids(self, application_id: str) -> tuple[str, ...]:
        """Read the selected DataVersion datasets from the workspace store."""

        application = self.get_application(application_id)
        workspace = self.foundation.get_migration_workspace(application.workspace_id)
        projection = self.foundation.get_workspace_source_projection(
            workspace.workspace_id
        )
        if projection is None:
            return ()
        return tuple(item.dataset_id for item in projection.datasets)

    def get_application(self, application_id: str) -> RunRecipeApplication:
        application_id = require_uuid(application_id, "application_id")
        with self.database.connect(self.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                "SELECT * FROM recipe_application WHERE application_id = ?",
                [application_id],
            )
            if not rows:
                self.foundation._raise_missing_identity(connection, application_id)
        return self._application_from_row(rows[0])

    def save_application_materialization(
        self,
        application_id: str,
        *,
        expected_evidence_hash: str,
        status: RecipeApplicationStatus,
        issues: tuple[MigrationRunPlanIssue, ...],
        mapping_id: str | None,
        mapping_content_hash: str | None,
        evidence_hash: str,
        actor: Actor,
    ) -> RunRecipeApplication:
        """Save one compiler result without touching another application."""

        application_id = require_uuid(application_id, "application_id")
        expected_evidence_hash = require_hash(
            expected_evidence_hash,
            "expected_evidence_hash",
        )
        evidence_hash = require_hash(evidence_hash, "evidence_hash")
        issue_hash = content_hash([item.to_dict() for item in issues])
        now = utc_now()
        with self.database.connect(self.registry_path) as connection:
            connection.begin()
            try:
                row = connection.execute(
                    "SELECT project_id FROM recipe_application "
                    "WHERE application_id = ? AND evidence_hash = ?",
                    [application_id, expected_evidence_hash],
                ).fetchone()
                if row is None:
                    current = connection.execute(
                        "SELECT evidence_hash FROM recipe_application "
                        "WHERE application_id = ?",
                        [application_id],
                    ).fetchone()
                    if current == (evidence_hash,):
                        connection.rollback()
                        return self.get_application(application_id)
                    if current is None:
                        self.foundation._raise_missing_identity(
                            connection,
                            application_id,
                        )
                    raise MigrationConflictError(
                        "RecipeApplication changed before materialization"
                    )
                connection.execute(
                    """
                    UPDATE recipe_application
                       SET status = ?, issue_hash = ?, mapping_id = ?,
                           mapping_content_hash = ?, evidence_hash = ?,
                           updated_at = ?
                     WHERE application_id = ?
                    """,
                    [
                        RecipeApplicationStatus(status).value,
                        issue_hash,
                        mapping_id,
                        mapping_content_hash,
                        evidence_hash,
                        now.isoformat(),
                        application_id,
                    ],
                )
                connection.execute(
                    "DELETE FROM recipe_application_issue "
                    "WHERE application_id = ?",
                    [application_id],
                )
                self._insert_issues(connection, application_id, issues)
                self.foundation._insert_event(
                    connection,
                    project_id=str(row[0]),
                    aggregate_kind="RECIPE_APPLICATION",
                    aggregate_id=application_id,
                    aggregate_revision=1,
                    event_type="RECIPE_APPLICATION_MATERIALIZED",
                    detail={
                        "evidence_hash": evidence_hash,
                        "mapping_content_hash": mapping_content_hash,
                        "status": RecipeApplicationStatus(status).value,
                    },
                    actor=actor,
                    occurred_at=now,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_application(application_id)

    def progress(self, migration_run_id: str) -> IntegratedRunProgress:
        plan = self.get_requirement_plan(migration_run_id)
        applications = self.list_applications(migration_run_id)
        counts = {status: 0 for status in RecipeApplicationStatus}
        by_recipe = {}
        for application in applications:
            counts[application.status] += 1
            by_recipe[application.recipe_id] = application
        next_application_id = next(
            (
                by_recipe[recipe_id].application_id
                for recipe_id in plan.application_order
                if by_recipe[recipe_id].status
                not in {
                    RecipeApplicationStatus.RECONCILED,
                    RecipeApplicationStatus.FAILED,
                }
            ),
            None,
        )
        return IntegratedRunProgress(
            migration_run_id=migration_run_id,
            total_applications=len(applications),
            status_counts={key: value for key, value in counts.items() if value},
            next_application_id=next_application_id,
        )

    def get_bundle(self, migration_run_id: str) -> IntegratedRunBundle:
        run = self.foundation.get_migration_run(migration_run_id)
        return IntegratedRunBundle(
            run=run,
            target_binding=self.get_target_binding(migration_run_id),
            requirement_plan=self.get_requirement_plan(migration_run_id),
            applications=self.list_applications(migration_run_id),
            workspaces=self.foundation.list_migration_workspaces(migration_run_id),
        )

    def _insert_registry(
        self,
        *,
        run: MigrationRun,
        target_binding: RunTargetBinding,
        requirement_plan: MigrationRunRequirementPlan,
        applications: tuple[PlannedRecipeApplication, ...],
        target_schema: OdooSchemaCatalog,
        reference_bundle: ReferenceBundle | None,
        expected_project_revision: int,
        operation_id: str,
        actor: Actor,
    ) -> None:
        with self.database.connect(self.registry_path) as connection:
            connection.begin()
            try:
                existing = connection.execute(
                    "SELECT migration_run_id FROM migration_run "
                    "WHERE migration_run_id = ?",
                    [run.migration_run_id],
                ).fetchone()
                if existing is None:
                    self.foundation._assert_project_revision(
                        connection,
                        run.project_id,
                        expected_project_revision,
                    )
                    self._validate_context(
                        connection,
                        run,
                        target_binding,
                        requirement_plan,
                        applications,
                        target_schema,
                        reference_bundle,
                    )
                    identities = (
                        run.migration_run_id,
                        target_binding.target_binding_id,
                        *(item.application.application_id for item in applications),
                        *(item.workspace.workspace_id for item in applications),
                    )
                    for identity in identities:
                        self.foundation._assert_identity_available(connection, identity)
                    connection.execute(
                        "INSERT INTO migration_run_identity VALUES (?)",
                        [run.migration_run_id],
                    )
                    connection.execute(
                        "INSERT INTO migration_run VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        self.foundation._run_values(run),
                    )
                    connection.execute(
                        "INSERT INTO target_binding VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        self._target_values(target_binding),
                    )
                    connection.execute(
                        "INSERT INTO migration_run_requirement_plan VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        self._plan_values(requirement_plan),
                    )
                    connection.execute(
                        "INSERT INTO migration_run_target_schema VALUES "
                        "(?, ?, ?, ?, ?, ?)",
                        [
                            run.migration_run_id,
                            target_binding.target_binding_id,
                            requirement_plan.content_hash,
                            target_schema.content_hash,
                            target_schema.to_json(),
                            target_schema.captured_at.isoformat(),
                        ],
                    )
                    if reference_bundle is not None:
                        connection.execute(
                            "INSERT INTO migration_run_reference_bundle "
                            "VALUES (?, ?, ?, ?)",
                            [
                                run.migration_run_id,
                                target_binding.target_binding_id,
                                reference_bundle.content_hash,
                                canonical_json(
                                    reference_bundle.to_portable_dict()
                                ),
                            ],
                        )
                    for item in applications:
                        connection.execute(
                            "INSERT INTO recipe_application_identity VALUES (?)",
                            [item.application.application_id],
                        )
                    for item in applications:
                        connection.execute(
                            "INSERT INTO migration_workspace_identity VALUES (?)",
                            [item.workspace.workspace_id],
                        )
                        connection.execute(
                            "INSERT INTO migration_workspace VALUES "
                            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            self.foundation._workspace_values(item.workspace),
                        )
                        connection.execute(
                            "INSERT INTO recipe_application VALUES "
                            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            self._application_values(item.application),
                        )
                        self._insert_issues(
                            connection,
                            item.application.application_id,
                            item.issues,
                        )
                        if item.requirements:
                            connection.executemany(
                                "INSERT INTO recipe_application_requirement "
                                "VALUES (?, ?, ?, ?)",
                                [
                                    [
                                        item.application.application_id,
                                        requirement.model,
                                        canonical_json(list(requirement.fields)),
                                        content_hash(requirement.to_dict()),
                                    ]
                                    for requirement in item.requirements
                                ],
                            )
                        if item.reference_requirements:
                            connection.executemany(
                                "INSERT INTO "
                                "recipe_application_reference_requirement "
                                "VALUES (?, ?, ?, ?)",
                                [
                                    [
                                        item.application.application_id,
                                        requirement.name,
                                        requirement.content_hash,
                                        content_hash(requirement.to_dict()),
                                    ]
                                    for requirement in item.reference_requirements
                                ],
                            )
                    next_project_revision = self.foundation._advance_project(
                        connection,
                        run.project_id,
                        expected_project_revision,
                        run.updated_at,
                    )
                    self.foundation._insert_event(
                        connection,
                        project_id=run.project_id,
                        aggregate_kind="MIGRATION_RUN",
                        aggregate_id=run.migration_run_id,
                        aggregate_revision=run.optimistic_revision,
                        event_type="INTEGRATED_TEST_RUN_PLANNED",
                        detail={
                            "application_count": len(applications),
                            "project_revision": next_project_revision,
                            "requirement_plan_hash": requirement_plan.content_hash,
                        },
                        actor=actor,
                        occurred_at=run.created_at,
                    )
                    for item in applications:
                        self.foundation._insert_event(
                            connection,
                            project_id=run.project_id,
                            aggregate_kind="RECIPE_APPLICATION",
                            aggregate_id=item.application.application_id,
                            aggregate_revision=1,
                            event_type="RECIPE_APPLICATION_CREATED",
                            detail={
                                "recipe_id": item.application.recipe_id,
                                "recipe_revision": item.application.recipe_revision,
                                "workspace_id": item.workspace.workspace_id,
                            },
                            actor=actor,
                            occurred_at=item.application.created_at,
                        )
                self.foundation._set_pending_stage(
                    connection,
                    operation_id,
                    "REGISTRY_COMMITTED",
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _validate_context(
        self,
        connection,
        run: MigrationRun,
        target_binding: RunTargetBinding,
        requirement_plan: MigrationRunRequirementPlan,
        applications: tuple[PlannedRecipeApplication, ...],
        target_schema: OdooSchemaCatalog,
        reference_bundle: ReferenceBundle | None,
    ) -> None:
        data_version = connection.execute(
            "SELECT project_id, purpose, state FROM data_version "
            "WHERE data_version_id = ?",
            [run.data_version_id],
        ).fetchone()
        if data_version != (run.project_id, "TEST", "FROZEN"):
            raise MigrationConflictError(
                "Integrated Test planning requires one frozen Test DataVersion"
            )
        if run.purpose is not MigrationRunPurpose.TEST:
            raise MigrationConflictError("M4 provisions Test runs only")
        if (
            run.target_binding_id != target_binding.target_binding_id
            or target_binding.project_id != run.project_id
            or target_binding.migration_run_id != run.migration_run_id
            or requirement_plan.migration_run_id != run.migration_run_id
            or requirement_plan.target_binding_id != target_binding.target_binding_id
            or requirement_plan.data_version_id != run.data_version_id
        ):
            raise MigrationConflictError("Integrated run identities do not match")
        if (
            target_schema.content_hash != target_binding.schema_dependency_hash
            or target_schema.connection_target_hash
            != target_binding.connection_target_hash
        ):
            raise MigrationConflictError(
                "Target binding does not match the captured run schema"
            )
        reference_hashes = tuple(
            sorted(
                item.content_hash
                for item in (reference_bundle.datasets if reference_bundle else ())
            )
        )
        if reference_hashes != target_binding.reference_snapshot_hashes:
            raise MigrationConflictError(
                "Target binding does not match the captured run references"
            )
        selected = {
            (item.recipe_id, item.recipe_revision): item.semantic_hash
            for item in requirement_plan.selected_revisions
        }
        if len(applications) != len(selected):
            raise MigrationConflictError(
                "Every selected Recipe revision needs one application"
            )
        application_references = {
            (requirement.name, requirement.content_hash)
            for item in applications
            for requirement in item.reference_requirements
        }
        plan_references = {
            (requirement.name, requirement.content_hash)
            for requirement in requirement_plan.reference_requirements
        }
        if application_references != plan_references:
            raise MigrationConflictError(
                "Application references do not match the run requirement plan"
            )
        for item in applications:
            application = item.application
            workspace = item.workspace
            expected_hash = selected.get(
                (application.recipe_id, application.recipe_revision)
            )
            recipe = connection.execute(
                """
                SELECT r.project_id, rr.semantic_hash
                  FROM recipe r
                  JOIN recipe_revision rr ON rr.recipe_id = r.recipe_id
                 WHERE r.recipe_id = ? AND rr.version = ?
                """,
                [application.recipe_id, application.recipe_revision],
            ).fetchone()
            if recipe != (run.project_id, expected_hash):
                raise MigrationConflictError(
                    "Selected Recipe revision does not belong to this Project"
                )
            if expected_hash != application.recipe_semantic_hash:
                raise MigrationConflictError(
                    "RecipeApplication semantic identity changed"
                )
            if (
                application.project_id,
                application.migration_run_id,
                application.data_version_id,
                application.workspace_id,
                application.target_binding_id,
            ) != (
                run.project_id,
                run.migration_run_id,
                run.data_version_id,
                workspace.workspace_id,
                target_binding.target_binding_id,
            ):
                raise MigrationConflictError(
                    "RecipeApplication does not match its run context"
                )
            if (
                workspace.project_id,
                workspace.migration_run_id,
                workspace.data_version_id,
                workspace.recipe_application_id,
            ) != (
                run.project_id,
                run.migration_run_id,
                run.data_version_id,
                application.application_id,
            ):
                raise MigrationConflictError(
                    "MigrationWorkspace does not match its RecipeApplication"
                )

    @staticmethod
    def _planned_dict(value: PlannedRecipeApplication) -> dict[str, object]:
        return {
            "application": MigrationRunPlanningRepository._application_dict(
                value.application
            ),
            "dataset_ids": list(value.dataset_ids),
            "issues": [item.to_dict() for item in value.issues],
            "requirements": [item.to_dict() for item in value.requirements],
            "reference_requirements": [
                item.to_dict() for item in value.reference_requirements
            ],
            "workspace": MigrationFoundationRepository._workspace_dict(
                value.workspace
            ),
        }

    def _stored_plan(self, detail: Mapping[str, object]):
        applications = tuple(
            PlannedRecipeApplication(
                application=self._application_from_dict(dict(item["application"])),
                workspace=self.foundation._workspace_from_dict(
                    dict(item["workspace"])
                ),
                dataset_ids=tuple(str(value) for value in item["dataset_ids"]),
                requirements=tuple(
                    OdooModelRequirement(
                        model=str(value["model"]),
                        fields=tuple(str(field) for field in value["fields"]),
                    )
                    for value in item["requirements"]
                ),
                reference_requirements=tuple(
                    ReferenceRequirement(
                        name=str(value["name"]),
                        content_hash=str(value["content_hash"]),
                    )
                    for value in item.get("reference_requirements", ())
                ),
                issues=tuple(self._issue_from_dict(value) for value in item["issues"]),
            )
            for item in detail["applications"]
        )
        return (
            self.foundation._run_from_dict(dict(detail["run"])),
            self._target_from_dict(dict(detail["target_binding"])),
            self._plan_from_dict(dict(detail["requirement_plan"])),
            applications,
            OdooSchemaCatalog.from_json(str(detail["target_schema_json"])),
            (
                ReferenceBundle.from_dict(dict(detail["reference_bundle"]))
                if detail.get("reference_bundle") is not None
                else None
            ),
        )

    @staticmethod
    def _target_values(value: RunTargetBinding) -> list[object]:
        return [
            value.target_binding_id,
            value.project_id,
            value.migration_run_id,
            value.environment,
            value.connection_target_hash,
            value.credential_role,
            value.credential_generation,
            value.principal_hash,
            value.permission_hash,
            value.context_hash,
            value.schema_dependency_hash,
            canonical_json(list(value.reference_snapshot_hashes)),
            value.content_hash,
            value.created_at.isoformat(),
        ]

    @staticmethod
    def _target_from_dict(value: Mapping[str, object]) -> RunTargetBinding:
        result = RunTargetBinding(
            target_binding_id=str(value["target_binding_id"]),
            project_id=str(value["project_id"]),
            migration_run_id=str(value["migration_run_id"]),
            environment=str(value["environment"]),
            connection_target_hash=str(value["connection_target_hash"]),
            credential_role=str(value["credential_role"]),
            credential_generation=str(value["credential_generation"]),
            principal_hash=str(value["principal_hash"]),
            permission_hash=str(value["permission_hash"]),
            context_hash=str(value["context_hash"]),
            schema_dependency_hash=str(value["schema_dependency_hash"]),
            reference_snapshot_hashes=tuple(
                str(item) for item in value.get("reference_snapshot_hashes", ())
            ),
            created_at=datetime.fromisoformat(str(value["created_at"])),
        )
        if value.get("content_hash") != result.content_hash:
            raise MigrationConflictError("Stored TargetBinding hash is invalid")
        return result

    @classmethod
    def _target_from_row(cls, value: Mapping[str, object]) -> RunTargetBinding:
        return cls._target_from_dict(
            {
                **dict(value),
                "reference_snapshot_hashes": json.loads(
                    str(value["reference_snapshot_hashes_json"])
                ),
            }
        )

    @staticmethod
    def _plan_values(value: MigrationRunRequirementPlan) -> list[object]:
        return [
            value.migration_run_id,
            value.project_id,
            value.data_version_id,
            value.target_binding_id,
            value.contract_version,
            canonical_json([item.to_dict() for item in value.selected_revisions]),
            canonical_json([item.to_dict() for item in value.dependencies]),
            canonical_json([item.to_dict() for item in value.model_requirements]),
            canonical_json(
                [item.to_dict() for item in value.reference_requirements]
            ),
            canonical_json(list(value.application_order)),
            value.content_hash,
            value.created_at.isoformat(),
        ]

    @staticmethod
    def _plan_from_dict(value: Mapping[str, object]) -> MigrationRunRequirementPlan:
        result = MigrationRunRequirementPlan(
            migration_run_id=str(value["migration_run_id"]),
            project_id=str(value["project_id"]),
            data_version_id=str(value["data_version_id"]),
            target_binding_id=str(value["target_binding_id"]),
            selected_revisions=tuple(
                RecipeRevisionSelection(
                    recipe_id=str(item["recipe_id"]),
                    recipe_revision=int(item["recipe_revision"]),
                    semantic_hash=str(item["semantic_hash"]),
                )
                for item in value["selected_revisions"]
            ),
            dependencies=tuple(
                RecipeDependency(
                    before_recipe_id=str(item["before_recipe_id"]),
                    after_recipe_id=str(item["after_recipe_id"]),
                    kind=str(item["kind"]),
                    reason=str(item["reason"]),
                )
                for item in value["dependencies"]
            ),
            model_requirements=tuple(
                OdooModelRequirement(
                    model=str(item["model"]),
                    fields=tuple(str(field) for field in item["fields"]),
                )
                for item in value["model_requirements"]
            ),
            reference_requirements=tuple(
                ReferenceRequirement(
                    name=str(item["name"]),
                    content_hash=str(item["content_hash"]),
                )
                for item in value.get("reference_requirements", ())
            ),
            application_order=tuple(str(item) for item in value["application_order"]),
            created_at=datetime.fromisoformat(str(value["created_at"])),
            contract_version=int(value["contract_version"]),
        )
        if value.get("content_hash") != result.content_hash:
            raise MigrationConflictError("Stored run requirement plan is invalid")
        return result

    @classmethod
    def _plan_from_row(cls, value: Mapping[str, object]) -> MigrationRunRequirementPlan:
        return cls._plan_from_dict(
            {
                **dict(value),
                "application_order": json.loads(str(value["application_order_json"])),
                "dependencies": json.loads(str(value["dependencies_json"])),
                "model_requirements": json.loads(
                    str(value["model_requirements_json"])
                ),
                "reference_requirements": json.loads(
                    str(value["reference_requirements_json"])
                ),
                "selected_revisions": json.loads(
                    str(value["selected_revisions_json"])
                ),
            }
        )

    @staticmethod
    def _application_values(value: RunRecipeApplication) -> list[object]:
        return [
            value.application_id,
            value.project_id,
            value.migration_run_id,
            value.data_version_id,
            value.workspace_id,
            value.recipe_id,
            value.recipe_revision,
            value.recipe_semantic_hash,
            value.target_binding_id,
            value.physical_binding_hash,
            value.parameter_values_hash,
            value.status.value,
            value.issue_hash,
            value.mapping_id,
            value.mapping_content_hash,
            value.evidence_hash,
            value.created_at.isoformat(),
            value.updated_at.isoformat(),
        ]

    @staticmethod
    def _application_dict(value: RunRecipeApplication) -> dict[str, object]:
        return {
            "application_id": value.application_id,
            "created_at": value.created_at.isoformat(),
            "data_version_id": value.data_version_id,
            "evidence_hash": value.evidence_hash,
            "issue_hash": value.issue_hash,
            "mapping_content_hash": value.mapping_content_hash,
            "mapping_id": value.mapping_id,
            "migration_run_id": value.migration_run_id,
            "parameter_values_hash": value.parameter_values_hash,
            "physical_binding_hash": value.physical_binding_hash,
            "project_id": value.project_id,
            "recipe_id": value.recipe_id,
            "recipe_revision": value.recipe_revision,
            "recipe_semantic_hash": value.recipe_semantic_hash,
            "status": value.status.value,
            "target_binding_id": value.target_binding_id,
            "updated_at": value.updated_at.isoformat(),
            "workspace_id": value.workspace_id,
        }

    @staticmethod
    def _application_from_dict(value: Mapping[str, object]) -> RunRecipeApplication:
        return RunRecipeApplication(
            application_id=str(value["application_id"]),
            project_id=str(value["project_id"]),
            migration_run_id=str(value["migration_run_id"]),
            data_version_id=str(value["data_version_id"]),
            workspace_id=str(value["workspace_id"]),
            recipe_id=str(value["recipe_id"]),
            recipe_revision=int(value["recipe_revision"]),
            recipe_semantic_hash=str(value["recipe_semantic_hash"]),
            target_binding_id=str(value["target_binding_id"]),
            physical_binding_hash=str(value["physical_binding_hash"]),
            parameter_values_hash=str(value["parameter_values_hash"]),
            status=RecipeApplicationStatus(str(value["status"])),
            issue_hash=str(value["issue_hash"]),
            mapping_id=str(value["mapping_id"]) if value.get("mapping_id") else None,
            mapping_content_hash=(
                str(value["mapping_content_hash"])
                if value.get("mapping_content_hash")
                else None
            ),
            evidence_hash=str(value["evidence_hash"]),
            created_at=datetime.fromisoformat(str(value["created_at"])),
            updated_at=datetime.fromisoformat(str(value["updated_at"])),
        )

    @classmethod
    def _application_from_row(
        cls,
        value: Mapping[str, object],
    ) -> RunRecipeApplication:
        return cls._application_from_dict(value)

    @staticmethod
    def _insert_issues(connection, application_id, issues) -> None:
        if not issues:
            return
        connection.executemany(
            "INSERT INTO recipe_application_issue VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                [
                    application_id,
                    ordinal,
                    issue.code,
                    issue.level.value,
                    issue.message,
                    issue.recovery_action,
                    canonical_json(list(issue.recipe_ids)),
                    content_hash(issue.to_dict()),
                ]
                for ordinal, issue in enumerate(issues, start=1)
            ],
        )

    @staticmethod
    def _issue_from_dict(value: Mapping[str, object]) -> MigrationRunPlanIssue:
        return MigrationRunPlanIssue(
            code=str(value["code"]),
            level=MigrationRunPlanIssueLevel(str(value["level"])),
            message=str(value["message"]),
            recovery_action=str(value["recovery_action"]),
            recipe_ids=tuple(str(item) for item in value.get("recipe_ids", ())),
        )

    @classmethod
    def _issue_from_row(cls, value: Mapping[str, object]) -> MigrationRunPlanIssue:
        issue = cls._issue_from_dict(
            {
                **dict(value),
                "recipe_ids": json.loads(str(value["recipe_ids_json"])),
            }
        )
        if content_hash(issue.to_dict()) != str(value["content_hash"]):
            raise MigrationConflictError("Stored RecipeApplication issue is invalid")
        return issue
