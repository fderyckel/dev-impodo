"""Intent recording checks authority and current metadata without approving a load."""

from dataclasses import replace
import unittest
from unittest.mock import Mock
from uuid import uuid4

from impodo.application.business_intent_service import BusinessIntentService
from impodo.domain.workspace.business_intent import BusinessIntent, BusinessIntentError, BusinessModelIntent, ExpectedCount
from tests.support.business_intent import ACTOR, metadata_fixture


class BusinessIntentServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = metadata_fixture()
        self.workspaces, self.sources, self.schemas, self.mappings, self.repository, self.authorization = (Mock() for _ in range(6))
        self.workspaces.get.return_value = self.fixture.workspace
        self.sources.get_mapping_source_selection.return_value = self.fixture.selection
        self.schemas.get_odoo_schema_catalog.return_value = self.fixture.schema
        self.schemas.get_schema_governance.return_value = self.fixture.governance
        self.mappings.get_mapping_revision.return_value = self.fixture.mapping
        self.mappings.get_mapping_validation.return_value = self.fixture.validation
        self.authorization.require.return_value = self.fixture.access
        self.repository.save_business_intent.side_effect = lambda _id, revision, **kwargs: revision
        self.service = BusinessIntentService(workspaces=self.workspaces, sources=self.sources, schemas=self.schemas,
            mappings=self.mappings, repository=self.repository, authorization=self.authorization)
        self.workspace_id = self.fixture.workspace.workspace_id
        self.intent = BusinessIntent((BusinessModelIntent("res.partner", "update", update_fields=("email",), creates=ExpectedCount(maximum=0)),))

    def save(self, binding, *, intent=None):
        return self.service.save(self.workspace_id, intent=intent or self.intent, expected_version=0,
            expected_binding_hash=binding, operation_id=str(uuid4()), actor=ACTOR)

    def test_save_reads_each_owner_once_and_retains_verified_author(self):
        packet = self.service.inputs(self.workspace_id, actor=ACTOR)
        for port in (self.workspaces, self.sources, self.schemas, self.mappings):
            port.reset_mock()
        recorded = self.save(packet.binding_hash)
        self.assertEqual(recorded.intent, self.intent)
        self.assertEqual(recorded.author, ACTOR.identity)
        for method in (self.workspaces.get, self.sources.get_mapping_source_selection, self.schemas.get_odoo_schema_catalog,
            self.schemas.get_schema_governance, self.mappings.get_mapping_revision, self.mappings.get_mapping_validation):
            method.assert_called_once()
        self.assertEqual(recorded.version, 1)
        self.assertNotIn("approval", recorded.to_json())
        self.assertNotIn("execution_snapshot", recorded.to_json())

    def test_denial_precedes_child_evidence_access(self):
        self.authorization.require.side_effect = PermissionError("denied")
        for action in (lambda: self.service.inputs(self.workspace_id, actor=ACTOR),
            lambda: self.service.get(self.workspace_id, actor=ACTOR), lambda: self.save("unknown")):
            with self.assertRaises(PermissionError):
                action()
        self.workspaces.get.assert_not_called()
        self.sources.get_mapping_source_selection.assert_not_called()
        self.repository.get_business_intent.assert_not_called()
        self.repository.save_business_intent.assert_not_called()

    def test_stale_form_and_validation_cannot_record_new_intent(self):
        packet = self.service.inputs(self.workspace_id, actor=ACTOR)
        self.schemas.get_odoo_schema_catalog.return_value = replace(self.fixture.schema, odoo_version="20.0")
        with self.assertRaisesRegex(BusinessIntentError, "scope changed"):
            self.save(packet.binding_hash)
        self.schemas.get_odoo_schema_catalog.return_value = self.fixture.schema
        self.mappings.get_mapping_validation.return_value = replace(self.fixture.validation, mapping_content_hash="sha256:" + "0" * 64)
        with self.assertRaisesRegex(BusinessIntentError, "Recheck"):
            self.service.inputs(self.workspace_id, actor=ACTOR)
        self.repository.save_business_intent.assert_not_called()

    def test_history_needs_no_live_inputs_and_missing_intent_stays_absent(self):
        self.repository.get_business_intent.return_value = None
        self.assertIsNone(self.service.get(self.workspace_id, actor=ACTOR))
        self.workspaces.get.assert_not_called()
        self.mappings.get_mapping_revision.assert_not_called()
        self.service.get(self.workspace_id, actor=ACTOR, version=1)
        self.repository.get_business_intent.assert_called_with(self.workspace_id, 1)

    def test_unknown_run_purpose_and_production_20_never_enable_upsert(self):
        self.authorization.require.return_value = replace(self.fixture.access, run_purpose=None)
        with self.assertRaisesRegex(BusinessIntentError, "run purpose"):
            self.service.inputs(self.workspace_id, actor=ACTOR)
        self.workspaces.get.assert_not_called()
        self.authorization.require.return_value = replace(self.fixture.access, run_purpose="PRODUCTION")
        self.schemas.get_odoo_schema_catalog.return_value = replace(self.fixture.schema, odoo_version="20.0")
        packet = self.service.inputs(self.workspace_id, actor=ACTOR)
        with self.assertRaisesRegex(BusinessIntentError, "write mode"):
            self.save(packet.binding_hash, intent=BusinessIntent((BusinessModelIntent("res.partner", "upsert"),)))
