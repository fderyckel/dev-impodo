"""Exercise saved-data lifecycle commands with explicit persistence and I/O seams.

Registry and artifact integration suites prove physical persistence. These
tests isolate configuration meaning, admission, phase ownership, and retries
across publication/acceptance boundaries without requiring an Odoo server.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4, uuid5

from impodo.application.odoo_saved_data_service import (
    SavedOdooCaptureAccess,
    SavedOdooDataService,
)
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo_capture import OdooCaptureFilterPolicy, OdooCaptureSelection
from impodo.domain.odoo_source_policy import odoo_source_policy_hash
from impodo.domain.project.foundation import (
    MigrationConflictError,
    MigrationNotFoundError,
)
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.shared.models import target_identity_hash
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    SourceMode,
    WorkspaceState,
    WorkspaceStatus,
)


class SavedOdooDataServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._setup(19)

    def _setup(self, major):
        self.major = major
        self.project = str(uuid4())
        self.target = target_identity_hash(
            connection_mode="REMOTE", base_url="https://source.example.test",
            database="odoo-dev",
        )
        self.company = OdooCompanySelection(self.target, 1, "Company A")
        self.source_id = str(uuid4())
        self.now = datetime.now(UTC)
        self.state_rows = {
            self.source_id: WorkspaceState(
                workspace_id=self.source_id,
                name="Capture source",
                source_system="Odoo",
                source_mode=SourceMode.ODOO,
                status=WorkspaceStatus.REGISTERED,
                odoo_connection_mode=OdooConnectionMode.REMOTE,
                odoo_base_url="https://source.example.test",
                odoo_database="odoo-dev",
                intended_models=("res.partner",),
                odoo_company=self.company,
                created_at=self.now,
                updated_at=self.now,
            )
        }
        self.owners = {
            self.source_id: SimpleNamespace(
                project_id=self.project, data_version_id=str(uuid4())
            )
        }
        self.fields = (
            SchemaField(
                "name",
                "Name",
                "char",
                True,
                False,
                None,
                None,
                (),
                stored=True,
                computed=False,
                has_inverse=False,
                related=False,
                translated=False,
                company_dependent=False,
                searchable=True,
                exportable=True,
            ),
        )
        self.schema_rows = {
            self.source_id: self._schema(self.state_rows[self.source_id])
        }
        self.choice_rows = {self.source_id: (self._choice(self.source_id),)}
        self.captured = {}
        self.snapshot_rows = {}
        self.manifest_rows = {}
        self.definitions = {}
        self.history = {}
        self.versions = {}
        self.operations = {}
        self.capture_calls = 0
        self.generation_hash = content_hash("source generation 1")
        self.binding_rows = {}
        self.configuration_bindings = {}
        self.database_bindings = Mock()
        self.database_bindings.require_current.side_effect = self._require_binding
        self._bind_source(self.source_id)
        self.accepted = set()
        self.local_depth = 0
        self.repository = Mock()
        self.repository.get_definition.side_effect = self._get_definition
        self.repository.get_configuration.side_effect = (
            lambda project, identity, revision: self.history[(identity, revision)]
        )
        self.repository.create_definition.side_effect = self._create_definition
        self.repository.save_definition.side_effect = self._save_definition
        self.repository.rebind_source_definition.side_effect = self._save_definition
        self.repository.get_configuration_source_binding.side_effect = (
            lambda project, identity, revision: self.configuration_bindings.get((identity, revision))
        )
        self.repository.get_version_for_operation.side_effect = (
            lambda project, identity, operation: self.versions.get(operation)
        )
        self.repository.get_capture_operation.side_effect = (
            lambda project, identity, operation: self.operations.get(operation)
        )
        self.repository.bind_capture_operation.side_effect = lambda operation: (
            self.operations.setdefault(operation.operation_id, operation)
        )
        self.repository.append_version.side_effect = self._append_version
        self.access = Mock()
        self.access.require.side_effect = lambda actor, capability, workspace_id: (
            self.owners[workspace_id]
        )
        self.states = Mock()
        self.states.repository.get.side_effect = lambda identity: self.state_rows[
            identity
        ]
        self.states.repository.get_current_odoo_relationship_scope.return_value = None
        self.states.update_schema_scope.side_effect = self._update_scope
        self.evidence = Mock()
        self.evidence.get_odoo_schema_catalog.side_effect = lambda identity: (
            self.schema_rows.get(identity)
        )
        self.evidence.get_current_odoo_capture_selections.side_effect = (
            lambda identity: self.choice_rows.get(identity, ())
        )
        self.evidence.get_source_selection.side_effect = lambda identity: (
            self.captured.get(identity)
        )
        self.evidence.get_current_source_snapshots.side_effect = lambda identity: (
            self.snapshot_rows.get(identity, ())
        )
        self.workspaces = Mock()
        self.workspaces.prepare_refresh.side_effect = self._fresh
        self.sources = Mock()
        self.sources.capture_filters = None
        self.schemas = Mock()
        self.schemas.prepare_capture.side_effect = lambda state, metadata, **kwargs: (
            self._schema(state)
        )
        self.provenance = Mock()
        self.provenance.current_manifests.side_effect = lambda identity, **kwargs: (
            self.manifest_rows.get(identity, ())
        )
        self.publication = Mock()
        self.publication.publish.side_effect = self._publish
        self.projections = Mock()
        self.projections.accept_odoo_capture.side_effect = self._accept
        self.companies = Mock()
        self.companies.set_source_company.side_effect = self._set_company
        self.factory = Mock(side_effect=self._live)
        self.authorization = Mock()
        self.service = self._service()

    def _service(self):
        return SavedOdooDataService(
            repository=self.repository,
            access=self.access,
            authorization=self.authorization,
            states=self.states,
            evidence=self.evidence,
            workspaces=self.workspaces,
            sources=self.sources,
            schemas=self.schemas,
            provenance=self.provenance,
            publication=self.publication,
            projections=self.projections,
            source_companies=self.companies,
            database_bindings=self.database_bindings,
            local_operation=self._local,
        )

    def _local(self, operation):
        self.local_depth += 1
        try:
            return operation()
        finally:
            self.local_depth -= 1

    def _schema(self, state):
        return OdooSchemaCatalog(
            workspace_id=state.workspace_id,
            policy_hash=odoo_source_policy_hash(self.major),
            captured_at=self.now,
            captured_by="Fixture",
            connection_mode=state.odoo_connection_mode.value,
            database="odoo-dev",
            odoo_version=f"{self.major}.0",
            models=(SchemaModel("res.partner", "Contact", self.fields),),
            content_hash=content_hash(
                (self.major, tuple(field.name for field in self.fields))
            ),
            origin=SchemaOrigin.LIVE_API,
            read_credential_binding_hash=content_hash("credential"),
            read_principal_hash=content_hash("principal"),
            read_permission_hash=content_hash("permission"),
            read_context_hash=state.odoo_company.context_hash,
            connection_target_hash=self.target,
        )

    def _choice(self, identity):
        schema = self.schema_rows[identity]
        return OdooCaptureSelection.create(
            selection_id=str(uuid4()),
            version=1,
            data_version_id=self.owners[identity].data_version_id,
            dataset_name="contacts",
            model="res.partner",
            field_names=("name",),
            filter_policy=OdooCaptureFilterPolicy.ACTIVE_AND_ARCHIVED_RECORDS,
            max_rows=100,
            policy_hash=schema.policy_hash,
            connection_target_hash=self.target,
            schema_scope_hash=schema.content_hash,
            read_principal_hash=schema.read_principal_hash,
            read_permission_hash=schema.read_permission_hash,
            context_hash=schema.read_context_hash,
            created_at=self.now,
            created_by="Fixture",
        )

    def _get_definition(self, project, identity):
        result = self.definitions.get(identity)
        if result is None or result.project_id != project:
            raise MigrationNotFoundError("Definition missing")
        return result

    def _create_definition(self, definition, *, source_binding_hash):
        self.definitions[definition.saved_data_id] = definition
        self.history[(definition.saved_data_id, 1)] = definition
        self.configuration_bindings[(definition.saved_data_id, 1)] = source_binding_hash
        return definition

    def _save_definition(self, definition, *, expected_revision, source_binding_hash):
        current = self.definitions[definition.saved_data_id]
        if current.revision != expected_revision:
            raise MigrationConflictError("Stale fixture revision")
        updated = replace(
            definition,
            revision=current.revision + 1,
            configuration_revision=current.configuration_revision + 1,
        )
        self.definitions[definition.saved_data_id] = updated
        self.history[(updated.saved_data_id, updated.configuration_revision)] = updated
        self.configuration_bindings[(updated.saved_data_id, updated.configuration_revision)] = source_binding_hash
        return updated

    def _append_version(self, version, *, expected_revision, source_binding_hash):
        current = self.definitions[version.saved_data_id]
        if current.revision != expected_revision:
            raise MigrationConflictError("Stale fixture revision")
        self.assertIn(version.workspace_id, self.accepted)
        self.assertEqual(source_binding_hash, self.configuration_bindings.get((version.saved_data_id, version.configuration_revision)))
        updated = replace(
            current,
            revision=current.revision + 1,
            latest_version=version.version_number,
        )
        self.definitions[current.saved_data_id] = updated
        self.versions[version.operation_id] = version
        return updated, version

    def _fresh(self, identity, *, actor, operation_id):
        self.assertGreater(self.local_depth, 0)
        fresh_id = str(uuid5(UUID(operation_id), "workspace"))
        if fresh_id not in self.state_rows:
            self.state_rows[fresh_id] = replace(
                self.state_rows[identity], workspace_id=fresh_id
            )
            self.owners[fresh_id] = SimpleNamespace(
                project_id=self.project, data_version_id=str(uuid4())
            )
        return self.state_rows[fresh_id]

    def _set_company(self, identity, company, *, actor, expected_revision):
        self.state_rows[identity] = replace(
            self.state_rows[identity], odoo_company=company
        )

    def _update_scope(self, identity, **kwargs):
        self.schema_rows[identity] = kwargs["schema_catalog"]
        self.choice_rows[identity] = tuple(kwargs["capture_selections"])

    def _live(self, state, credential_owner):
        self.assertEqual(self.local_depth, 0)
        self._bind_source(state.workspace_id)
        return SavedOdooCaptureAccess(
            Mock(), None, content_hash("credential"), Mock(), model_catalog=Mock()
        )

    def _bind_source(self, identity):
        self.binding_rows[identity] = SimpleNamespace(
            content_hash=content_hash((identity, self.generation_hash)),
            generation_hash=self.generation_hash,
            project_id=self.project,
        )

    def _require_binding(self, identity, role, **kwargs):
        binding = self.binding_rows.get(identity)
        if binding is None or binding.generation_hash != self.generation_hash:
            raise WorkspaceError("The source database generation changed; review and rebind its source")
        return binding

    def _publish(self, identity, gateway, *, actor):
        self.assertEqual(self.local_depth, 0)
        self.capture_calls += 1
        dataset = SimpleNamespace(dataset_id=self.choice_rows[identity][0].dataset_id)
        selection = SimpleNamespace(
            data_version_id=self.owners[identity].data_version_id,
            datasets=(dataset,),
            content_hash=content_hash((identity, self.capture_calls)),
        )
        self.captured[identity] = selection
        self.snapshot_rows[identity] = (Mock(),)
        self.manifest_rows[identity] = (
            SimpleNamespace(
                dataset_id=dataset.dataset_id,
                model="res.partner",
                row_count=2,
                content_hash=content_hash((identity, "manifest", self.capture_calls)),
                capture_finished_at=datetime.now(UTC),
            ),
        )
        return SimpleNamespace(
            source_selection=selection,
            source_snapshots=self.snapshot_rows[identity],
            manifests=self.manifest_rows[identity],
        )

    def _accept(self, identity, selection, snapshots, manifests, *, actor):
        self.assertGreater(self.local_depth, 0)
        self.accepted.add(identity)

    def _create(self, *, all_fields=False):
        return self.service.create(
            self.source_id,
            actor=LOCAL_ACTOR,
            label="Clean contacts",
            all_fields=all_fields,
        )

    def _refresh(self, definition, operation=None):
        return self.service.refresh(
            definition.project_id,
            definition.saved_data_id,
            actor=LOCAL_ACTOR,
            expected_revision=definition.revision,
            operation_id=operation or str(uuid4()),
            access_factory=self.factory,
        )

    def test_configure_replay_keeps_initial_meaning_after_revision_and_avoids_remote_reads(
        self,
    ):
        for major in (19, 20):
            with self.subTest(major=major):
                self._setup(major)
                operation = str(uuid4())
                definition = self.service.configure(
                    self.source_id,
                    actor=LOCAL_ACTOR,
                    label="Clean contacts",
                    source_company=self.company,
                    all_fields=False,
                    operation_id=operation,
                    access_factory=self.factory,
                )
                revised = self.service.revise(
                    definition.project_id,
                    definition.saved_data_id,
                    definition.source_workspace_id,
                    actor=LOCAL_ACTOR,
                    expected_revision=definition.revision,
                    label="Revised label",
                    all_fields=True,
                )
                self.factory.reset_mock()
                replay = self.service.configure(
                    self.source_id,
                    actor=LOCAL_ACTOR,
                    label="Clean contacts",
                    source_company=self.company,
                    all_fields=False,
                    operation_id=operation,
                    access_factory=self.factory,
                )
                self.assertEqual(replay, revised)
                self.factory.assert_not_called()
                with self.assertRaises(MigrationConflictError):
                    self.service.configure(
                        self.source_id,
                        actor=LOCAL_ACTOR,
                        label="Changed request",
                        source_company=self.company,
                        all_fields=False,
                        operation_id=operation,
                        access_factory=self.factory,
                    )

    def test_failed_configuration_remote_read_preserves_source_choices(self):
        self.factory.side_effect = RuntimeError("Source unavailable")
        original = self.choice_rows[self.source_id]
        with self.assertRaisesRegex(RuntimeError, "Source unavailable"):
            self.service.configure(
                self.source_id,
                actor=LOCAL_ACTOR,
                label="Clean contacts",
                source_company=self.company,
                all_fields=False,
                operation_id=str(uuid4()),
                access_factory=self.factory,
            )
        self.companies.set_source_company.assert_not_called()
        self.assertEqual(self.choice_rows[self.source_id], original)
        self.assertEqual(self.definitions, {})

    def test_refresh_repeats_without_selection_id_drift_and_completed_replay_is_offline(
        self,
    ):
        for major in (19, 20):
            with self.subTest(major=major):
                self._setup(major)
                definition = self._create()
                operation = str(uuid4())
                first = self._refresh(definition, operation)
                self.factory.reset_mock()
                self.service = self._service()
                self.assertEqual(self._refresh(definition, operation), first)
                self.factory.assert_not_called()
                updated = self.definitions[definition.saved_data_id]
                second = self._refresh(updated)
                self.assertEqual(second.version_number, 2)
                self.assertNotEqual(first.workspace_id, second.workspace_id)
                self.assertEqual(self.capture_calls, 2)

    def test_interrupted_acceptance_reuses_complete_publication_without_source(self):
        for major in (19, 20):
            with self.subTest(major=major):
                self._setup(major)
                definition = self._create()
                operation = str(uuid4())
                self.projections.accept_odoo_capture.side_effect = RuntimeError(
                    "Interrupted acceptance"
                )
                with self.assertRaisesRegex(RuntimeError, "Interrupted acceptance"):
                    self._refresh(definition, operation)
                self.assertIsNone(
                    self.definitions[definition.saved_data_id].latest_version
                )
                self.assertEqual(len(self.operations), 1)
                self.projections.accept_odoo_capture.side_effect = self._accept
                self.factory.side_effect = AssertionError(
                    "Source must not be contacted"
                )
                self.service = self._service()
                recovered = self._refresh(definition, operation)
                self.assertEqual(recovered.version_number, 1)
                self.assertEqual(self.capture_calls, 1)

    def test_interrupted_version_append_reuses_accepted_capture_and_keeps_old_version(
        self,
    ):
        definition = self._create()
        first = self._refresh(definition)
        updated = self.definitions[definition.saved_data_id]
        operation = str(uuid4())
        self.repository.append_version.side_effect = RuntimeError("Interrupted append")
        with self.assertRaisesRegex(RuntimeError, "Interrupted append"):
            self._refresh(updated, operation)
        self.assertEqual(self.definitions[definition.saved_data_id].latest_version, 1)
        self.assertIn(first, self.versions.values())
        self.repository.append_version.side_effect = self._append_version
        self.factory.side_effect = AssertionError("Source must not be contacted")
        self.service = self._service()
        recovered = self._refresh(updated, operation)
        self.assertEqual(recovered.version_number, 2)
        self.assertEqual(self.capture_calls, 2)

    def test_source_database_company_and_configuration_drift_block_before_remote(self):
        definition = self._create()
        original_schema = self.schema_rows[self.source_id]
        self.schema_rows[self.source_id] = replace(
            original_schema, connection_target_hash=content_hash("odoo-test")
        )
        with self.assertRaisesRegex(WorkspaceError, "source database changed"):
            self._refresh(definition)
        self.schema_rows[self.source_id] = original_schema
        self.state_rows[self.source_id] = replace(
            self.state_rows[self.source_id],
            odoo_company=OdooCompanySelection(self.target, 2, "Company B"),
        )
        with self.assertRaisesRegex(WorkspaceError, "source company changed"):
            self._refresh(definition)
        self.factory.assert_not_called()

    def test_all_fields_uses_only_restorable_scalar_values_for_each_major(self):
        for major in (19, 20):
            with self.subTest(major=major):
                self._setup(major)
                self.fields = (
                    *self.fields,
                    replace(self.fields[0], name="readonly_value", readonly=True),
                    replace(
                        self.fields[0],
                        name="computed_value",
                        computed=True,
                        has_inverse=False,
                    ),
                    replace(
                        self.fields[0],
                        name="editable_inverse",
                        computed=True,
                        has_inverse=True,
                    ),
                    replace(
                        self.fields[0], name="company_value", company_dependent=True
                    ),
                    replace(
                        self.fields[0],
                        name="relation_value",
                        type="many2one",
                        relation="res.company",
                    ),
                )
                self._refresh(self._create(all_fields=True))
                self.assertEqual(
                    self.choice_rows[self.source_id][0].field_names,
                    ("editable_inverse", "name"),
                )
