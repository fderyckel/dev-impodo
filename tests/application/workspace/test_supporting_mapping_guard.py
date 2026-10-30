"""Consumer freshness checks reuse metadata and preserve legacy and Odoo owners."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from impodo.application.supporting_mapping_guard import SupportingMappingGuard
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import SourceMode
from tests.application.workspace import test_supporting_review as review_fixture
from tests.domain.workspace.test_supporting_closure import relationship
from impodo.domain.mapping.contracts import DatasetMapping


class SupportingMappingGuardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = review_fixture.SupportingReviewApplicationTests()
        self.fixture.setUp()
        self.review = self.fixture.save()
        self.workspaces, self.sources, self.reviews, self.preparation = (Mock() for _ in range(4))
        self.workspaces.get.return_value = self.fixture.workspace
        self.sources.get_source_selection.return_value = self.fixture.selection
        self.reviews.get_supporting_review.return_value = self.review
        self.preparation.get_derived_entity_plan.return_value = None
        self.guard = SupportingMappingGuard(self.workspaces, self.sources, self.reviews, self.preparation)
        self.datasets = (DatasetMapping("products", "res.partner", relationships=(
            relationship("country_id", "res.country"),
        )),)
        # Country's reviewed standard key is code, rather than the generic name fixture.
        from dataclasses import replace
        from impodo.domain.mapping.contracts import ReferenceKeyMapping
        rel = self.datasets[0].relationships[0]
        self.datasets = (replace(self.datasets[0], relationships=(replace(rel, resolver=replace(rel.resolver, key_mappings=(ReferenceKeyMapping("source", "code"),))),)),)

    def test_reads_each_metadata_owner_once_and_rejects_changed_source(self):
        self.guard.require_mapping("workspace", self.datasets, self.fixture.schema, None)
        self.reviews.get_supporting_review.assert_called_once_with("workspace")
        self.sources.get_source_selection.assert_called_once_with("workspace")
        self.preparation.get_derived_entity_plan.assert_called_once_with("workspace")
        from dataclasses import replace
        self.sources.get_source_selection.return_value = replace(self.fixture.selection, content_hash="changed")
        with self.assertRaisesRegex(WorkspaceError, "need refresh"):
            self.guard.require_mapping("workspace", self.datasets, self.fixture.schema, None)

    def test_preparation_reuses_loaded_source_and_rules_and_blocks_changed_access(self):
        schemas = SimpleNamespace(get_odoo_schema_catalog=lambda _id: self.fixture.schema, get_schema_governance=lambda _id: None)
        self.guard.require_preparation("workspace", self.datasets, schemas, self.fixture.workspace, self.fixture.selection, None)
        self.workspaces.get.assert_not_called()
        self.sources.get_source_selection.assert_not_called()
        self.preparation.get_derived_entity_plan.assert_not_called()
        self.fixture.schema.read_permission_hash = "changed"
        with self.assertRaisesRegex(WorkspaceError, "need refresh"):
            self.guard.require_preparation("workspace", self.datasets, schemas, self.fixture.workspace, self.fixture.selection, None)

    def test_legacy_and_odoo_source_keep_their_existing_decision_owner(self):
        self.reviews.get_supporting_review.return_value = None
        self.assertIsNone(self.guard.require_mapping("workspace", self.datasets, self.fixture.schema, None))
        self.sources.get_source_selection.assert_not_called()
        self.reviews.get_supporting_review.return_value = self.review
        self.fixture.workspace.source_mode = SourceMode.ODOO
        self.assertIsNone(self.guard.require_mapping("workspace", self.datasets, self.fixture.schema, None))
        self.sources.get_source_selection.assert_not_called()

    def test_changed_derived_rules_retire_the_review_without_evaluating_rows(self):
        from tests.domain.workspace.test_supporting_sources import preparation, lookup_rule
        self.preparation.get_derived_entity_plan.return_value = preparation(self.fixture.selection, lookup_rule())
        with self.assertRaisesRegex(WorkspaceError, "need refresh"):
            self.guard.require_mapping("workspace", self.datasets, self.fixture.schema, None)
