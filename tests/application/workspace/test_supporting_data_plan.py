"""Authorized bulk metadata orchestration, independent of storage adapters."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from impodo.application.schema_workspace_service import SchemaWorkspaceService
from impodo.domain.shared.access import Actor, ActorIdentity, Capability
from impodo.domain.shared.models import target_identity_hash
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.supporting_plan import (
    MAX_SUPPORTING_RULES,
    SupportingPlanGap,
    SupportingPlanRole,
)
from impodo.domain.workspace.workbench import OdooConnectionMode, SourceMode
from tests.domain.workspace.test_supporting_plan import model, relation
from tests.domain.workspace.test_supporting_sources import (
    accepted_selection,
    lookup_rule,
    preparation,
)


class SupportingDataPlanningApplicationTests(unittest.TestCase):
    def setUp(self):
        self.actor = Actor(
            ActorIdentity("organization", "member", "Data manager"),
            frozenset({Capability.PROJECT_VIEW}),
        )
        self.authorization = Mock()
        self.workspaces = Mock()
        self.workspaces.get.return_value = SimpleNamespace(
            source_mode=SourceMode.FILE,
            intended_models=("x.product",),
            odoo_connection_mode=OdooConnectionMode.REMOTE,
            odoo_base_url="https://odoo.example.test",
            odoo_database="fictional",
        )
        self.sources, self.schemas, self.derived = Mock(), Mock(), Mock()
        self.selection = accepted_selection()
        self.preparation = preparation(self.selection, lookup_rule())
        self.schema = SimpleNamespace(
            odoo_version="19.0",
            content_hash="schema-hash",
            connection_target_hash=target_identity_hash(
                connection_mode="REMOTE",
                base_url="https://odoo.example.test",
                database="fictional",
            ),
            models=(
                model("x.product", relation("category_id", "x.category")),
                model("x.category"),
            ),
        )
        self.sources.get_source_selection.return_value = self.selection
        self.schemas.get_odoo_schema_catalog.return_value = self.schema
        self.derived.get_derived_entity_plan.return_value = self.preparation
        self.service = SchemaWorkspaceService(
            self.workspaces,
            self.sources,
            self.schemas,
            self.authorization,
            derived_entities=self.derived,
        )

    def query(self, **overrides):
        values = dict(
            expected_source_selection_hash=self.selection.content_hash,
            expected_schema_hash=self.schema.content_hash,
            expected_preparation_hash=self.preparation.content_hash,
            actor=self.actor,
        )
        values.update(overrides)
        return self.service.supporting_data_plan("workspace", **values)

    def test_named_actor_is_authorized_before_four_bulk_reads(self):
        calls = Mock()
        calls.attach_mock(self.authorization, "authorization")
        calls.attach_mock(self.workspaces, "workspaces")
        calls.attach_mock(self.sources, "sources")
        calls.attach_mock(self.schemas, "schemas")
        calls.attach_mock(self.derived, "derived")
        result = self.query()
        self.assertTrue(result.complete)
        self.assertEqual(result.edges[0].role, SupportingPlanRole.INCOMING_CANDIDATE)
        self.authorization.require.assert_called_once_with(
            self.actor, Capability.PROJECT_VIEW, workspace_id="workspace"
        )
        self.assertEqual(
            [item[0] for item in calls.mock_calls],
            [
                "authorization.require",
                "workspaces.get",
                "sources.get_source_selection",
                "schemas.get_odoo_schema_catalog",
                "derived.get_derived_entity_plan",
            ],
        )
        self.assertEqual(
            self.workspaces.get.return_value.intended_models, ("x.product",)
        )

    def test_denial_precedes_all_evidence_reads(self):
        self.authorization.require.side_effect = PermissionError("denied")
        with self.assertRaises(PermissionError):
            self.query()
        self.workspaces.get.assert_not_called()
        self.sources.get_source_selection.assert_not_called()
        self.schemas.get_odoo_schema_catalog.assert_not_called()
        self.derived.get_derived_entity_plan.assert_not_called()

    def test_changed_source_schema_or_preparation_rejects_the_proposal(self):
        for key in (
            "expected_source_selection_hash",
            "expected_schema_hash",
            "expected_preparation_hash",
        ):
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(WorkspaceError, "inputs changed"),
            ):
                self.query(**{key: "stale"})

    def test_other_workspace_or_source_plan_is_rejected(self):
        self.derived.get_derived_entity_plan.return_value = replace(
            self.preparation, workspace_id="other"
        )
        with self.assertRaisesRegex(WorkspaceError, "another workspace"):
            self.query(
                expected_preparation_hash=self.derived.get_derived_entity_plan.return_value.content_hash
            )
        stale = replace(self.preparation, source_selection_hash="stale")
        self.derived.get_derived_entity_plan.return_value = stale
        with self.assertRaisesRegex(ValueError, "accepted selection"):
            self.query(expected_preparation_hash=stale.content_hash)

    def test_target_change_and_missing_capture_are_rejected(self):
        self.schema.connection_target_hash = "other-target"
        with self.assertRaisesRegex(WorkspaceError, "current target"):
            self.query()
        self.schemas.get_odoo_schema_catalog.return_value = None
        with self.assertRaisesRegex(WorkspaceError, "capture Odoo"):
            self.query()

    def test_odoo_source_keeps_its_existing_relationship_scope_owner(self):
        self.workspaces.get.return_value.source_mode = SourceMode.ODOO
        with self.assertRaisesRegex(WorkspaceError, "reviewed relationship scope"):
            self.query()
        self.sources.get_source_selection.assert_not_called()

    def test_metadata_limit_returns_an_explicit_incomplete_plan(self):
        oversized = preparation(
            self.selection, *(lookup_rule() for _ in range(MAX_SUPPORTING_RULES + 1))
        )
        self.derived.get_derived_entity_plan.return_value = oversized
        result = self.query(expected_preparation_hash=oversized.content_hash)
        self.assertFalse(result.complete)
        self.assertEqual(result.issues[0].reason, SupportingPlanGap.INPUT_LIMIT)
        self.assertEqual(result.edges, ())
