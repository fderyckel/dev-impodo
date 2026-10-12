from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import tempfile
import unittest
from uuid import uuid4

import duckdb

from tests.support.paths import REPOSITORY_ROOT

from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.derived_entity_repository import DerivedEntityRepository
from impodo.adapters.duckdb.schema_repository import SchemaRepository
from impodo.adapters.duckdb.source_repository import SourceRepository
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.domain.odoo_capture import (
    OdooCaptureFilterPolicy,
    OdooCaptureSelection,
)
from impodo.domain.odoo_relationship_scope import OdooRelationshipScope
from impodo.domain.odoo_source_policy import ODOO_SOURCE_POLICY_HASH
from impodo.domain.run.setup import OdooConnectionMode
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.shared.models import target_identity_hash
from impodo.domain.workspace.contracts import (
    OdooModelCatalog,
    OdooModelSummary,
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)
from impodo.domain.workspace.workbench import (
    SourceMode,
    WorkspaceState,
    WorkspaceStateConflictError,
    WorkspaceStateService,
    WorkspaceStatus,
)
from tests.support.workspace_access import workspace_access_service


ROOT = REPOSITORY_ROOT
MODEL = "x.atomic.root"
TARGET_HASH = target_identity_hash(
    connection_mode="LOCAL",
    base_url="http://127.0.0.1:8069",
    database="odoo20",
)
MODEL_CATALOG_HASH = "sha256:" + "1" * 64
SCHEMA_HASH = "sha256:" + "2" * 64
READ_CREDENTIAL_HASH = "sha256:" + "3" * 64
READ_PRINCIPAL_HASH = "sha256:" + "4" * 64
READ_PERMISSION_HASH = "sha256:" + "5" * 64
READ_CONTEXT_HASH = "sha256:" + "6" * 64


class AtomicOdooCapturePublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.database = DuckDbWorkspaceDatabase(self.temporary.name)
        self.workspaces = WorkspaceStateRepository(self.database)
        self.schemas = SchemaRepository(self.database)
        self.sources = SourceRepository(
            self.database,
            DerivedEntityRepository(self.database),
        )
        self.service = WorkspaceStateService(
            self.workspaces,
            workspace_access_service(),
        )
        now = datetime.now(timezone.utc)
        self.workspace = WorkspaceState(
            workspace_id=str(uuid4()),
            name="Atomic Odoo source capture",
            source_system="Odoo",
            source_mode=SourceMode.ODOO,
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo20",
            intended_applications=("Manufacturing",),
            intended_models=("x.previous",),
            status=WorkspaceStatus.REGISTERED,
            registered_at=now,
        )
        self.workspaces.initialize_workbench(self.workspace, actor=LOCAL_ACTOR)
        self.model_catalog = _model_catalog(self.workspace.workspace_id, now)
        self.schemas.save_odoo_model_catalog(
            self.workspace.workspace_id,
            self.model_catalog,
            actor=LOCAL_ACTOR,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_publishes_scope_schema_and_generated_selection_together(self) -> None:
        schema = _schema(self.workspace.workspace_id)
        selection = _selection(schema)

        saved = self.service.update_schema_scope(
            self.workspace.workspace_id,
            actor=LOCAL_ACTOR,
            expected_revision=self.workspace.revision,
            permitted_models=(MODEL,),
            relationship_decisions=(),
            relationship_root_models=(MODEL,),
            schema_catalog=schema,
            capture_selections=(selection,),
            expected_model_catalog_content_hash=self.model_catalog.content_hash,
        )

        self.assertEqual(saved.revision, self.workspace.revision + 1)
        self.assertEqual(self.workspaces.get(saved.workspace_id), saved)
        scope = self.workspaces.get_current_odoo_relationship_scope(saved.workspace_id)
        self.assertIsNotNone(scope)
        assert scope is not None
        self.assertEqual(scope.root_models, (MODEL,))
        self.assertEqual(
            self.schemas.get_odoo_schema_catalog(saved.workspace_id),
            schema,
        )
        self.assertEqual(
            self.sources.get_current_odoo_capture_selections(saved.workspace_id),
            (selection,),
        )
        self.assertEqual(self._latest_event_type(), "ODOO_CAPTURE_PLAN_PUBLISHED")

    def test_duplicate_selection_failure_rolls_back_the_whole_publication(self) -> None:
        saved, scope, schema, selection = self._publish_initial()
        next_scope, next_schema, next_selection, candidate = self._next_publication(
            saved,
            scope,
            schema,
        )
        audit_count = self._audit_count()

        with self.assertRaises(duckdb.ConstraintException):
            self.workspaces.update_schema_scope(
                candidate,
                expected_revision=saved.revision,
                actor=LOCAL_ACTOR,
                relationship_scope=next_scope,
                schema_catalog=next_schema,
                capture_selections=(next_selection, next_selection),
                verify_relationship_scope=True,
                expected_relationship_scope_content_hash=scope.content_hash,
                expected_model_catalog_content_hash=self.model_catalog.content_hash,
            )

        self.assertEqual(self.workspaces.get(saved.workspace_id), saved)
        self.assertEqual(
            self.workspaces.get_current_odoo_relationship_scope(saved.workspace_id),
            scope,
        )
        self.assertEqual(
            self.schemas.get_odoo_schema_catalog(saved.workspace_id),
            schema,
        )
        self.assertEqual(
            self.sources.get_current_odoo_capture_selections(saved.workspace_id),
            (selection,),
        )
        self.assertEqual(self._audit_count(), audit_count)
        with self._connect() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT COUNT(*)
                      FROM odoo_relationship_scope_revision
                     WHERE scope_id = ? AND version = ?
                    """,
                    [next_scope.scope_id, next_scope.version],
                ).fetchone(),
                (0,),
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT COUNT(*)
                      FROM odoo_capture_selection_revision
                     WHERE selection_id = ?
                    """,
                    [next_selection.selection_id],
                ).fetchone(),
                (0,),
            )

    def test_stale_relationship_scope_rejects_without_writing(self) -> None:
        saved, scope, schema, selection = self._publish_initial()
        next_scope, next_schema, next_selection, candidate = self._next_publication(
            saved,
            scope,
            schema,
        )
        audit_count = self._audit_count()

        with self.assertRaisesRegex(
            WorkspaceStateConflictError,
            "relationship decisions changed",
        ):
            self.workspaces.update_schema_scope(
                candidate,
                expected_revision=saved.revision,
                actor=LOCAL_ACTOR,
                relationship_scope=next_scope,
                schema_catalog=next_schema,
                capture_selections=(next_selection,),
                verify_relationship_scope=True,
                expected_relationship_scope_content_hash="sha256:" + "f" * 64,
                expected_model_catalog_content_hash=self.model_catalog.content_hash,
            )

        self._assert_initial_publication(saved, scope, schema, selection, audit_count)

    def test_stale_model_catalog_rejects_without_writing(self) -> None:
        saved, scope, schema, selection = self._publish_initial()
        next_scope, next_schema, next_selection, candidate = self._next_publication(
            saved,
            scope,
            schema,
        )
        audit_count = self._audit_count()

        with self.assertRaisesRegex(
            WorkspaceStateConflictError,
            "model discovery changed",
        ):
            self.workspaces.update_schema_scope(
                candidate,
                expected_revision=saved.revision,
                actor=LOCAL_ACTOR,
                relationship_scope=next_scope,
                schema_catalog=next_schema,
                capture_selections=(next_selection,),
                verify_relationship_scope=True,
                expected_relationship_scope_content_hash=scope.content_hash,
                expected_model_catalog_content_hash="sha256:" + "f" * 64,
            )

        self._assert_initial_publication(saved, scope, schema, selection, audit_count)

    def test_stale_model_catalog_rejects_schema_only_publication(self) -> None:
        schema = _schema(self.workspace.workspace_id)
        audit_count = self._audit_count()

        with self.assertRaisesRegex(
            WorkspaceStateConflictError,
            "model discovery changed",
        ):
            self.service.update_schema_scope(
                self.workspace.workspace_id,
                actor=LOCAL_ACTOR,
                expected_revision=self.workspace.revision,
                permitted_models=(MODEL,),
                relationship_decisions=(),
                relationship_root_models=(MODEL,),
                schema_catalog=schema,
                expected_model_catalog_content_hash="sha256:" + "f" * 64,
            )

        self.assertEqual(
            self.workspaces.get(self.workspace.workspace_id),
            self.workspace,
        )
        self.assertIsNone(
            self.workspaces.get_current_odoo_relationship_scope(
                self.workspace.workspace_id
            )
        )
        self.assertIsNone(
            self.schemas.get_odoo_schema_catalog(self.workspace.workspace_id)
        )
        self.assertEqual(
            self.sources.get_current_odoo_capture_selections(
                self.workspace.workspace_id
            ),
            (),
        )
        self.assertEqual(self._audit_count(), audit_count)

    def _publish_initial(
        self,
    ) -> tuple[
        WorkspaceState,
        OdooRelationshipScope,
        OdooSchemaCatalog,
        OdooCaptureSelection,
    ]:
        schema = _schema(self.workspace.workspace_id)
        selection = _selection(schema)
        saved = self.service.update_schema_scope(
            self.workspace.workspace_id,
            actor=LOCAL_ACTOR,
            expected_revision=self.workspace.revision,
            permitted_models=(MODEL,),
            relationship_decisions=(),
            relationship_root_models=(MODEL,),
            schema_catalog=schema,
            capture_selections=(selection,),
            expected_model_catalog_content_hash=self.model_catalog.content_hash,
        )
        scope = self.workspaces.get_current_odoo_relationship_scope(saved.workspace_id)
        assert scope is not None
        return saved, scope, schema, selection

    def _next_publication(
        self,
        saved: WorkspaceState,
        scope: OdooRelationshipScope,
        schema: OdooSchemaCatalog,
    ) -> tuple[
        OdooRelationshipScope,
        OdooSchemaCatalog,
        OdooCaptureSelection,
        WorkspaceState,
    ]:
        next_scope = OdooRelationshipScope.create(
            scope_id=scope.scope_id,
            version=scope.version + 1,
            decisions=scope.decisions,
            root_models=scope.root_models,
            explicit_actions_by_model=scope.explicit_actions_by_model,
            recorded_at=datetime.now(timezone.utc),
            recorded_by=LOCAL_ACTOR.identity.display_name,
        )
        next_schema = replace(schema, content_hash="sha256:" + "7" * 64)
        next_selection = _selection(next_schema)
        candidate = replace(
            saved,
            revision=saved.revision + 1,
            updated_at=datetime.now(timezone.utc),
        )
        return next_scope, next_schema, next_selection, candidate

    def _assert_initial_publication(
        self,
        saved: WorkspaceState,
        scope: OdooRelationshipScope,
        schema: OdooSchemaCatalog,
        selection: OdooCaptureSelection,
        audit_count: int,
    ) -> None:
        self.assertEqual(self.workspaces.get(saved.workspace_id), saved)
        self.assertEqual(
            self.workspaces.get_current_odoo_relationship_scope(saved.workspace_id),
            scope,
        )
        self.assertEqual(
            self.schemas.get_odoo_schema_catalog(saved.workspace_id),
            schema,
        )
        self.assertEqual(
            self.sources.get_current_odoo_capture_selections(saved.workspace_id),
            (selection,),
        )
        self.assertEqual(self._audit_count(), audit_count)

    def _connect(self) -> duckdb.DuckDBPyConnection:
        database_path = (
            self.workspaces.workspace_directory(self.workspace.workspace_id)
            / "workspace-engine.duckdb"
        )
        return self.workspaces._connect(database_path)

    def _audit_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM audit_event").fetchone()
        assert row is not None
        return int(row[0])

    def _latest_event_type(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT event_type FROM audit_event ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
        assert row is not None
        return str(row[0])


def _model_catalog(workspace_id: str, now: datetime) -> OdooModelCatalog:
    return OdooModelCatalog(
        workspace_id=workspace_id,
        connection_target_hash=TARGET_HASH,
        policy_hash=ODOO_SOURCE_POLICY_HASH,
        captured_at=now,
        captured_by=LOCAL_ACTOR.identity.display_name,
        connection_mode="LOCAL",
        database="odoo20",
        odoo_version="20.0",
        models=(
            OdooModelSummary(
                name=MODEL,
                label="Atomic root",
                modules=("x_atomic",),
                state="manual",
            ),
        ),
        content_hash=MODEL_CATALOG_HASH,
        read_credential_binding_hash=READ_CREDENTIAL_HASH,
        read_principal_hash=READ_PRINCIPAL_HASH,
        read_permission_hash=READ_PERMISSION_HASH,
        read_context_hash=READ_CONTEXT_HASH,
    )


def _schema(workspace_id: str) -> OdooSchemaCatalog:
    return OdooSchemaCatalog(
        workspace_id=workspace_id,
        policy_hash=ODOO_SOURCE_POLICY_HASH,
        captured_at=datetime.now(timezone.utc),
        captured_by=LOCAL_ACTOR.identity.display_name,
        connection_mode="LOCAL",
        database="odoo20",
        odoo_version="20.0",
        models=(
            SchemaModel(
                name=MODEL,
                label="Atomic root",
                fields=(
                    SchemaField(
                        name="name",
                        label="Name",
                        type="char",
                        required=True,
                        readonly=False,
                        relation=None,
                        relation_field=None,
                        selection=(),
                        stored=True,
                        computed=False,
                        has_inverse=False,
                        related=False,
                        translated=False,
                        company_dependent=False,
                        searchable=True,
                        sortable=True,
                        exportable=True,
                    ),
                    SchemaField(
                        name="write_date",
                        label="Last Updated on",
                        type="datetime",
                        required=False,
                        readonly=True,
                        relation=None,
                        relation_field=None,
                        selection=(),
                        stored=True,
                        computed=False,
                        has_inverse=False,
                        related=False,
                        translated=False,
                        company_dependent=False,
                        searchable=True,
                        sortable=True,
                        exportable=True,
                    ),
                ),
            ),
        ),
        content_hash=SCHEMA_HASH,
        origin=SchemaOrigin.LIVE_API,
        read_credential_binding_hash=READ_CREDENTIAL_HASH,
        read_principal_hash=READ_PRINCIPAL_HASH,
        read_permission_hash=READ_PERMISSION_HASH,
        read_context_hash=READ_CONTEXT_HASH,
        connection_target_hash=TARGET_HASH,
    )


def _selection(schema: OdooSchemaCatalog) -> OdooCaptureSelection:
    return OdooCaptureSelection.create(
        selection_id=str(uuid4()),
        version=1,
        data_version_id=str(uuid4()),
        dataset_name="odoo_x_atomic_root",
        model=MODEL,
        field_names=("name",),
        filter_policy=OdooCaptureFilterPolicy.ALL_MATCHING_RECORDS,
        max_rows=10_000,
        connection_target_hash=schema.connection_target_hash,
        schema_scope_hash=schema.content_hash,
        read_principal_hash=schema.read_principal_hash,
        read_permission_hash=schema.read_permission_hash,
        context_hash=schema.read_context_hash,
        created_at=datetime.now(timezone.utc),
        created_by=LOCAL_ACTOR.identity.display_name,
    )


if __name__ == "__main__":
    unittest.main()
