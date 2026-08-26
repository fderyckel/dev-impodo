"""Persist Project-owned Test setup selections and activation state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime

from impodo.domain.shared.access import Actor, ActorIdentity
from ...domain.serialization import canonical_json
from impodo.domain.project.foundation import (
    MigrationConflictError,
    MigrationNotFoundError,
    MigrationOperationKind,
    MigrationOperationState,
    require_hash,
    require_revision,
    require_uuid,
)
from impodo.domain.run.test_setup import (
    RecipeRunParameterValue,
    TestRunParameterValues,
    TestRunSetupBinding,
)
from .migration_foundation_repository import MigrationFoundationRepository


class TestRunRepository:
    """Own restart-safe Test setup bindings and bounded Project reads."""

    def __init__(self, foundation: MigrationFoundationRepository) -> None:
        self.foundation = foundation
        self.database = foundation.database
        self.registry_path = foundation.registry_path

    def bind_setup(
        self,
        binding: TestRunSetupBinding,
        *,
        expected_workspace_revision: int,
        operation_id: str,
        request_hash: str,
        actor: Actor,
    ) -> TestRunSetupBinding:
        operation_id = require_uuid(operation_id, "operation_id")
        intent = self.foundation._reserve_intent(
            operation_id=operation_id,
            project_id=binding.project_id,
            owner_kind="MIGRATION_RUN",
            owner_id=binding.migration_run_id,
            kind=MigrationOperationKind.TEST_RUN_SETUP,
            request_hash=require_hash(request_hash, "request_hash"),
            expected_revision=expected_workspace_revision,
            detail={"binding": binding.to_dict()},
            actor=actor,
        )
        stored = self._from_dict(dict(intent.detail["binding"]))
        if intent.state is MigrationOperationState.COMMITTED:
            return self.get(stored.migration_run_id)
        with self.database.connect(self.registry_path) as connection:
            connection.begin()
            try:
                existing = connection.execute(
                    "SELECT content_hash FROM test_run_setup_binding "
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
                        stored.test_run_setup_id,
                    )
                    connection.execute(
                        "INSERT INTO test_run_setup_binding VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        aggregate_revision=1,
                        event_type="TEST_RUN_SETUP_CREATED",
                        detail={
                            "data_version_id": stored.data_version_id,
                            "project_revision": project_revision,
                            "recipe_count": len(stored.selected_revisions),
                            "setup_workspace_id": stored.setup_workspace_id,
                        },
                        actor=actor,
                        occurred_at=stored.created_at,
                    )
                elif str(existing[0]) != stored.content_hash:
                    raise MigrationConflictError(
                        "Test run setup identity already has different meaning"
                    )
                self.foundation._commit_intent(
                    connection,
                    operation_id,
                    stage="TEST_SETUP_BOUND",
                    result={"migration_run_id": stored.migration_run_id},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get(stored.migration_run_id)

    def get(self, migration_run_id: str) -> TestRunSetupBinding:
        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        with self.database.connect(self.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                "SELECT * FROM test_run_setup_binding WHERE migration_run_id = ?",
                [migration_run_id],
            )
        if not rows:
            raise MigrationNotFoundError("Test run setup not found")
        return self._from_row(rows[0])

    def for_workspace(self, workspace_id: str) -> TestRunSetupBinding | None:
        workspace_id = require_uuid(workspace_id, "workspace_id")
        with self.database.connect(self.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                "SELECT binding.* FROM migration_workspace workspace "
                "JOIN test_run_setup_binding binding ON "
                "binding.migration_run_id = workspace.migration_run_id "
                "WHERE workspace.workspace_id = ?",
                [workspace_id],
            )
        return self._from_row(rows[0]) if rows else None

    def list_for_project(self, project_id: str) -> tuple[TestRunSetupBinding, ...]:
        project_id = require_uuid(project_id, "project_id")
        with self.database.connect(self.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                "SELECT * FROM test_run_setup_binding WHERE project_id = ? "
                "ORDER BY created_at DESC, migration_run_id DESC",
                [project_id],
            )
        return tuple(self._from_row(row) for row in rows)

    def get_parameter_values(
        self,
        migration_run_id: str,
    ) -> TestRunParameterValues | None:
        """Read the current run-owned Recipe answers in one bounded query."""

        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        with self.database.connect(self.registry_path) as connection:
            rows = self.foundation._rows(
                connection,
                "SELECT * FROM test_run_parameter_values "
                "WHERE migration_run_id = ?",
                [migration_run_id],
            )
        return self._parameter_values_from_row(rows[0]) if rows else None

    def replace_parameter_values(
        self,
        values: TestRunParameterValues,
        *,
        expected_revision: int | None,
        actor: Actor,
    ) -> TestRunParameterValues:
        """Replace run-owned answers before the Test setup is activated."""

        with self.database.connect(self.registry_path) as connection:
            connection.begin()
            try:
                binding = connection.execute(
                    "SELECT test_run_setup_id, project_id, state "
                    "FROM test_run_setup_binding WHERE migration_run_id = ?",
                    [values.migration_run_id],
                ).fetchone()
                if binding != (
                    values.test_run_setup_id,
                    values.project_id,
                    "SETUP",
                ):
                    raise MigrationConflictError(
                        "Run values require the current editable Test setup"
                    )
                current = connection.execute(
                    "SELECT revision FROM test_run_parameter_values "
                    "WHERE migration_run_id = ?",
                    [values.migration_run_id],
                ).fetchone()
                stored_values = canonical_json(
                    [item.to_dict() for item in values.values]
                )
                if current is None:
                    if expected_revision is not None or values.revision != 1:
                        raise MigrationConflictError(
                            "Run values changed; reload and retry"
                        )
                    connection.execute(
                        "INSERT INTO test_run_parameter_values VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            values.test_run_setup_id,
                            values.project_id,
                            values.migration_run_id,
                            values.revision,
                            stored_values,
                            values.content_hash,
                            values.updated_by.issuer,
                            values.updated_by.subject_id,
                            values.updated_by.display_name,
                            values.updated_at.isoformat(),
                            values.contract_version,
                        ],
                    )
                else:
                    expected = require_revision(
                        expected_revision,
                        "expected_run_parameter_values_revision",
                    )
                    if current != (expected,) or values.revision != expected + 1:
                        raise MigrationConflictError(
                            "Run values changed; reload and retry"
                        )
                    updated = connection.execute(
                        """
                        UPDATE test_run_parameter_values
                           SET revision = ?, values_json = ?, content_hash = ?,
                               updated_by_issuer = ?, updated_by_subject = ?,
                               updated_by_display_name = ?, updated_at = ?,
                               contract_version = ?
                         WHERE migration_run_id = ? AND revision = ?
                         RETURNING migration_run_id
                        """,
                        [
                            values.revision,
                            stored_values,
                            values.content_hash,
                            values.updated_by.issuer,
                            values.updated_by.subject_id,
                            values.updated_by.display_name,
                            values.updated_at.isoformat(),
                            values.contract_version,
                            values.migration_run_id,
                            expected,
                        ],
                    ).fetchone()
                    if updated is None:
                        raise MigrationConflictError(
                            "Run values changed; reload and retry"
                        )
                self.foundation._insert_event(
                    connection,
                    project_id=values.project_id,
                    aggregate_kind="TEST_RUN_PARAMETER_VALUES",
                    aggregate_id=values.migration_run_id,
                    aggregate_revision=values.revision,
                    event_type="TEST_RUN_PARAMETER_VALUES_REPLACED",
                    detail={
                        "content_hash": values.content_hash,
                        "parameter_count": len(values.values),
                    },
                    actor=actor,
                    occurred_at=values.updated_at,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        saved = self.get_parameter_values(values.migration_run_id)
        if saved is None:
            raise MigrationConflictError("Run values were not saved")
        return saved

    @staticmethod
    def _validate_setup(connection, binding: TestRunSetupBinding) -> None:
        run = connection.execute(
            "SELECT project_id, data_version_id, purpose, state, target_binding_id "
            "FROM migration_run WHERE migration_run_id = ?",
            [binding.migration_run_id],
        ).fetchone()
        if run != (
            binding.project_id,
            binding.data_version_id,
            "TEST",
            "DRAFT",
            None,
        ):
            raise MigrationConflictError("Test setup does not match its draft run")
        data_version = connection.execute(
            "SELECT project_id, purpose, state FROM data_version "
            "WHERE data_version_id = ?",
            [binding.data_version_id],
        ).fetchone()
        if data_version != (binding.project_id, "TEST", "DRAFT"):
            raise MigrationConflictError(
                "Test setup requires one fresh draft Test DataVersion"
            )
        workspace = connection.execute(
            "SELECT project_id, data_version_id, migration_run_id, "
            "recipe_application_id FROM migration_workspace WHERE workspace_id = ?",
            [binding.setup_workspace_id],
        ).fetchone()
        if workspace != (
            binding.project_id,
            binding.data_version_id,
            binding.migration_run_id,
            None,
        ):
            raise MigrationConflictError("Test setup workspace does not match its run")
        for selection in binding.selected_revisions:
            recipe = connection.execute(
                "SELECT recipe.project_id, revision.semantic_hash FROM recipe "
                "JOIN recipe_revision revision ON revision.recipe_id = recipe.recipe_id "
                "WHERE recipe.recipe_id = ? AND revision.version = ?",
                [selection.recipe_id, selection.recipe_revision],
            ).fetchone()
            if recipe != (binding.project_id, selection.semantic_hash):
                raise MigrationConflictError(
                    "Test setup Recipe version is unavailable or changed"
                )

    @staticmethod
    def _values(binding: TestRunSetupBinding) -> list[object]:
        return [
            binding.test_run_setup_id,
            binding.project_id,
            binding.migration_run_id,
            binding.data_version_id,
            binding.setup_workspace_id,
            canonical_json([item.to_dict() for item in binding.selected_revisions]),
            canonical_json([item.to_dict() for item in binding.dependencies]),
            binding.state.value,
            binding.target_binding_id,
            binding.content_hash,
            binding.created_at.isoformat(),
            binding.activated_at.isoformat() if binding.activated_at else None,
            binding.contract_version,
        ]

    @classmethod
    def _from_row(cls, value: Mapping[str, object]) -> TestRunSetupBinding:
        return cls._from_dict(
            {
                **dict(value),
                "selected_revisions": json.loads(str(value["selected_revisions_json"])),
                "dependencies": json.loads(str(value["dependencies_json"])),
            }
        )

    @staticmethod
    def _from_dict(value: Mapping[str, object]) -> TestRunSetupBinding:
        return TestRunSetupBinding.from_dict(dict(value))

    @staticmethod
    def _parameter_values_from_row(
        value: Mapping[str, object],
    ) -> TestRunParameterValues:
        result = TestRunParameterValues(
            test_run_setup_id=str(value["test_run_setup_id"]),
            project_id=str(value["project_id"]),
            migration_run_id=str(value["migration_run_id"]),
            revision=int(value["revision"]),
            values=tuple(
                RecipeRunParameterValue(
                    recipe_id=str(item["recipe_id"]),
                    logical_parameter_id=str(item["logical_parameter_id"]),
                    value=item["value"],
                )
                for item in json.loads(str(value["values_json"]))
            ),
            updated_by=ActorIdentity(
                issuer=str(value["updated_by_issuer"]),
                subject_id=str(value["updated_by_subject"]),
                display_name=str(value["updated_by_display_name"]),
            ),
            updated_at=datetime.fromisoformat(str(value["updated_at"])),
            contract_version=int(value["contract_version"]),
        )
        if str(value["content_hash"]) != result.content_hash:
            raise ValueError("Stored Test run value hash is inconsistent")
        return result
