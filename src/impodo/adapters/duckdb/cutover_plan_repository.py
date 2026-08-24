"""Persist Project CutoverPlan revisions and exact Test qualifications."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Mapping
from uuid import UUID, uuid5

from ...access import Actor, ActorIdentity
from ...domain.serialization import canonical_json, content_hash
from ...migration_cutover import (
    ApplicationQualificationEvidence,
    CutoverPlan,
    CutoverPlanQualification,
    CutoverPlanRevision,
    CutoverWriteOwnership,
    PROJECT_SHARED_CONTROL_IDS,
    ProjectCutoverSelection,
    QualifiedOutcomes,
    RecipeApplicationQualification,
    RunCutoverPlanBinding,
)
from ...migration_foundation import (
    FaultInjector,
    MigrationConflictError,
    MigrationNotFoundError,
    MigrationOperationKind,
    MigrationOperationState,
    require_hash,
    require_revision,
    require_uuid,
    required_text,
    utc_now,
)
from ...migration_run_planning import (
    MigrationRunRequirementPlan,
    RecipeApplicationStatus,
    RecipeDependency,
    RecipeRevisionSelection,
)
from .migration_foundation_repository import MigrationFoundationRepository
from ..protected_project_evidence_store import ProtectedProjectEvidenceStore


class CutoverPlanRepository:
    """Own bounded CutoverPlan projections and protected evidence."""

    def __init__(
        self,
        foundation: MigrationFoundationRepository,
        evidence_store: ProtectedProjectEvidenceStore,
    ) -> None:
        self.foundation = foundation
        self.evidence_store = evidence_store
        self.database = foundation.database
        self.registry_path = foundation.registry_path

    def ensure_for_run(
        self,
        *,
        project_id: str,
        migration_run_id: str,
        requirement_plan: MigrationRunRequirementPlan,
        write_ownership: tuple[CutoverWriteOwnership, ...],
        operation_id: str,
        actor: Actor,
        display_name: str = "Project cutover plan",
        fault: FaultInjector | None = None,
    ) -> RunCutoverPlanBinding:
        """Append a plan revision only when its reusable meaning changed."""

        project_id = require_uuid(project_id, "project_id")
        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        operation_id = require_uuid(operation_id, "operation_id")
        clean_name = required_text(display_name, "display_name", maximum=200)
        if (
            requirement_plan.project_id != project_id
            or requirement_plan.migration_run_id != migration_run_id
        ):
            raise MigrationConflictError("CutoverPlan does not match its Test run")
        meaning = self._meaning(requirement_plan, write_ownership)
        request_hash = content_hash(
            {
                "migration_run_id": migration_run_id,
                "plan_meaning": meaning,
                "project_id": project_id,
                "display_name": clean_name,
            }
        )
        existing_intent = self._intent(operation_id)
        if existing_intent is not None:
            if existing_intent.request_hash != request_hash:
                raise MigrationConflictError(
                    "Operation identity was already used with different meaning"
                )
            if existing_intent.state is MigrationOperationState.COMMITTED:
                return self.get_run_binding(migration_run_id)
            stored_revision = self._revision_from_dict(
                dict(existing_intent.detail["plan_revision"])
            )
            expected_workspace_revision = int(existing_intent.expected_revision or 0)
            created_plan = bool(existing_intent.detail["created_plan"])
        else:
            project = self.foundation.get_project(project_id)
            cutover_plan_id = str(uuid5(UUID(project_id), "project-cutover-plan"))
            current = self._current_revision_or_none(cutover_plan_id)
            meaning_hash = content_hash(meaning)
            if current is not None and current.meaning_hash == meaning_hash:
                version = current.version
                parent_version = current.parent_version
                created_at = current.created_at
                plan_content_hash = current.content_hash
            else:
                version = 1 if current is None else current.version + 1
                parent_version = None if current is None else current.version
                created_at = utc_now()
                unhashed = {
                    **meaning,
                    "created_at": created_at.isoformat(),
                    "created_by": self._actor_dict(actor.identity),
                    "cutover_plan_id": cutover_plan_id,
                    "parent_version": parent_version,
                    "project_id": project_id,
                    "version": version,
                }
                plan_content_hash = content_hash(unhashed)
            stored_revision = CutoverPlanRevision(
                cutover_plan_id=cutover_plan_id,
                project_id=project_id,
                version=version,
                parent_version=parent_version,
                selected_revisions=tuple(requirement_plan.selected_revisions),
                dependencies=tuple(requirement_plan.dependencies),
                write_ownership=tuple(sorted(write_ownership)),
                shared_control_ids=PROJECT_SHARED_CONTROL_IDS,
                requirement_plan_hash=str(meaning["requirement_plan_hash"]),
                created_by=actor.identity,
                created_at=created_at,
                meaning_hash=meaning_hash,
                content_hash=plan_content_hash,
            )
            created_plan = current is None
            detail = {
                "created_plan": created_plan,
                "display_name": clean_name,
                "migration_run_id": migration_run_id,
                "plan_revision": stored_revision.to_dict(),
            }
            existing_intent = self.foundation._reserve_intent(
                operation_id=operation_id,
                project_id=project_id,
                owner_kind="CUTOVER_PLAN",
                owner_id=cutover_plan_id,
                kind=MigrationOperationKind.CUTOVER_PLAN_REVISION,
                request_hash=request_hash,
                expected_revision=project.optimistic_revision,
                detail=detail,
                actor=actor,
            )
            expected_workspace_revision = int(existing_intent.expected_revision or 0)

        self.foundation._fault(fault, "INTENT_RESERVED")
        bound_at = utc_now()
        with self.database.connect(self.registry_path) as connection:
            connection.begin()
            try:
                binding = connection.execute(
                    "SELECT plan_content_hash FROM migration_run_cutover_plan "
                    "WHERE migration_run_id = ?",
                    [migration_run_id],
                ).fetchone()
                if binding is None:
                    self.foundation._assert_workspace_revision(
                        connection,
                        project_id,
                        expected_workspace_revision,
                    )
                    if created_plan:
                        connection.execute(
                            "INSERT INTO cutover_plan_identity VALUES (?)",
                            [stored_revision.cutover_plan_id],
                        )
                        connection.execute(
                            "INSERT INTO cutover_plan VALUES "
                            "(?, ?, ?, ?, ?, ?, ?, ?)",
                            [
                                stored_revision.cutover_plan_id,
                                project_id,
                                clean_name,
                                stored_revision.version,
                                1,
                                stored_revision.created_at.isoformat(),
                                stored_revision.created_at.isoformat(),
                                None,
                            ],
                        )
                    if self._revision_row(
                        connection,
                        stored_revision.cutover_plan_id,
                        stored_revision.version,
                    ) is None:
                        self._insert_revision(connection, stored_revision)
                        if not created_plan:
                            connection.execute(
                                "UPDATE cutover_plan SET current_revision = ?, "
                                "optimistic_revision = optimistic_revision + 1, "
                                "updated_at = ? WHERE cutover_plan_id = ?",
                                [
                                    stored_revision.version,
                                    stored_revision.created_at.isoformat(),
                                    stored_revision.cutover_plan_id,
                                ],
                            )
                    connection.execute(
                        "INSERT INTO migration_run_cutover_plan VALUES "
                        "(?, ?, ?, ?, ?)",
                        [
                            migration_run_id,
                            stored_revision.cutover_plan_id,
                            stored_revision.version,
                            stored_revision.content_hash,
                            bound_at.isoformat(),
                        ],
                    )
                    next_revision = self.foundation._advance_project(
                        connection,
                        project_id,
                        expected_workspace_revision,
                        bound_at,
                    )
                    self.foundation._insert_event(
                        connection,
                        project_id=project_id,
                        aggregate_kind="CUTOVER_PLAN",
                        aggregate_id=stored_revision.cutover_plan_id,
                        aggregate_revision=stored_revision.version,
                        event_type="CUTOVER_PLAN_BOUND_TO_TEST_RUN",
                        detail={
                            "migration_run_id": migration_run_id,
                            "plan_content_hash": stored_revision.content_hash,
                            "project_revision": next_revision,
                        },
                        actor=actor,
                        occurred_at=bound_at,
                    )
                elif str(binding[0]) != stored_revision.content_hash:
                    raise MigrationConflictError(
                        "Test run already belongs to another CutoverPlan revision"
                    )
                self.foundation._commit_intent(
                    connection,
                    operation_id,
                    stage="PLAN_BOUND",
                    result={"migration_run_id": migration_run_id},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self.foundation._fault(fault, "REGISTRY_COMMITTED")
        return self.get_run_binding(migration_run_id)

    def qualify(
        self,
        *,
        plan: CutoverPlanRevision,
        migration_run_id: str,
        application_evidence: tuple[ApplicationQualificationEvidence, ...],
        target_binding_hash: str,
        integrated_payload: Mapping[str, object],
        expected_workspace_revision: int,
        operation_id: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> CutoverPlanQualification:
        """Publish all exact per-application and integrated evidence together."""

        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        operation_id = require_uuid(operation_id, "operation_id")
        expected_workspace_revision = require_revision(
            expected_workspace_revision,
            "expected_workspace_revision",
        )
        target_binding_hash = require_hash(
            target_binding_hash,
            "target_binding_hash",
        )
        applications = tuple(sorted(application_evidence, key=lambda item: item.application_id))
        self._validate_qualification_context(
            plan,
            migration_run_id=migration_run_id,
            target_binding_hash=target_binding_hash,
            applications=applications,
            integrated_payload=integrated_payload,
        )
        integrated_hash = content_hash(integrated_payload)
        qualification_id = str(uuid5(UUID(operation_id), "integrated-qualification"))
        application_ids = tuple(item.application_id for item in applications)
        app_qualification_by_application = {
            item.application_id: str(
                uuid5(UUID(operation_id), f"application:{item.application_id}")
            )
            for item in applications
        }
        app_qualification_ids = tuple(
            sorted(app_qualification_by_application.values())
        )
        request_hash = content_hash(
            {
                "application_evidence_hashes": [item.content_hash for item in applications],
                "integrated_evidence_hash": integrated_hash,
                "plan_content_hash": plan.content_hash,
                "project_id": plan.project_id,
            }
        )
        detail = {
            "application_evidence": [item.to_dict() for item in applications],
            "application_qualification_ids": list(app_qualification_ids),
            "integrated_payload": dict(integrated_payload),
            "qualification_id": qualification_id,
        }
        intent = self.foundation._reserve_intent(
            operation_id=operation_id,
            project_id=plan.project_id,
            owner_kind="CUTOVER_PLAN",
            owner_id=plan.cutover_plan_id,
            kind=MigrationOperationKind.CUTOVER_PLAN_QUALIFICATION,
            request_hash=request_hash,
            expected_revision=expected_workspace_revision,
            detail=detail,
            actor=actor,
        )
        if intent.state is MigrationOperationState.COMMITTED:
            return self.get_qualification(qualification_id)
        self.foundation._fault(fault, "INTENT_RESERVED")

        app_artifacts = []
        for evidence in applications:
            app_qualification_id = app_qualification_by_application[
                evidence.application_id
            ]
            artifact = self.evidence_store.put(
                plan.project_id,
                qualification_id=app_qualification_id,
                logical_hash=evidence.content_hash,
                payload=canonical_json(evidence.to_dict()).encode("utf-8"),
            )
            app_artifacts.append(artifact)
        integrated_artifact = self.evidence_store.put(
            plan.project_id,
            qualification_id=qualification_id,
            logical_hash=integrated_hash,
            payload=canonical_json(integrated_payload).encode("utf-8"),
        )
        self.foundation._fault(fault, "EVIDENCE_STORED")

        qualified_at = utc_now()
        with self.database.connect(self.registry_path) as connection:
            connection.begin()
            try:
                existing = connection.execute(
                    "SELECT integrated_evidence_hash FROM "
                    "cutover_plan_qualification WHERE qualification_id = ?",
                    [qualification_id],
                ).fetchone()
                if existing is None:
                    self.foundation._assert_workspace_revision(
                        connection,
                        plan.project_id,
                        int(intent.expected_revision or 0),
                    )
                    self._assert_current_binding(
                        connection,
                        migration_run_id,
                        plan,
                    )
                    for evidence, artifact in zip(
                        applications,
                        app_artifacts,
                        strict=True,
                    ):
                        app_id = app_qualification_by_application[
                            evidence.application_id
                        ]
                        connection.execute(
                            "INSERT INTO recipe_qualification VALUES "
                            "(?, ?, ?, ?, ?, ?, 'TEST_QUALIFIED', ?, ?, ?, ?, ?, ?, ?, ?)",
                            [
                                app_id,
                                plan.project_id,
                                evidence.recipe_id,
                                evidence.recipe_revision,
                                evidence.application_id,
                                evidence.target_binding_hash,
                                canonical_json(evidence.outcomes.to_dict()),
                                artifact.storage_key,
                                artifact.artifact_hash,
                                evidence.content_hash,
                                actor.identity.issuer,
                                actor.identity.subject_id,
                                actor.identity.display_name,
                                qualified_at.isoformat(),
                            ],
                        )
                    connection.execute(
                        "INSERT INTO cutover_plan_qualification VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'TEST_QUALIFIED', ?, ?, ?, ?)",
                        [
                            qualification_id,
                            plan.project_id,
                            plan.cutover_plan_id,
                            plan.version,
                            plan.content_hash,
                            migration_run_id,
                            canonical_json(list(application_ids)),
                            canonical_json(list(app_qualification_ids)),
                            target_binding_hash,
                            plan.requirement_plan_hash,
                            integrated_hash,
                            integrated_artifact.storage_key,
                            integrated_artifact.artifact_hash,
                            actor.identity.issuer,
                            actor.identity.subject_id,
                            actor.identity.display_name,
                            qualified_at.isoformat(),
                        ],
                    )
                    connection.execute(
                        "UPDATE recipe_application SET status = ?, updated_at = ? "
                        "WHERE migration_run_id = ?",
                        [
                            RecipeApplicationStatus.QUALIFIED.value,
                            qualified_at.isoformat(),
                            migration_run_id,
                        ],
                    )
                    connection.execute(
                        "UPDATE migration_run SET state = 'COMPLETED', "
                        "optimistic_revision = optimistic_revision + 1, updated_at = ? "
                        "WHERE migration_run_id = ?",
                        [qualified_at.isoformat(), migration_run_id],
                    )
                    next_revision = self.foundation._advance_project(
                        connection,
                        plan.project_id,
                        int(intent.expected_revision or 0),
                        qualified_at,
                    )
                    self.foundation._insert_event(
                        connection,
                        project_id=plan.project_id,
                        aggregate_kind="CUTOVER_PLAN",
                        aggregate_id=plan.cutover_plan_id,
                        aggregate_revision=plan.version,
                        event_type="CUTOVER_PLAN_TEST_QUALIFIED",
                        detail={
                            "integrated_evidence_hash": integrated_hash,
                            "project_revision": next_revision,
                            "qualification_id": qualification_id,
                            "test_run_id": migration_run_id,
                        },
                        actor=actor,
                        occurred_at=qualified_at,
                    )
                elif str(existing[0]) != integrated_hash:
                    raise MigrationConflictError(
                        "Qualification identity has different evidence"
                    )
                self.foundation._commit_intent(
                    connection,
                    operation_id,
                    stage="QUALIFICATION_COMMITTED",
                    result={"qualification_id": qualification_id},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self.foundation._fault(fault, "REGISTRY_COMMITTED")
        return self.get_qualification(qualification_id)

    def select(
        self,
        qualification_id: str,
        *,
        expected_workspace_revision: int,
        operation_id: str,
        actor: Actor,
    ) -> ProjectCutoverSelection:
        """Select exact qualified evidence without granting Production authority."""

        qualification = self.get_qualification(qualification_id)
        operation_id = require_uuid(operation_id, "operation_id")
        expected_workspace_revision = require_revision(
            expected_workspace_revision,
            "expected_workspace_revision",
        )
        selection_id = str(uuid5(UUID(operation_id), "project-cutover-selection"))
        selection_hash = content_hash(
            {
                "cutover_plan_id": qualification.cutover_plan_id,
                "cutover_plan_revision": qualification.cutover_plan_revision,
                "qualification_id": qualification.qualification_id,
                "project_id": qualification.project_id,
            }
        )
        artifact = self.evidence_store.inspect(
            qualification.project_id,
            storage_key=qualification.evidence_storage_key,
            logical_hash=qualification.integrated_evidence_hash,
        )
        if artifact.artifact_hash != qualification.artifact_hash:
            raise MigrationConflictError(
                "Protected integrated qualification evidence changed"
            )
        intent = self.foundation._reserve_intent(
            operation_id=operation_id,
            project_id=qualification.project_id,
            owner_kind="CUTOVER_PLAN",
            owner_id=qualification.cutover_plan_id,
            kind=MigrationOperationKind.PROJECT_CUTOVER_SELECTION,
            request_hash=selection_hash,
            expected_revision=expected_workspace_revision,
            detail={
                "qualification_id": qualification.qualification_id,
                "selection_id": selection_id,
            },
            actor=actor,
        )
        if intent.state is MigrationOperationState.COMMITTED:
            return self.get_selection(selection_id)
        selected_at = utc_now()
        with self.database.connect(self.registry_path) as connection:
            connection.begin()
            try:
                existing = connection.execute(
                    "SELECT content_hash FROM project_cutover_selection "
                    "WHERE cutover_selection_id = ?",
                    [selection_id],
                ).fetchone()
                if existing is None:
                    self.foundation._assert_workspace_revision(
                        connection,
                        qualification.project_id,
                        int(intent.expected_revision or 0),
                    )
                    current = connection.execute(
                        "SELECT current_revision FROM cutover_plan "
                        "WHERE cutover_plan_id = ?",
                        [qualification.cutover_plan_id],
                    ).fetchone()
                    if current is None or int(current[0]) != qualification.cutover_plan_revision:
                        raise MigrationConflictError(
                            "Qualify the current CutoverPlan revision before selecting it"
                        )
                    connection.execute(
                        "INSERT INTO project_cutover_selection VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            selection_id,
                            qualification.project_id,
                            qualification.cutover_plan_id,
                            qualification.cutover_plan_revision,
                            qualification.qualification_id,
                            selection_hash,
                            actor.identity.issuer,
                            actor.identity.subject_id,
                            actor.identity.display_name,
                            selected_at.isoformat(),
                        ],
                    )
                    next_revision = self.foundation._advance_project(
                        connection,
                        qualification.project_id,
                        int(intent.expected_revision or 0),
                        selected_at,
                    )
                    self.foundation._insert_event(
                        connection,
                        project_id=qualification.project_id,
                        aggregate_kind="CUTOVER_PLAN",
                        aggregate_id=qualification.cutover_plan_id,
                        aggregate_revision=qualification.cutover_plan_revision,
                        event_type="PROJECT_CUTOVER_QUALIFICATION_SELECTED",
                        detail={
                            "project_revision": next_revision,
                            "qualification_id": qualification.qualification_id,
                            "selection_id": selection_id,
                        },
                        actor=actor,
                        occurred_at=selected_at,
                    )
                elif str(existing[0]) != selection_hash:
                    raise MigrationConflictError(
                        "Cutover selection identity has different meaning"
                    )
                self.foundation._commit_intent(
                    connection,
                    operation_id,
                    stage="SELECTION_COMMITTED",
                    result={"cutover_selection_id": selection_id},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_selection(selection_id)

    def get_plan(self, cutover_plan_id: str) -> CutoverPlan:
        cutover_plan_id = require_uuid(cutover_plan_id, "cutover_plan_id")
        with self.database.connect(self.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                "SELECT * FROM cutover_plan WHERE cutover_plan_id = ?",
                [cutover_plan_id],
            )
        if not rows:
            raise MigrationNotFoundError("CutoverPlan not found")
        row = rows[0]
        return CutoverPlan(
            cutover_plan_id=str(row["cutover_plan_id"]),
            project_id=str(row["project_id"]),
            display_name=str(row["display_name"]),
            current_revision=int(row["current_revision"]),
            optimistic_revision=int(row["optimistic_revision"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            archived_at=(
                datetime.fromisoformat(str(row["archived_at"]))
                if row["archived_at"] is not None
                else None
            ),
        )

    def get_revision(
        self,
        cutover_plan_id: str,
        version: int,
    ) -> CutoverPlanRevision:
        cutover_plan_id = require_uuid(cutover_plan_id, "cutover_plan_id")
        version = require_revision(version, "version")
        with self.database.connect(self.registry_path) as connection:
            row = self._revision_row(connection, cutover_plan_id, version)
            if row is None:
                raise MigrationNotFoundError("CutoverPlan revision not found")
            recipes = self.foundation._rows(
                connection,
                "SELECT * FROM cutover_plan_recipe WHERE cutover_plan_id = ? "
                "AND plan_revision = ? ORDER BY recipe_id",
                [cutover_plan_id, version],
            )
            dependencies = self.foundation._rows(
                connection,
                "SELECT * FROM cutover_dependency WHERE cutover_plan_id = ? "
                "AND plan_revision = ? ORDER BY before_recipe_id, after_recipe_id",
                [cutover_plan_id, version],
            )
            ownership = self.foundation._rows(
                connection,
                "SELECT * FROM cutover_write_ownership WHERE cutover_plan_id = ? "
                "AND plan_revision = ? ORDER BY recipe_id, model, field",
                [cutover_plan_id, version],
            )
        return CutoverPlanRevision(
            cutover_plan_id=cutover_plan_id,
            project_id=self.get_plan(cutover_plan_id).project_id,
            version=version,
            parent_version=(
                int(row["parent_version"])
                if row["parent_version"] is not None
                else None
            ),
            selected_revisions=tuple(
                RecipeRevisionSelection(
                    recipe_id=str(item["recipe_id"]),
                    recipe_revision=int(item["recipe_revision"]),
                    semantic_hash=str(item["semantic_hash"]),
                )
                for item in recipes
            ),
            dependencies=tuple(
                RecipeDependency(
                    before_recipe_id=str(item["before_recipe_id"]),
                    after_recipe_id=str(item["after_recipe_id"]),
                    kind=str(item["kind"]),
                    reason=str(item["reason"]),
                )
                for item in dependencies
            ),
            write_ownership=tuple(
                CutoverWriteOwnership(
                    recipe_id=str(item["recipe_id"]),
                    model=str(item["model"]),
                    field=str(item["field"]),
                )
                for item in ownership
            ),
            shared_control_ids=tuple(json.loads(str(row["shared_controls_json"]))),
            requirement_plan_hash=str(row["requirement_plan_hash"]),
            created_by=self._actor_from_row(row, "created_by"),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            meaning_hash=str(row["meaning_hash"]),
            content_hash=str(row["content_hash"]),
        )

    def get_run_binding(self, migration_run_id: str) -> RunCutoverPlanBinding:
        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        with self.database.connect(self.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                "SELECT * FROM migration_run_cutover_plan WHERE migration_run_id = ?",
                [migration_run_id],
            )
        if not rows:
            raise MigrationNotFoundError("Test run CutoverPlan binding not found")
        row = rows[0]
        return RunCutoverPlanBinding(
            migration_run_id=migration_run_id,
            cutover_plan_id=str(row["cutover_plan_id"]),
            cutover_plan_revision=int(row["cutover_plan_revision"]),
            plan_content_hash=str(row["plan_content_hash"]),
            bound_at=datetime.fromisoformat(str(row["bound_at"])),
        )

    def get_qualification(self, qualification_id: str) -> CutoverPlanQualification:
        qualification_id = require_uuid(qualification_id, "qualification_id")
        with self.database.connect(self.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                "SELECT * FROM cutover_plan_qualification WHERE qualification_id = ?",
                [qualification_id],
            )
        if not rows:
            raise MigrationNotFoundError("CutoverPlan qualification not found")
        row = rows[0]
        return self._qualification_from_row(row)

    def assert_qualification_authentic(
        self,
        qualification: CutoverPlanQualification,
    ) -> None:
        """Authenticate the selected Test evidence before Production reuse."""

        artifact = self.evidence_store.inspect(
            qualification.project_id,
            storage_key=qualification.evidence_storage_key,
            logical_hash=qualification.integrated_evidence_hash,
        )
        if artifact.artifact_hash != qualification.artifact_hash:
            raise MigrationConflictError(
                "Protected integrated qualification evidence changed"
            )

    @classmethod
    def _qualification_from_row(
        cls,
        row: Mapping[str, object],
    ) -> CutoverPlanQualification:
        return CutoverPlanQualification(
            qualification_id=str(row["qualification_id"]),
            project_id=str(row["project_id"]),
            cutover_plan_id=str(row["cutover_plan_id"]),
            cutover_plan_revision=int(row["cutover_plan_revision"]),
            plan_content_hash=str(row["plan_content_hash"]),
            test_run_id=str(row["test_run_id"]),
            application_ids=tuple(json.loads(str(row["application_ids_json"]))),
            application_qualification_ids=tuple(
                json.loads(str(row["application_qualification_ids_json"]))
            ),
            target_binding_hash=str(row["target_binding_hash"]),
            requirement_plan_hash=str(row["requirement_plan_hash"]),
            integrated_evidence_hash=str(row["integrated_evidence_hash"]),
            evidence_storage_key=str(row["evidence_storage_key"]),
            artifact_hash=str(row["artifact_hash"]),
            status=str(row["status"]),
            qualified_by=cls._actor_from_row(row, "qualified_by"),
            qualified_at=datetime.fromisoformat(str(row["qualified_at"])),
        )

    def list_qualifications(
        self,
        cutover_plan_id: str,
        version: int | None = None,
    ) -> tuple[CutoverPlanQualification, ...]:
        cutover_plan_id = require_uuid(cutover_plan_id, "cutover_plan_id")
        parameters: list[object] = [cutover_plan_id]
        clause = ""
        if version is not None:
            clause = " AND cutover_plan_revision = ?"
            parameters.append(require_revision(version, "version"))
        with self.database.connect(self.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                "SELECT * FROM cutover_plan_qualification "
                "WHERE cutover_plan_id = ?" + clause + " ORDER BY qualified_at DESC",
                parameters,
            )
        return tuple(self._qualification_from_row(row) for row in rows)

    def get_selection(self, selection_id: str) -> ProjectCutoverSelection:
        selection_id = require_uuid(selection_id, "selection_id")
        with self.database.connect(self.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                "SELECT * FROM project_cutover_selection "
                "WHERE cutover_selection_id = ?",
                [selection_id],
            )
        if not rows:
            raise MigrationNotFoundError("Project cutover selection not found")
        row = rows[0]
        return ProjectCutoverSelection(
            cutover_selection_id=selection_id,
            project_id=str(row["project_id"]),
            cutover_plan_id=str(row["cutover_plan_id"]),
            cutover_plan_revision=int(row["cutover_plan_revision"]),
            qualification_id=str(row["qualification_id"]),
            content_hash=str(row["content_hash"]),
            selected_by=self._actor_from_row(row, "selected_by"),
            selected_at=datetime.fromisoformat(str(row["selected_at"])),
        )

    def current_selection(self, project_id: str) -> ProjectCutoverSelection | None:
        project_id = require_uuid(project_id, "project_id")
        with self.database.connect(self.registry_path) as connection:
            row = connection.execute(
                "SELECT cutover_selection_id FROM project_cutover_selection "
                "WHERE project_id = ? ORDER BY selected_at DESC, "
                "cutover_selection_id DESC LIMIT 1",
                [project_id],
            ).fetchone()
        return self.get_selection(str(row[0])) if row is not None else None

    def committed_qualification_for_operation(
        self,
        operation_id: str,
        *,
        project_id: str,
        migration_run_id: str,
        integrated_evidence_hash: str,
        expected_workspace_revision: int,
        actor: Actor,
    ) -> CutoverPlanQualification | None:
        """Return one exact committed replay before mutable evidence review."""

        intent = self._intent(require_uuid(operation_id, "operation_id"))
        if intent is None:
            return None
        if (
            intent.kind is not MigrationOperationKind.CUTOVER_PLAN_QUALIFICATION
            or intent.project_id != project_id
            or intent.expected_revision != expected_workspace_revision
            or intent.actor.issuer != actor.identity.issuer
            or intent.actor.subject_id != actor.identity.subject_id
        ):
            raise MigrationConflictError(
                "Operation identity was already used with different meaning"
            )
        if intent.state is not MigrationOperationState.COMMITTED:
            return None
        qualification_id = str(intent.detail["qualification_id"])
        qualification = self.get_qualification(qualification_id)
        if (
            qualification.test_run_id != migration_run_id
            or qualification.integrated_evidence_hash != integrated_evidence_hash
        ):
            raise MigrationConflictError(
                "Operation identity was already used with different evidence"
            )
        return qualification

    def committed_selection_for_operation(
        self,
        operation_id: str,
        *,
        project_id: str,
        qualification_id: str,
        expected_workspace_revision: int,
        actor: Actor,
    ) -> ProjectCutoverSelection | None:
        """Return one exact committed rollout-selection replay."""

        intent = self._intent(require_uuid(operation_id, "operation_id"))
        if intent is None:
            return None
        if (
            intent.kind is not MigrationOperationKind.PROJECT_CUTOVER_SELECTION
            or intent.project_id != project_id
            or intent.expected_revision != expected_workspace_revision
            or intent.actor.issuer != actor.identity.issuer
            or intent.actor.subject_id != actor.identity.subject_id
            or str(intent.detail["qualification_id"]) != qualification_id
        ):
            raise MigrationConflictError(
                "Operation identity was already used with different meaning"
            )
        if intent.state is not MigrationOperationState.COMMITTED:
            return None
        return self.get_selection(str(intent.detail["selection_id"]))

    @staticmethod
    def _meaning(
        requirement_plan: MigrationRunRequirementPlan,
        ownership: tuple[CutoverWriteOwnership, ...],
    ) -> dict[str, object]:
        requirement_hash = content_hash(
            {
                "application_order": list(requirement_plan.application_order),
                "contract_version": requirement_plan.contract_version,
                "dependencies": [item.to_dict() for item in requirement_plan.dependencies],
                "model_requirements": [
                    item.to_dict() for item in requirement_plan.model_requirements
                ],
                "reference_requirements": [
                    item.to_dict() for item in requirement_plan.reference_requirements
                ],
                "selected_revisions": [
                    item.to_dict() for item in requirement_plan.selected_revisions
                ],
            }
        )
        return {
            "contract_version": 1,
            "dependencies": [item.to_dict() for item in requirement_plan.dependencies],
            "requirement_plan_hash": requirement_hash,
            "selected_revisions": [
                item.to_dict() for item in requirement_plan.selected_revisions
            ],
            "shared_control_ids": list(PROJECT_SHARED_CONTROL_IDS),
            "write_ownership": [item.to_dict() for item in sorted(ownership)],
        }

    @staticmethod
    def _validate_qualification_context(
        plan: CutoverPlanRevision,
        *,
        migration_run_id: str,
        target_binding_hash: str,
        applications: tuple[ApplicationQualificationEvidence, ...],
        integrated_payload: Mapping[str, object],
    ) -> None:
        selections = {
            (item.recipe_id, item.recipe_revision, item.semantic_hash)
            for item in plan.selected_revisions
        }
        evidence_selections = {
            (item.recipe_id, item.recipe_revision, item.recipe_semantic_hash)
            for item in applications
        }
        if not applications or selections != evidence_selections:
            raise MigrationConflictError(
                "Application evidence does not cover the exact CutoverPlan"
            )
        if any(
            item.project_id != plan.project_id
            or item.migration_run_id != migration_run_id
            or item.target_binding_hash != target_binding_hash
            for item in applications
        ):
            raise MigrationConflictError(
                "Application evidence does not match the Project Test run"
            )
        expected = {
            "applications": [item.to_dict() for item in applications],
            "contract_version": 1,
            "cutover_plan_content_hash": plan.content_hash,
            "cutover_plan_id": plan.cutover_plan_id,
            "cutover_plan_revision": plan.version,
            "migration_run_id": migration_run_id,
            "project_id": plan.project_id,
            "shared_controls": {
                "control:project.integrated_reconciliation": True,
                "control:project.package_completeness": True,
            },
            "target_binding_hash": target_binding_hash,
        }
        if dict(integrated_payload) != expected:
            raise MigrationConflictError(
                "Integrated qualification payload is not the exact current evidence"
            )

    def _insert_revision(self, connection, revision: CutoverPlanRevision) -> None:
        connection.execute(
            "INSERT INTO cutover_plan_revision VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                revision.cutover_plan_id,
                revision.version,
                revision.parent_version,
                canonical_json(list(revision.shared_control_ids)),
                revision.requirement_plan_hash,
                revision.meaning_hash,
                revision.content_hash,
                revision.created_by.issuer,
                revision.created_by.subject_id,
                revision.created_by.display_name,
                revision.created_at.isoformat(),
            ],
        )
        for item in revision.selected_revisions:
            connection.execute(
                "INSERT INTO cutover_plan_recipe VALUES (?, ?, ?, ?, ?)",
                [
                    revision.cutover_plan_id,
                    revision.version,
                    item.recipe_id,
                    item.recipe_revision,
                    item.semantic_hash,
                ],
            )
        for item in revision.dependencies:
            connection.execute(
                "INSERT INTO cutover_dependency VALUES (?, ?, ?, ?, ?, ?)",
                [
                    revision.cutover_plan_id,
                    revision.version,
                    item.before_recipe_id,
                    item.after_recipe_id,
                    item.kind,
                    item.reason,
                ],
            )
        for item in revision.write_ownership:
            connection.execute(
                "INSERT INTO cutover_write_ownership VALUES (?, ?, ?, ?, ?)",
                [
                    revision.cutover_plan_id,
                    revision.version,
                    item.recipe_id,
                    item.model,
                    item.field,
                ],
            )

    @staticmethod
    def _assert_current_binding(connection, run_id: str, plan: CutoverPlanRevision) -> None:
        row = connection.execute(
            "SELECT binding.plan_content_hash, plan.current_revision "
            "FROM migration_run_cutover_plan binding JOIN cutover_plan plan "
            "ON plan.cutover_plan_id = binding.cutover_plan_id "
            "WHERE binding.migration_run_id = ? AND binding.cutover_plan_id = ? "
            "AND binding.cutover_plan_revision = ?",
            [run_id, plan.cutover_plan_id, plan.version],
        ).fetchone()
        if row is None or str(row[0]) != plan.content_hash or int(row[1]) != plan.version:
            raise MigrationConflictError(
                "The Test run no longer proves the current CutoverPlan revision"
            )

    def _current_revision_or_none(self, cutover_plan_id: str) -> CutoverPlanRevision | None:
        with self.database.connect(self.registry_path) as connection:
            row = connection.execute(
                "SELECT current_revision FROM cutover_plan WHERE cutover_plan_id = ?",
                [cutover_plan_id],
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return self.get_revision(cutover_plan_id, int(row[0]))

    @staticmethod
    def _revision_row(connection, cutover_plan_id: str, version: int):
        rows = MigrationFoundationRepository._rows(
            connection,
            "SELECT * FROM cutover_plan_revision WHERE cutover_plan_id = ? "
            "AND version = ?",
            [cutover_plan_id, version],
        )
        return rows[0] if rows else None

    def _intent(self, operation_id: str):
        with self.database.connect(self.registry_path) as connection:
            row = connection.execute(
                "SELECT operation_id FROM project_operation_intent "
                "WHERE operation_id = ?",
                [operation_id],
            ).fetchone()
        return (
            self.foundation.get_operation_intent(operation_id)
            if row is not None
            else None
        )

    @staticmethod
    def _actor_dict(actor: ActorIdentity) -> dict[str, str]:
        return {
            "display_name": actor.display_name,
            "issuer": actor.issuer,
            "subject_id": actor.subject_id,
        }

    @staticmethod
    def _actor_from_row(row: Mapping[str, object], prefix: str) -> ActorIdentity:
        return ActorIdentity(
            issuer=str(row[f"{prefix}_issuer"]),
            subject_id=str(row[f"{prefix}_subject"]),
            display_name=str(row[f"{prefix}_display_name"]),
        )

    @staticmethod
    def _revision_from_dict(value: Mapping[str, object]) -> CutoverPlanRevision:
        actor = dict(value["created_by"])
        return CutoverPlanRevision(
            cutover_plan_id=str(value["cutover_plan_id"]),
            project_id=str(value["project_id"]),
            version=int(value["version"]),
            parent_version=(
                int(value["parent_version"])
                if value["parent_version"] is not None
                else None
            ),
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
            write_ownership=tuple(
                CutoverWriteOwnership(
                    recipe_id=str(item["recipe_id"]),
                    model=str(item["model"]),
                    field=str(item["field"]),
                )
                for item in value["write_ownership"]
            ),
            shared_control_ids=tuple(str(item) for item in value["shared_control_ids"]),
            requirement_plan_hash=str(value["requirement_plan_hash"]),
            created_by=ActorIdentity(
                issuer=str(actor["issuer"]),
                subject_id=str(actor["subject_id"]),
                display_name=str(actor["display_name"]),
            ),
            created_at=datetime.fromisoformat(str(value["created_at"])),
            meaning_hash=str(value["meaning_hash"]),
            content_hash=str(value["content_hash"]),
        )
